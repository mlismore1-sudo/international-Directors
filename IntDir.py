import base64
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Companies by SIC + Director Residence", layout="wide")

TARGET_SIC_CODES = sorted({
    "62012", "62020", "63120", "47910", "46190", "46499",
    "70229", "73110", "74909", "68209", "64209", "68100",
    "32990", "10890", "86900", "93130", "96040", "82990",
})
TARGET_SIC_CODE_SET = set(TARGET_SIC_CODES)

TARGET_RESIDENCE_COUNTRIES = {
    "ireland",
    "france",
    "poland",
    "germany",
    "spain",
    "portugal",
    "belgium",
    "austria",
    "netherlands",
    "the netherlands",
    "croatia",
    "denmark",
    "sweden",
    "norway",
    "finland",
}

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
LEADS_DIR = DATA_DIR / "leads"
LEADS_DIR.mkdir(exist_ok=True)

TEAM_MEMBERS = ["Brad", "James"]
QUICK_ADD_DEFAULT = 15

RESULT_COLUMNS = [
    "company_number",
    "company_name",
    "sector",
    "incorporated_on",
    "time_added_to_table",
    "pull_order",
    "has_target_resident_director",
    "matched_director_count",
    "matched_director_names",
    "matched_countries_of_residence",
]

LEAD_COLUMNS = [
    "company_number",
    "company_name",
    "sector",
    "incorporated_on",
    "has_target_resident_director",
    "matched_director_count",
    "matched_director_names",
    "matched_countries_of_residence",
    "added_by",
    "added_at",
]


def today_uk() -> date:
    return datetime.now().astimezone().date()


def now_uk_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def normalize_country(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def get_api_keys() -> List[str]:
    keys: List[str] = []
    list_style_keys = st.secrets.get("COMPANIES_HOUSE_API_KEYS", [])
    if list_style_keys:
        keys.extend([str(k).strip() for k in list_style_keys if str(k).strip()])

    for key_name in ["CH_API_KEY_1", "CH_API_KEY_2", "CH_API_KEY_3"]:
        value = st.secrets.get(key_name, "")
        if value:
            keys.append(str(value).strip())

    deduped_keys = []
    seen = set()
    for key in keys:
        if key and key not in seen:
            deduped_keys.append(key)
            seen.add(key)
    return deduped_keys


def auth_header(api_key: str) -> Dict[str, str]:
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "User-Agent": "streamlit-companies-house-sic-director-residence-app",
    }


def classify_sector(sic_codes: List[str]) -> Optional[str]:
    codes = {str(code) for code in (sic_codes or [])}
    matches = sorted(codes & TARGET_SIC_CODE_SET)
    if not matches:
        return None
    return matches[0]


@st.cache_resource(show_spinner=False)
def get_session() -> requests.Session:
    return requests.Session()


def fetch_with_rotation(
    url: str,
    params: Optional[Dict[str, str]],
    api_keys: List[str],
    timeout: int = 30,
) -> requests.Response:
    session = get_session()
    last_response = None

    for api_key in api_keys:
        response = session.get(url, headers=auth_header(api_key), params=params, timeout=timeout)
        if response.status_code in (401, 429):
            last_response = response
            continue
        response.raise_for_status()
        return response

    if last_response is not None:
        last_response.raise_for_status()

    raise RuntimeError("No valid Companies House API keys were available.")


def fetch_company_officers(company_number: str, api_keys: List[str]) -> List[dict]:
    url = f"https://api.company-information.service.gov.uk/company/{company_number}/officers"
    start_index = 0
    items_per_page = 100
    all_items: List[dict] = []

    while True:
        params = {
            "items_per_page": str(items_per_page),
            "start_index": str(start_index),
        }

        try:
            response = fetch_with_rotation(url, params, api_keys)
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 500):
                return []
            return []
        except requests.exceptions.RequestException:
            return []

        payload = response.json()
        items = payload.get("items", []) or []
        all_items.extend(items)

        total_results = int(payload.get("total_results", 0) or 0)
        start_index += items_per_page

        if start_index >= total_results or not items:
            break

    return all_items


