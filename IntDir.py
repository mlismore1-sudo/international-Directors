import base64
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="International Directors", layout="wide")

TARGET_DIRECTOR_COUNTRIES = {
    "france",
    "germany",
    "spain",
    "norway",
    "italy",
    "sweden",
    "netherlands",
    "belgium",
    "finland",
    "denmark",
    "poland",
    "portugal",
    "usa",
    "united states",
    "united states of america",
    "india",
    "hong kong",
}

COUNTRY_DISPLAY_ORDER = [
    ("France", {"france"}),
    ("Germany", {"germany"}),
    ("Spain", {"spain"}),
    ("Norway", {"norway"}),
    ("Italy", {"italy"}),
    ("Sweden", {"sweden"}),
    ("Netherlands", {"netherlands"}),
    ("Belgium", {"belgium"}),
    ("Finland", {"finland"}),
    ("Denmark", {"denmark"}),
    ("Poland", {"poland"}),
    ("Portugal", {"portugal"}),
    ("USA", {"usa", "united states", "united states of america"}),
    ("India", {"india"}),
    ("Hong Kong", {"hong kong"}),
]

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
    "time_added_to_table",
    "pull_order",
]

LEAD_COLUMNS = [
    "company_number",
    "company_name",
    "sector",
    "added_by",
    "added_at",
]

RATE_LIMIT_PER_KEY = 599
RATE_LIMIT_WINDOW_SECONDS = 300
REQUEST_PAUSE_SECONDS = 0.15

_key_request_log: Dict[str, Deque[float]] = {}


def today_uk_date() -> date:
    return datetime.now().astimezone().date()


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
        "User-Agent": "streamlit-international-directors-app",
    }


