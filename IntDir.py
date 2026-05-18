import base64
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title="Companies Incorporated Today", layout="wide")

TARGET_SIC_CODES = tuple(sorted({
    "62012", "62020", "63120", "47910", "46190", "46499",
    "70229", "73110", "74909", "68209", "64209", "68100",
    "32990", "10890", "86900", "93130", "96040", "82990",
    "72110",
}))
TARGET_SIC_CODE_SET = set(TARGET_SIC_CODES)

TECH_BIOTECH_CODES = {"62012", "72110"}

FLAGGED_COUNTRY_ALIASES = {
    "france": "France",
    "germany": "Germany",
    "spain": "Spain",
    "portugal": "Portugal",
    "usa": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "united states": "United States",
    "us": "United States",
    "singapore": "Singapore",
    "hong kong": "Hong Kong",
    "finland": "Finland",
    "iceland": "Iceland",
    "norway": "Norway",
    "sweden": "Sweden",
    "denmark": "Denmark",
    "belgium": "Belgium",
    "netherlands": "Netherlands",
    "poland": "Poland",
    "italy": "Italy",
    "austria": "Austria",
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
    "director_countries_flagged",
    "matched_director_countries",
    "time_added_to_table",
    "pull_order",
]

LEAD_COLUMNS = [
    "company_number",
    "company_name",
    "sector",
    "director_countries_flagged",
    "matched_director_countries",
    "added_by",
    "added_at",
]


def today_uk_str() -> str:
    return datetime.now().astimezone().date().isoformat()


def now_uk_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def get_api_keys() -> List[str]:
    keys: List[str] = []

    list_style_keys = st.secrets.get("COMPANIES_HOUSE_API_KEYS", [])
    if list_style_keys:
        keys.extend([str(k).strip() for k in list_style_keys if str(k).strip()])

    for key_name in ["CH_API_KEY_1", "CH_API_KEY_2", "CH_API_KEY_3"]:
        value = st.secrets.get(key_name, "")
        if value:
            keys.append(str(value).strip())

    deduped = []
    seen = set()
    for key in keys:
        if key and key not in seen:
            deduped.append(key)
            seen.add(key)
    return deduped


def auth_header(api_key: str) -> Dict[str, str]:
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "User-Agent": "streamlit-companies-house-today-app",
    }


def classify_sector(sic_codes: List[str]) -> Optional[str]:
    codes = {str(code).strip() for code in (sic_codes or []) if str(code).strip()}
    matched_codes = sorted(codes & TARGET_SIC_CODE_SET)
    return ", ".join(matched_codes) if matched_codes else None


def parse_sector_codes(sector_value: str) -> set[str]:
    if not sector_value:
        return set()
    return {code.strip() for code in str(sector_value).split(",") if code.strip()}