def extract_target_resident_directors(company_number: str, api_keys: List[str]) -> Dict[str, str]:
    officers = fetch_company_officers(company_number, api_keys)

    matched_names: List[str] = []
    matched_countries: List[str] = []

    for officer in officers:
        officer_role = str(officer.get("officer_role", "")).strip().lower()
        resigned_on = officer.get("resigned_on")
        country_of_residence_raw = str(officer.get("country_of_residence", "")).strip()
        normalized_country = normalize_country(country_of_residence_raw)

        if officer_role != "director":
            continue
        if resigned_on:
            continue
        if not normalized_country:
            continue
        if normalized_country not in TARGET_RESIDENCE_COUNTRIES:
            continue

        name = str(officer.get("name", "")).strip()
        if name:
            matched_names.append(name)
        if country_of_residence_raw:
            matched_countries.append(country_of_residence_raw)

    unique_names = list(dict.fromkeys(matched_names))
    unique_countries = list(dict.fromkeys(matched_countries))

    return {
        "has_target_resident_director": "Yes" if unique_names or unique_countries else "No",
        "matched_director_count": str(len(unique_names)),
        "matched_director_names": " | ".join(unique_names),
        "matched_countries_of_residence": " | ".join(unique_countries),
    }


def fetch_companies_in_date_range(api_keys: List[str], from_date: str, to_date: str) -> pd.DataFrame:
    url = "https://api.company-information.service.gov.uk/advanced-search/companies"
    start_index = 0
    page_size = 100
    rows = []
    pull_counter = 0

    while True:
        params = {
            "incorporated_from": from_date,
            "incorporated_to": to_date,
            "sic_codes": ",".join(TARGET_SIC_CODES),
            "size": str(page_size),
            "start_index": str(start_index),
        }

        response = fetch_with_rotation(url, params, api_keys)
        payload = response.json()
        items = payload.get("items", []) or []

        for item in items:
            sic_codes = [str(code) for code in item.get("sic_codes", []) if code]
            sector = classify_sector(sic_codes)
            if not sector:
                continue

            company_number = str(item.get("company_number", "")).strip()
            if not company_number:
                continue

            try:
                director_match_info = extract_target_resident_directors(company_number, api_keys)
            except requests.exceptions.RequestException:
                director_match_info = {
                    "has_target_resident_director": "No",
                    "matched_director_count": "0",
                    "matched_director_names": "",
                    "matched_countries_of_residence": "",
                }

            rows.append({
                "company_number": company_number,
                "company_name": str(item.get("company_name", "")).strip(),
                "sector": sector,
                "incorporated_on": str(item.get("date_of_creation", "")).strip(),
                "time_added_to_table": now_uk_str(),
                "pull_order": pull_counter,
                "has_target_resident_director": director_match_info["has_target_resident_director"],
                "matched_director_count": director_match_info["matched_director_count"],
                "matched_director_names": director_match_info["matched_director_names"],
                "matched_countries_of_residence": director_match_info["matched_countries_of_residence"],
            })
            pull_counter += 1

        if len(items) < page_size:
            break

        start_index += page_size

    if not rows:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    df = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    return (
        df.sort_values("pull_order", ascending=False, kind="stable")
        .drop_duplicates(subset=["company_number"], keep="first")
        .reset_index(drop=True)
    )


def get_store_paths(from_date: str, to_date: str) -> Tuple[Path, Path]:
    suffix = f"{from_date}_to_{to_date}"
    snapshot_path = DATA_DIR / f"companies_{suffix}.csv"
    seen_path = DATA_DIR / f"seen_{suffix}.csv"
    return snapshot_path, seen_path


def lead_file_path(person: str, from_date: str, to_date: str) -> Path:
    suffix = f"{from_date}_to_{to_date}"
    return LEADS_DIR / f"{person.strip().lower()}_leads_{suffix}.csv"


@st.cache_data(show_spinner=False)
def load_results_csv(path_str: str, mtime: float) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return pd.read_csv(path, dtype={c: "string" for c in RESULT_COLUMNS}).fillna("")