def normalize_country(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def classify_country_match(officer_countries: Set[str]) -> Optional[str]:
    for label, aliases in COUNTRY_DISPLAY_ORDER:
        if officer_countries & aliases:
            return label
    return None


@st.cache_resource(show_spinner=False)
def get_session() -> requests.Session:
    return requests.Session()


def _prune_key_log(api_key: str) -> None:
    now = time.time()
    if api_key not in _key_request_log:
        _key_request_log[api_key] = deque()

    while _key_request_log[api_key] and now - _key_request_log[api_key][0] >= RATE_LIMIT_WINDOW_SECONDS:
        _key_request_log[api_key].popleft()


def _can_use_key(api_key: str) -> bool:
    _prune_key_log(api_key)
    return len(_key_request_log[api_key]) < RATE_LIMIT_PER_KEY


def _record_key_use(api_key: str) -> None:
    _prune_key_log(api_key)
    _key_request_log[api_key].append(time.time())


def _seconds_until_key_available(api_key: str) -> int:
    _prune_key_log(api_key)
    if len(_key_request_log[api_key]) < RATE_LIMIT_PER_KEY:
        return 0
    oldest = _key_request_log[api_key][0]
    remaining = int(RATE_LIMIT_WINDOW_SECONDS - (time.time() - oldest)) + 1
    return max(1, remaining)


def fetch_with_rotation(
    url: str,
    params: Dict[str, str],
    api_keys: List[str],
    timeout: int = 30,
) -> requests.Response:
    session = get_session()
    last_error_message = None

    available_keys = [key for key in api_keys if _can_use_key(key)]

    if not available_keys:
        wait_times = [_seconds_until_key_available(key) for key in api_keys]
        min_wait = min(wait_times) if wait_times else RATE_LIMIT_WINDOW_SECONDS
        raise RuntimeError(
            f"All Companies House API keys are currently at the rate limit. "
            f"Please wait about {min_wait} seconds and try again."
        )

    for api_key in available_keys:
        response = session.get(
            url,
            headers=auth_header(api_key),
            params=params,
            timeout=timeout,
        )

        _record_key_use(api_key)
        time.sleep(REQUEST_PAUSE_SECONDS)

        if response.status_code == 429:
            last_error_message = (
                f"Companies House API key hit 429 rate limit for {url} "
                f"with params={params}."
            )
            continue

        if response.status_code == 401:
            last_error_message = "A Companies House API key returned 401 Unauthorized."
            continue

        if not response.ok:
            raise RuntimeError(
                f"Companies House API error {response.status_code} for {url} "
                f"with params={params}. Response: {response.text[:1000]}"
            )

        return response

    if last_error_message:
        wait_times = [_seconds_until_key_available(key) for key in api_keys]
        min_wait = min(wait_times) if wait_times else RATE_LIMIT_WINDOW_SECONDS
        raise RuntimeError(f"{last_error_message} Try again in about {min_wait} seconds.")

    raise RuntimeError("No valid Companies House API keys were available.")


def fetch_company_officers(company_number: str, api_keys: List[str]) -> List[dict]:
    url = f"https://api.company-information.service.gov.uk/company/{company_number}/officers"
    params = {"items_per_page": "35"}
    response = fetch_with_rotation(url, params, api_keys)
    payload = response.json()
    return payload.get("items", []) or []


def get_matching_director_countries(company_number: str, api_keys: List[str]) -> Set[str]:
    try:
        officers = fetch_company_officers(company_number, api_keys)
    except Exception:
        return set()

    matches: Set[str] = set()

    for officer in officers:
        officer_role = str(officer.get("officer_role", "")).strip().lower()
        if officer_role not in {"director", "llp-member"}:
            continue

        if str(officer.get("resigned_on", "")).strip():
            continue

        country = normalize_country(officer.get("country_of_residence", ""))
        if country and country in TARGET_DIRECTOR_COUNTRIES:
            matches.add(country)

    return matches


def fetch_companies_for_date_range(
    api_keys: List[str],
    incorporated_from: str,
    incorporated_to: str,
) -> pd.DataFrame:
    url = "https://api.company-information.service.gov.uk/advanced-search/companies"
    start_index = 0
    page_size = 25
    rows = []
    pull_counter = 0

    while True:
        params = {
            "incorporated_from": incorporated_from,
            "incorporated_to": incorporated_to,
            "size": str(page_size),
            "start_index": str(start_index),
        }

        response = fetch_with_rotation(url, params, api_keys)
        payload = response.json()
        items = payload.get("items", []) or []

        for item in items:
            company_number = str(item.get("company_number", "")).strip()
            company_name = str(item.get("company_name", "")).strip()

            if not company_number:
                continue

            matching_countries = get_matching_director_countries(company_number, api_keys)
            sector = classify_country_match(matching_countries)

            if not sector:
                continue

            rows.append(
                {
                    "company_number": company_number,
                    "company_name": company_name,
                    "sector": sector,
                    "time_added_to_table": now_uk_str(),
                    "pull_order": pull_counter,
                }
            )
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


def get_store_paths(incorporated_from: str, incorporated_to: str) -> Tuple[Path, Path]:
    suffix = f"{incorporated_from}_to_{incorporated_to}"
    snapshot_path = DATA_DIR / f"companies_{suffix}.csv"
    seen_path = DATA_DIR / f"seen_{suffix}.csv"
    return snapshot_path, seen_path


def lead_file_path(person: str, incorporated_from: str, incorporated_to: str) -> Path:
    suffix = f"{incorporated_from}_to_{incorporated_to}"
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


def load_leads(person: str, incorporated_from: str, incorporated_to: str) -> pd.DataFrame:
    path = lead_file_path(person, incorporated_from, incorporated_to)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return load_leads_csv(str(path), mtime)


def identify_new_rows(current_df: pd.DataFrame, seen_df: pd.DataFrame) -> pd.DataFrame:
    if current_df.empty:
        return current_df.copy()

    if seen_df.empty or "company_number" not in seen_df.columns:
        return current_df.copy()

    unseen = current_df[
        ~current_df["company_number"].isin(seen_df["company_number"].astype(str))
    ].copy()

    return unseen.reset_index(drop=True)


def save_state(current_df: pd.DataFrame, snapshot_path: Path, seen_path: Path) -> None:
    current_df.to_csv(snapshot_path, index=False)
    current_df.to_csv(seen_path, index=False)


def add_company_to_leads(
    person: str,
    incorporated_from: str,
    incorporated_to: str,
    row: pd.Series,
    existing_leads: pd.DataFrame,
) -> bool:
    path = lead_file_path(person, incorporated_from, incorporated_to)
    company_number = str(row.get("company_number", "")).strip()

    if not company_number:
        return False

    if not existing_leads.empty and company_number in set(existing_leads["company_number"].astype(str)):
        return False

    new_row = pd.DataFrame(
        [
            {
                "company_number": company_number,
                "company_name": str(row.get("company_name", "")).strip(),
                "sector": str(row.get("sector", "")).strip(),
                "added_by": person,
                "added_at": now_uk_str(),
            }
        ],
        columns=LEAD_COLUMNS,
    )

    if path.exists():
        new_row.to_csv(path, mode="a", index=False, header=False)
    else:
        new_row.to_csv(path, index=False)

    return True


@st.cache_data(show_spinner=False)
def convert_results_csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""

    export_df = df[["company_name", "sector", "time_added_to_table"]].rename(
        columns={
            "company_name": "Company Name",
            "sector": "Matched Director Country",
            "time_added_to_table": "Time Added To Table",
        }
    )
    return export_df.to_csv(index=False).encode("utf-8")


@st.cache_data(show_spinner=False)
def convert_leads_csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""

    export_df = df.rename(
        columns={
            "company_number": "Company Number",
            "company_name": "Company Name",
            "sector": "Matched Director Country",
            "added_by": "Added By",
            "added_at": "Added At",
        }
    )
    return export_df.to_csv(index=False).encode("utf-8")


def get_sorted_current_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    return (
        df.sort_values(
            ["time_added_to_table", "pull_order"],
            ascending=[False, False],
            kind="stable",
        )
        .reset_index(drop=True)
    )


def merge_preserving_timestamps(
    fetched_df: pd.DataFrame,
    existing_df: pd.DataFrame,
) -> pd.DataFrame:
    if existing_df.empty:
        return fetched_df.copy()

    existing_lookup = existing_df.set_index("company_number")[["time_added_to_table", "pull_order"]]
    existing_numbers = set(existing_df["company_number"].astype(str))

    new_rows = fetched_df[
        ~fetched_df["company_number"].astype(str).isin(existing_numbers)
    ].copy()

    known_rows = fetched_df[
        fetched_df["company_number"].astype(str).isin(existing_numbers)
    ].copy()

    known_rows["time_added_to_table"] = known_rows["company_number"].map(
        existing_lookup["time_added_to_table"]
    )
    known_rows["pull_order"] = known_rows["company_number"].map(
        existing_lookup["pull_order"].astype(int)
    )

    merged = pd.concat([new_rows, known_rows], ignore_index=True)
    return merged.drop_duplicates(subset=["company_number"], keep="first").reset_index(drop=True)


def render_quick_add(
    df: pd.DataFrame,
    person: str,
    incorporated_from: str,
    incorporated_to: str,
    existing_leads: pd.DataFrame,
) -> None:
    st.subheader(f"Quick add to {person}'s leads")

    if df.empty:
        st.info("No companies available to add.")
        return

    existing_numbers = (
        set(existing_leads["company_number"].astype(str))
        if not existing_leads.empty
        else set()
    )

    for idx, (_, row) in enumerate(df.iterrows()):
        company_number = str(row.get("company_number", "")).strip()
        already_added = company_number in existing_numbers

        c1, c2, c3, c4 = st.columns([5, 1.6, 2, 0.9])
        c1.write(f"**{row['company_name']}**")
        c2.write(str(row["sector"]))
        c3.write(str(row["time_added_to_table"]))

        if already_added:
            c4.caption("Added")
        else:
            if c4.button("Add", key=f"add_{person}_{company_number}_{idx}"):
                added = add_company_to_leads(
                    person,
                    incorporated_from,
                    incorporated_to,
                    row,
                    existing_leads,
                )
                if added:
                    st.rerun()


def main() -> None:
    st.title("International Directors")
    st.caption("Filter companies by incorporation date and director country of residence.")

    api_keys = get_api_keys()
    if not api_keys:
        st.error("Add COMPANIES_HOUSE_API_KEYS or CH_API_KEY_1/2/3 to your Streamlit secrets before running the app.")
        st.stop()

    st.sidebar.header("Controls")
    selected_user = st.sidebar.selectbox("Working as", TEAM_MEMBERS, index=0)

    default_date = today_uk_date()
    start_date = st.sidebar.date_input("Incorporated from", value=default_date)
    end_date = st.sidebar.date_input("Incorporated to", value=default_date)

    if start_date > end_date:
        st.sidebar.error("'Incorporated from' must be on or before 'Incorporated to'.")
        st.stop()

    incorporated_from = start_date.isoformat()
    incorporated_to = end_date.isoformat()

    refresh = st.sidebar.button("Refresh now", type="primary")

    st.sidebar.caption(
        "Countries: France, Germany, Spain, Norway, Italy, Sweden, Netherlands, Belgium, "
        "Finland, Denmark, Poland, Portugal, USA, India, Hong Kong"
    )

    snapshot_path, seen_path = get_store_paths(incorporated_from, incorporated_to)

    if refresh or not snapshot_path.exists():
        try:
            fetched_df = fetch_companies_for_date_range(
                api_keys,
                incorporated_from,
                incorporated_to,
            )
        except Exception as e:
            st.error(str(e))
            st.stop()

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
        st.session_state["latest_df"] = current_df
        st.session_state["sorted_df"] = get_sorted_current_df(current_df)
        st.session_state["new_df"] = pd.DataFrame(columns=RESULT_COLUMNS)
        st.session_state["last_refresh"] = st.session_state.get(
            "last_refresh",
            "Not refreshed in this session",
        )

    current_df = st.session_state.get("latest_df", pd.DataFrame(columns=RESULT_COLUMNS))
    sorted_df = st.session_state.get("sorted_df", pd.DataFrame(columns=RESULT_COLUMNS))
    leads_df = load_leads(selected_user, incorporated_from, incorporated_to)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total matched", int(len(current_df)))
    c2.metric(f"{selected_user}'s leads", int(len(leads_df)))
    c3.metric("Quick add rows", QUICK_ADD_DEFAULT)

    st.caption(
        f"Working as {selected_user} | Incorporation range: {incorporated_from} to {incorporated_to} | "
        f"Last refresh: {st.session_state.get('last_refresh', 'Unknown')}"
    )

    newest_df = (
        sorted_df.head(QUICK_ADD_DEFAULT).reset_index(drop=True)
        if not sorted_df.empty
        else sorted_df
    )

    render_quick_add(
        newest_df,
        selected_user,
        incorporated_from,
        incorporated_to,
        leads_df,
    )

    with st.expander(f"{selected_user}'s leads", expanded=False):
        if leads_df.empty:
            st.info(f"No leads saved yet for {selected_user}.")
        else:
            leads_display = leads_df.rename(
                columns={
                    "company_number": "Company Number",
                    "company_name": "Company Name",
                    "sector": "Matched Director Country",
                    "added_by": "Added By",
                    "added_at": "Added At",
                }
            )
            st.dataframe(leads_display, use_container_width=True, hide_index=True)
            st.download_button(
                label=f"Download {selected_user}'s leads CSV",
                data=convert_leads_csv_bytes(leads_df),
                file_name=f"{selected_user.lower()}_leads_{incorporated_from}_to_{incorporated_to}.csv",
                mime="text/csv",
                key=f"download_{selected_user.lower()}_leads",
            )

    with st.expander("Results CSV", expanded=False):
        if not current_df.empty:
            st.download_button(
                label="Download results as CSV",
                data=convert_results_csv_bytes(current_df),
                file_name=f"companies_incorporated_{incorporated_from}_to_{incorporated_to}.csv",
                mime="text/csv",
                key="download_results_csv",
            )
        else:
            st.info("No results available yet.")

    with st.expander("Full table", expanded=False):
        if current_df.empty:
            st.info("No companies to show yet.")
        else:
            preview_df = sorted_df[
                ["company_name", "sector", "time_added_to_table"]
            ].rename(
                columns={
                    "company_name": "Company Name",
                    "sector": "Matched Director Country",
                    "time_added_to_table": "Time Added To Table",
                }
            )
            st.dataframe(preview_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