def normalize_country(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = " ".join(str(value).strip().lower().split())
    return FLAGGED_COUNTRY_ALIASES.get(cleaned, "")


@st.cache_resource(show_spinner=False)
def get_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=50,
        pool_maxsize=50,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_with_rotation(
    url: str,
    params: Dict[str, str],
    api_keys: List[str],
    timeout: Tuple[float, float] = (3.05, 20),
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


@st.cache_data(ttl=900, show_spinner=False)
def fetch_companies_incorporated_today(api_keys_tuple: tuple[str, ...], run_date: str) -> pd.DataFrame:
    api_keys = list(api_keys_tuple)
    url = "https://api.company-information.service.gov.uk/advanced-search/companies"
    start_index = 0
    page_size = 5000
    rows = []
    pull_counter = 0
    timestamp = now_uk_str()

    while True:
        params = {
            "incorporated_from": run_date,
            "incorporated_to": run_date,
            "sic_codes": ",".join(TARGET_SIC_CODES),
            "size": str(page_size),
            "start_index": str(start_index),
        }

        response = fetch_with_rotation(url, params, api_keys)
        payload = response.json()
        items = payload.get("items", []) or []

        if not items:
            break

        for item in items:
            sector = classify_sector(item.get("sic_codes", []) or [])
            if not sector:
                continue

            rows.append({
                "company_number": str(item.get("company_number", "")).strip(),
                "company_name": str(item.get("company_name", "")).strip(),
                "sector": sector,
                "director_countries_flagged": "No",
                "matched_director_countries": "",
                "time_added_to_table": timestamp,
                "pull_order": pull_counter,
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


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_director_country_flags_cached(company_number: str, api_keys_tuple: tuple[str, ...]) -> Tuple[str, str]:
    api_keys = list(api_keys_tuple)
    url = f"https://api.company-information.service.gov.uk/company/{company_number}/officers"
    start_index = 0
    items_per_page = 100
    matched_countries = set()

    while True:
        params = {
            "items_per_page": str(items_per_page),
            "start_index": str(start_index),
        }

        response = fetch_with_rotation(url, params, api_keys)
        payload = response.json()
        items = payload.get("items", []) or []

        for officer in items:
            if str(officer.get("officer_role", "")).strip().lower() != "director":
                continue

            normalized_country = normalize_country(officer.get("country_of_residence", ""))
            if normalized_country:
                matched_countries.add(normalized_country)

        total_results = int(payload.get("total_results", len(items)))
        start_index += len(items)

        if not items or start_index >= total_results:
            break

    if matched_countries:
        return "Yes", ", ".join(sorted(matched_countries))
    return "No", ""


def get_store_paths(run_date: str) -> Tuple[Path, Path]:
    snapshot_path = DATA_DIR / f"companies_{run_date}.csv"
    seen_path = DATA_DIR / f"seen_{run_date}.csv"
    return snapshot_path, seen_path


def lead_file_path(person: str, run_date: str) -> Path:
    return LEADS_DIR / f"{person.strip().lower()}_leads_{run_date}.csv"


@st.cache_data(show_spinner=False)
def load_results_csv(path_str: str, mtime: float) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame(columns=RESULT_COLUMNS)

    df = pd.read_csv(path, dtype="string").fillna("")
    for col in RESULT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[RESULT_COLUMNS]


@st.cache_data(show_spinner=False)
def load_leads_csv(path_str: str, mtime: float) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame(columns=LEAD_COLUMNS)

    df = pd.read_csv(path, dtype="string").fillna("")
    for col in LEAD_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[LEAD_COLUMNS]


def load_results(path: Path) -> pd.DataFrame:
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return load_results_csv(str(path), mtime)


def load_leads(person: str, run_date: str) -> pd.DataFrame:
    path = lead_file_path(person, run_date)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return load_leads_csv(str(path), mtime)


def identify_new_rows(current_df: pd.DataFrame, seen_df: pd.DataFrame) -> pd.DataFrame:
    if current_df.empty:
        return current_df.copy()
    if seen_df.empty or "company_number" not in seen_df.columns:
        return current_df.copy()

    unseen = current_df[~current_df["company_number"].astype(str).isin(seen_df["company_number"].astype(str))].copy()
    return unseen.reset_index(drop=True)


def save_state(current_df: pd.DataFrame, snapshot_path: Path, seen_path: Path) -> None:
    save_df = current_df.copy()

    if "is_tech_biotech" in save_df.columns:
        save_df = save_df.drop(columns=["is_tech_biotech"])
    if "tech_biotech_international_match" in save_df.columns:
        save_df = save_df.drop(columns=["tech_biotech_international_match"])

    save_df.to_csv(snapshot_path, index=False)
    save_df.to_csv(seen_path, index=False)


def add_company_to_leads(person: str, run_date: str, row: pd.Series, existing_leads: pd.DataFrame) -> bool:
    path = lead_file_path(person, run_date)
    company_number = str(row.get("company_number", "")).strip()
    if not company_number:
        return False

    existing_numbers = set(existing_leads["company_number"].astype(str)) if not existing_leads.empty else set()
    if company_number in existing_numbers:
        return False

    new_row = pd.DataFrame([{
        "company_number": company_number,
        "company_name": str(row.get("company_name", "")).strip(),
        "sector": str(row.get("sector", "")).strip(),
        "director_countries_flagged": str(row.get("director_countries_flagged", "No")).strip(),
        "matched_director_countries": str(row.get("matched_director_countries", "")).strip(),
        "added_by": person,
        "added_at": now_uk_str(),
    }], columns=LEAD_COLUMNS)

    if path.exists():
        new_row.to_csv(path, mode="a", index=False, header=False)
    else:
        new_row.to_csv(path, index=False)

    load_leads_csv.clear()
    return True


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if df.empty:
        df["is_tech_biotech"] = pd.Series(dtype="bool")
        df["tech_biotech_international_match"] = pd.Series(dtype="bool")
        return df

    df["sector"] = df["sector"].fillna("").astype(str)
    df["matched_director_countries"] = df["matched_director_countries"].fillna("").astype(str)

    df["is_tech_biotech"] = df["sector"].str.split(",").apply(
        lambda parts: bool({p.strip() for p in parts if p.strip()} & TECH_BIOTECH_CODES)
    )
    df["tech_biotech_international_match"] = (
        df["is_tech_biotech"] &
        df["matched_director_countries"].str.strip().ne("")
    )

    return df


def split_result_tables(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        empty_df = pd.DataFrame(columns=df.columns)
        return empty_df, empty_df

    tech_biotech_df = df[df["is_tech_biotech"]].reset_index(drop=True)
    matched_country_directors_df = df[~df["is_tech_biotech"]].reset_index(drop=True)
    return tech_biotech_df, matched_country_directors_df


def get_sorted_current_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    sortable = df.copy()
    sortable["pull_order"] = pd.to_numeric(sortable["pull_order"], errors="coerce").fillna(-1).astype(int)

    return (
        sortable.sort_values(
            ["time_added_to_table", "pull_order"],
            ascending=[False, False],
            kind="stable",
        )
        .reset_index(drop=True)
    )


def screen_only_new_companies(
    fetched_df: pd.DataFrame,
    existing_df: pd.DataFrame,
    api_keys: List[str],
) -> pd.DataFrame:
    if fetched_df.empty:
        return fetched_df.copy()

    screened_df = fetched_df.copy()
    screened_df["company_number"] = screened_df["company_number"].astype(str).str.strip()

    if existing_df.empty or "company_number" not in existing_df.columns:
        existing_lookup = pd.DataFrame(columns=[
            "director_countries_flagged",
            "matched_director_countries",
            "time_added_to_table",
            "pull_order",
        ])
        existing_numbers = set()
    else:
        tmp = existing_df.copy()
        tmp["company_number"] = tmp["company_number"].astype(str).str.strip()
        tmp["pull_order"] = pd.to_numeric(tmp["pull_order"], errors="coerce").fillna(-1).astype(int)
        tmp = tmp.drop_duplicates(subset=["company_number"], keep="first")
        existing_lookup = tmp.set_index("company_number")[
            ["director_countries_flagged", "matched_director_countries", "time_added_to_table", "pull_order"]
        ]
        existing_numbers = set(existing_lookup.index)

    known_mask = screened_df["company_number"].isin(existing_numbers)

    if known_mask.any():
        screened_df.loc[known_mask, "director_countries_flagged"] = (
            screened_df.loc[known_mask, "company_number"].map(existing_lookup["director_countries_flagged"]).fillna("No")
        )
        screened_df.loc[known_mask, "matched_director_countries"] = (
            screened_df.loc[known_mask, "company_number"].map(existing_lookup["matched_director_countries"]).fillna("")
        )
        screened_df.loc[known_mask, "time_added_to_table"] = (
            screened_df.loc[known_mask, "company_number"].map(existing_lookup["time_added_to_table"]).fillna(screened_df["time_added_to_table"])
        )
        screened_df.loc[known_mask, "pull_order"] = (
            screened_df.loc[known_mask, "company_number"].map(existing_lookup["pull_order"]).fillna(screened_df["pull_order"])
        )

    new_mask = ~known_mask
    if new_mask.any():
        new_company_numbers = screened_df.loc[new_mask, "company_number"].tolist()
        flags_lookup = {
            company_number: fetch_director_country_flags_cached(company_number, tuple(api_keys))
            for company_number in new_company_numbers
        }

        screened_df.loc[new_mask, "director_countries_flagged"] = (
            screened_df.loc[new_mask, "company_number"].map(lambda cn: flags_lookup[cn][0])
        )
        screened_df.loc[new_mask, "matched_director_countries"] = (
            screened_df.loc[new_mask, "company_number"].map(lambda cn: flags_lookup[cn][1])
        )

    screened_df["pull_order"] = pd.to_numeric(screened_df["pull_order"], errors="coerce").fillna(-1).astype(int)

    return (
        screened_df
        .drop_duplicates(subset=["company_number"], keep="first")
        .reset_index(drop=True)
    )


def render_quick_add(df: pd.DataFrame, person: str, run_date: str, existing_leads: pd.DataFrame) -> None:
    st.subheader(f"Quick add to {person}'s leads")

    if df.empty:
        st.info("No companies available to add.")
        return

    existing_numbers = set(existing_leads["company_number"].astype(str)) if not existing_leads.empty else set()

    for idx, row in enumerate(df.itertuples(index=False)):
        company_number = str(row.company_number).strip()
        already_added = company_number in existing_numbers

        c1, c2, c3, c4, c5 = st.columns([4.5, 1.2, 2.2, 2.2, 0.9])
        c1.write(f"**{row.company_name}**")
        c2.write(str(row.sector))
        c3.write(str(row.matched_director_countries).strip() if str(row.matched_director_countries).strip() else "-")
        c4.write(str(row.time_added_to_table))

        if already_added:
            c5.caption("Added")
        else:
            if c5.button("Add", key=f"add_{person}_{company_number}_{idx}"):
                added = add_company_to_leads(
                    person,
                    run_date,
                    pd.Series({
                        "company_number": row.company_number,
                        "company_name": row.company_name,
                        "sector": row.sector,
                        "director_countries_flagged": row.director_countries_flagged,
                        "matched_director_countries": row.matched_director_countries,
                    }),
                    existing_leads,
                )
                if added:
                    st.rerun()


def main() -> None:
    st.title("Companies Incorporated Today")
    st.caption(f"Filtered to SIC codes: {', '.join(TARGET_SIC_CODES)}")

    api_keys = get_api_keys()
    if not api_keys:
        st.error("Add COMPANIES_HOUSE_API_KEYS or CH_API_KEY_1/2/3 to your Streamlit secrets before running the app.")
        st.stop()

    run_date = today_uk_str()
    snapshot_path, seen_path = get_store_paths(run_date)

    st.sidebar.header("Controls")
    selected_user = st.sidebar.selectbox("Working as", TEAM_MEMBERS, index=0)
    show_flagged_only = st.sidebar.checkbox("Show only flagged director countries", value=True)
    refresh = st.sidebar.button("Refresh now", type="primary")

    if refresh or not snapshot_path.exists():
        with st.spinner("Refreshing Companies House data..."):
            fetched_df = fetch_companies_incorporated_today(tuple(api_keys), run_date)
            existing_df = load_results(snapshot_path)

            screened_df = screen_only_new_companies(fetched_df, existing_df, api_keys)
            screened_df = add_derived_columns(screened_df)

            seen_df = load_results(seen_path)
            new_df = identify_new_rows(screened_df, seen_df)

            save_state(screened_df, snapshot_path, seen_path)

            st.session_state["latest_df"] = screened_df
            st.session_state["sorted_df"] = get_sorted_current_df(screened_df)
            st.session_state["new_df"] = new_df
            st.session_state["last_refresh"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    else:
        current_df = load_results(snapshot_path)
        current_df = add_derived_columns(current_df)

        st.session_state.setdefault("latest_df", current_df)
        st.session_state.setdefault("sorted_df", get_sorted_current_df(current_df))
        st.session_state.setdefault("new_df", pd.DataFrame(columns=RESULT_COLUMNS))
        st.session_state.setdefault("last_refresh", "Not refreshed in this session")

    current_df = st.session_state.get("latest_df", pd.DataFrame(columns=RESULT_COLUMNS))
    sorted_df = st.session_state.get("sorted_df", pd.DataFrame(columns=RESULT_COLUMNS))
    leads_df = load_leads(selected_user, run_date)

    tech_biotech_df, matched_country_directors_df = split_result_tables(sorted_df)

    if show_flagged_only and not matched_country_directors_df.empty:
        matched_country_directors_df = matched_country_directors_df[
            matched_country_directors_df["director_countries_flagged"].astype(str).str.lower() == "yes"
        ].reset_index(drop=True)

    total_pulled = int(len(current_df))
    total_flagged = int(
        (current_df["director_countries_flagged"].astype(str).str.lower() == "yes").sum()
    ) if not current_df.empty else 0
    total_leads = int(len(leads_df))

    c1, c2, c3 = st.columns(3)
    c1.metric("Total pulled today", total_pulled)
    c2.metric("Flagged by director country", total_flagged)
    c3.metric(f"{selected_user}'s leads today", total_leads)

    st.caption(f"Working as {selected_user} | Last refresh: {st.session_state.get('last_refresh', 'Unknown')}")

    newest_df = matched_country_directors_df.head(QUICK_ADD_DEFAULT).reset_index(drop=True)
    render_quick_add(newest_df, selected_user, run_date, leads_df)

    with st.expander("Tech & Biotech Leads", expanded=True):
        if tech_biotech_df.empty:
            st.info("No tech or biotech leads to show yet.")
        else:
            tech_biotech_display = tech_biotech_df[[
                "company_name",
                "sector",
                "director_countries_flagged",
                "matched_director_countries",
                "tech_biotech_international_match",
                "time_added_to_table",
            ]].rename(columns={
                "company_name": "Company Name",
                "sector": "SIC Code(s)",
                "director_countries_flagged": "Director Countries Flagged",
                "matched_director_countries": "Matched Director Countries",
                "tech_biotech_international_match": "Tech/Biotech International Match",
                "time_added_to_table": "Time Added To Table",
            })
            st.dataframe(tech_biotech_display, use_container_width=True, hide_index=True)

    with st.expander("Matched Country Directors", expanded=False):
        if matched_country_directors_df.empty:
            st.info("No matched country director companies to show yet.")
        else:
            matched_display = matched_country_directors_df[[
                "company_name",
                "sector",
                "director_countries_flagged",
                "matched_director_countries",
                "time_added_to_table",
            ]].rename(columns={
                "company_name": "Company Name",
                "sector": "SIC Code(s)",
                "director_countries_flagged": "Director Countries Flagged",
                "matched_director_countries": "Matched Director Countries",
                "time_added_to_table": "Time Added To Table",
            })
            st.dataframe(matched_display, use_container_width=True, hide_index=True)

    with st.expander("Today's results CSV", expanded=False):
        if current_df.empty:
            st.info("No results available yet.")
        else:
            if not tech_biotech_df.empty:
                st.download_button(
                    label="Download tech & biotech leads CSV",
                    data=tech_biotech_display.to_csv(index=False).encode("utf-8"),
                    file_name=f"tech_biotech_leads_{run_date}.csv",
                    mime="text/csv",
                    key="download_tech_biotech_csv",
                )

            if not matched_country_directors_df.empty:
                st.download_button(
                    label="Download matched country directors CSV",
                    data=matched_display.to_csv(index=False).encode("utf-8"),
                    file_name=f"matched_country_directors_{run_date}.csv",
                    mime="text/csv",
                    key="download_matched_country_directors_csv",
                )


if __name__ == "__main__":
    main()