@st.cache_data(show_spinner=False)
def load_leads_csv(path_str: str, mtime: float) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame(columns=LEAD_COLUMNS)
    return pd.read_csv(path, dtype={c: "string" for c in LEAD_COLUMNS}).fillna("")


def load_results(path: Path) -> pd.DataFrame:
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return load_results_csv(str(path), mtime)


def load_leads(person: str, from_date: str, to_date: str) -> pd.DataFrame:
    path = lead_file_path(person, from_date, to_date)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return load_leads_csv(str(path), mtime)


def identify_new_rows(current_df: pd.DataFrame, seen_df: pd.DataFrame) -> pd.DataFrame:
    if current_df.empty:
        return current_df.copy()
    if seen_df.empty or "company_number" not in seen_df.columns:
        return current_df.copy()

    unseen = current_df[~current_df["company_number"].isin(seen_df["company_number"].astype(str))].copy()
    return unseen.reset_index(drop=True)


def save_state(current_df: pd.DataFrame, snapshot_path: Path, seen_path: Path) -> None:
    current_df.to_csv(snapshot_path, index=False)
    current_df.to_csv(seen_path, index=False)


def add_company_to_leads(
    person: str,
    from_date: str,
    to_date: str,
    row: pd.Series,
    existing_leads: pd.DataFrame,
) -> bool:
    path = lead_file_path(person, from_date, to_date)
    company_number = str(row.get("company_number", "")).strip()
    if not company_number:
        return False

    if not existing_leads.empty and company_number in set(existing_leads["company_number"].astype(str)):
        return False

    new_row = pd.DataFrame([{
        "company_number": company_number,
        "company_name": str(row.get("company_name", "")).strip(),
        "sector": str(row.get("sector", "")).strip(),
        "incorporated_on": str(row.get("incorporated_on", "")).strip(),
        "has_target_resident_director": str(row.get("has_target_resident_director", "")).strip(),
        "matched_director_count": str(row.get("matched_director_count", "")).strip(),
        "matched_director_names": str(row.get("matched_director_names", "")).strip(),
        "matched_countries_of_residence": str(row.get("matched_countries_of_residence", "")).strip(),
        "added_by": person,
        "added_at": now_uk_str(),
    }], columns=LEAD_COLUMNS)

    if path.exists():
        new_row.to_csv(path, mode="a", index=False, header=False)
    else:
        new_row.to_csv(path, index=False)

    return True


@st.cache_data(show_spinner=False)
def convert_results_csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""

    export_df = df[[
        "company_name",
        "sector",
        "incorporated_on",
        "has_target_resident_director",
        "matched_director_count",
        "matched_director_names",
        "matched_countries_of_residence",
        "time_added_to_table",
    ]].rename(columns={
        "company_name": "Company Name",
        "sector": "Matched SIC Code",
        "incorporated_on": "Incorporated On",
        "has_target_resident_director": "Has Target Resident Director",
        "matched_director_count": "Matched Director Count",
        "matched_director_names": "Matched Director Names",
        "matched_countries_of_residence": "Matched Countries Of Residence",
        "time_added_to_table": "Time Added To Table",
    })
    return export_df.to_csv(index=False).encode("utf-8")


@st.cache_data(show_spinner=False)
def convert_leads_csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""

    export_df = df.rename(columns={
        "company_number": "Company Number",
        "company_name": "Company Name",
        "sector": "Matched SIC Code",
        "incorporated_on": "Incorporated On",
        "has_target_resident_director": "Has Target Resident Director",
        "matched_director_count": "Matched Director Count",
        "matched_director_names": "Matched Director Names",
        "matched_countries_of_residence": "Matched Countries Of Residence",
        "added_by": "Added By",
        "added_at": "Added At",
    })
    return export_df.to_csv(index=False).encode("utf-8")


def get_sorted_current_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    sort_df = df.copy()
    sort_df["matched_director_count_num"] = pd.to_numeric(sort_df["matched_director_count"], errors="coerce").fillna(0)

    sorted_df = (
        sort_df.sort_values(
            ["has_target_resident_director", "matched_director_count_num", "time_added_to_table", "pull_order"],
            ascending=[False, False, False, False],
            kind="stable",
        )
        .drop(columns=["matched_director_count_num"])
        .reset_index(drop=True)
    )
    return sorted_df


def merge_preserving_timestamps(fetched_df: pd.DataFrame, existing_df: pd.DataFrame) -> pd.DataFrame:
    if existing_df.empty:
        return fetched_df.copy()

    existing_lookup = existing_df.set_index("company_number")[["time_added_to_table", "pull_order"]]
    existing_numbers = set(existing_df["company_number"].astype(str))

    new_rows = fetched_df[~fetched_df["company_number"].astype(str).isin(existing_numbers)].copy()

    known_rows = fetched_df[fetched_df["company_number"].astype(str).isin(existing_numbers)].copy()
    known_rows["time_added_to_table"] = known_rows["company_number"].map(existing_lookup["time_added_to_table"])
    known_rows["pull_order"] = known_rows["company_number"].map(existing_lookup["pull_order"].astype(int))

    merged = pd.concat([new_rows, known_rows], ignore_index=True)
    return merged.drop_duplicates(subset=["company_number"], keep="first").reset_index(drop=True)


def render_quick_add(
    df: pd.DataFrame,
    person: str,
    from_date: str,
    to_date: str,
    existing_leads: pd.DataFrame,
) -> None:
    st.subheader(f"Quick add to {person}'s leads")
    if df.empty:
        st.info("No companies available to add.")
        return

    existing_numbers = set(existing_leads["company_number"].astype(str)) if not existing_leads.empty else set()

    for idx, (_, row) in enumerate(df.iterrows()):
        company_number = str(row.get("company_number", "")).strip()
        already_added = company_number in existing_numbers

        c1, c2, c3, c4, c5, c6 = st.columns([4, 1, 1.2, 2.2, 1.2, 0.9])
        c1.write(f"**{row['company_name']}**")
        c2.write(str(row["sector"]))
        c3.write(str(row["incorporated_on"]))
        c4.write(str(row["matched_countries_of_residence"]))
        c5.write(str(row["matched_director_count"]))

        if already_added:
            c6.caption("Added")
        else:
            if c6.button("Add", key=f"add_{person}_{company_number}_{idx}"):
                added = add_company_to_leads(person, from_date, to_date, row, existing_leads)
                if added:
                    st.rerun()


def main() -> None:
    st.title("Companies by SIC + Director Residence")
    st.caption(
        "Pull companies by target SIC codes across a requested incorporation date range, "
        "then flag those with at least one active director residing in a target country."
    )

    api_keys = get_api_keys()
    if not api_keys:
        st.error("Add COMPANIES_HOUSE_API_KEYS or CH_API_KEY_1/2/3 to your Streamlit secrets before running the app.")
        st.stop()

    st.sidebar.header("Controls")
    selected_user = st.sidebar.selectbox("Working as", TEAM_MEMBERS, index=0)

    default_to = today_uk()
    default_from = default_to - timedelta(days=7)

    from_date_value = st.sidebar.date_input("Incorporated from", value=default_from)
    to_date_value = st.sidebar.date_input("Incorporated to", value=default_to)

    if from_date_value > to_date_value:
        st.sidebar.error("'Incorporated from' must be on or before 'Incorporated to'.")
        st.stop()

    from_date = from_date_value.isoformat()
    to_date = to_date_value.isoformat()

    st.sidebar.markdown("**Target residence countries**")
    st.sidebar.caption(
        "Ireland, France, Poland, Germany, Spain, Portugal, Belgium, Austria, "
        "Netherlands, Croatia, Denmark, Sweden, Norway, Finland"
    )

    flagged_only = st.sidebar.checkbox("Show flagged companies only", value=True)
    refresh = st.sidebar.button("Refresh now", type="primary")

    snapshot_path, seen_path = get_store_paths(from_date, to_date)

    if refresh or not snapshot_path.exists():
        fetched_df = fetch_companies_in_date_range(api_keys, from_date, to_date)
        existing_df = load_results(snapshot_path)
        current_df = merge_preserving_timestamps(fetched_df, existing_df)
        seen_df = load_results(seen_path)
        new_df = identify_new_rows(current_df, seen_df)
        save_state(current_df, snapshot_path, seen_path)

        st.session_state["latest_df"] = current_df
        st.session_state["sorted_df"] = get_sorted_current_df(current_df)
        st.session_state["new_df"] = new_df
        st.session_state["last_refresh"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    else:
        current_df = load_results(snapshot_path)
        st.session_state.setdefault("latest_df", current_df)
        st.session_state.setdefault("sorted_df", get_sorted_current_df(current_df))
        st.session_state.setdefault("new_df", pd.DataFrame(columns=RESULT_COLUMNS))
        st.session_state.setdefault("last_refresh", "Not refreshed in this session")

    current_df = st.session_state.get("latest_df", pd.DataFrame(columns=RESULT_COLUMNS))
    sorted_df = st.session_state.get("sorted_df", pd.DataFrame(columns=RESULT_COLUMNS))

    if flagged_only and not sorted_df.empty:
        visible_df = sorted_df[sorted_df["has_target_resident_director"] == "Yes"].reset_index(drop=True)
    else:
        visible_df = sorted_df.copy()

    leads_df = load_leads(selected_user, from_date, to_date)

    total_flagged = 0
    if not current_df.empty and "has_target_resident_director" in current_df.columns:
        total_flagged = int((current_df["has_target_resident_director"] == "Yes").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total pulled", int(len(current_df)))
    c2.metric("Flagged companies", total_flagged)
    c3.metric(f"{selected_user}'s leads", int(len(leads_df)))
    c4.metric("Quick add rows", QUICK_ADD_DEFAULT)

    st.caption(
        f"Range: {from_date} to {to_date} | Working as {selected_user} | "
        f"Last refresh: {st.session_state.get('last_refresh', 'Unknown')}"
    )

    newest_df = visible_df.head(QUICK_ADD_DEFAULT).reset_index(drop=True) if not visible_df.empty else visible_df
    render_quick_add(newest_df, selected_user, from_date, to_date, leads_df)

    with st.expander(f"{selected_user}'s leads for selected range", expanded=False):
        if leads_df.empty:
            st.info(f"No leads saved yet for {selected_user}.")
        else:
            leads_display = leads_df.rename(columns={
                "company_number": "Company Number",
                "company_name": "Company Name",
                "sector": "Matched SIC Code",
                "incorporated_on": "Incorporated On",
                "has_target_resident_director": "Has Target Resident Director",
                "matched_director_count": "Matched Director Count",
                "matched_director_names": "Matched Director Names",
                "matched_countries_of_residence": "Matched Countries Of Residence",
                "added_by": "Added By",
                "added_at": "Added At",
            })
            st.dataframe(leads_display, use_container_width=True, hide_index=True)
            st.download_button(
                label=f"Download {selected_user}'s leads CSV",
                data=convert_leads_csv_bytes(leads_df),
                file_name=f"{selected_user.lower()}_leads_{from_date}_to_{to_date}.csv",
                mime="text/csv",
                key=f"download_{selected_user.lower()}_leads",
            )

    with st.expander("Results CSV", expanded=False):
        if not visible_df.empty:
            st.download_button(
                label="Download visible results as CSV",
                data=convert_results_csv_bytes(visible_df),
                file_name=f"companies_{from_date}_to_{to_date}.csv",
                mime="text/csv",
                key="download_results_csv",
            )
        else:
            st.info("No results available for the current filters.")

    with st.expander("Full table", expanded=False):
        if visible_df.empty:
            st.info("No companies to show.")
        else:
            preview_df = visible_df[[
                "company_name",
                "sector",
                "incorporated_on",
                "has_target_resident_director",
                "matched_director_count",
                "matched_director_names",
                "matched_countries_of_residence",
                "time_added_to_table",
            ]].rename(columns={
                "company_name": "Company Name",
                "sector": "Matched SIC Code",
                "incorporated_on": "Incorporated On",
                "has_target_resident_director": "Has Target Resident Director",
                "matched_director_count": "Matched Director Count",
                "matched_director_names": "Matched Director Names",
                "matched_countries_of_residence": "Matched Countries Of Residence",
                "time_added_to_table": "Time Added To Table",
            })
            st.dataframe(preview_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
