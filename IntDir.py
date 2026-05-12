import base64
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="International Directors Queue", layout="wide")

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

TEAM_MEMBERS = ["Brad", "James"]

DISCOVERY_PAGE_SIZE = 25
DISCOVERY_PAGES_PER_RUN = 2
PROCESS_BATCH_SIZE_DEFAULT = 15
REQUEST_PAUSE_SECONDS = 0.12

RATE_LIMIT_PER_KEY = 599
RATE_LIMIT_WINDOW_SECONDS = 300

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

LEADS_DIR = DATA_DIR / "leads"
LEADS_DIR.mkdir(exist_ok=True)

QUEUE_DIR = DATA_DIR / "queues"
QUEUE_DIR.mkdir(exist_ok=True)

DISCOVERY_DIR = DATA_DIR / "discovery"
DISCOVERY_DIR.mkdir(exist_ok=True)

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

QUEUE_COLUMNS = [
    "company_number",
    "company_name",
    "discovered_at",
    "last_seen_at",
    "status",
    "matched_country",
    "processed_at",
    "error_message",
    "priority_rank",
]

DISCOVERY_COLUMNS = [
    "company_number",
    "company_name",
    "discovered_at",
    "page_number",
]

_key_request_log: Dict[str, Deque[float]] = {}


def today_uk_date() -> date:
    return datetime.now().astimezone().date()


def now_uk_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def date_suffix(incorporated_from: str, incorporated_to: str) -> str:
    return f"{incorporated_from}_to_{incorporated_to}"


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
        "User-Agent": "streamlit-international-directors-queue-app",
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
            last_error_message = f"Rate limit hit for one API key on {url}."
            continue

        if response.status_code == 401:
            last_error_message = "A Companies House API key returned 401 Unauthorized."
            continue

        if response.status_code == 403:
            raise RuntimeError(
                f"Companies House API error 403 for {url} with params={params}. "
                f"Response: {response.text[:1000]}"
            )

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


def queue_file_path(incorporated_from: str, incorporated_to: str) -> Path:
    return QUEUE_DIR / f"queue_{date_suffix(incorporated_from, incorporated_to)}.csv"


def discovery_file_path(incorporated_from: str, incorporated_to: str) -> Path:
    return DISCOVERY_DIR / f"discovery_{date_suffix(incorporated_from, incorporated_to)}.csv"


def snapshot_path(incorporated_from: str, incorporated_to: str) -> Path:
    return DATA_DIR / f"matched_{date_suffix(incorporated_from, incorporated_to)}.csv"


def lead_file_path(person: str, incorporated_from: str, incorporated_to: str) -> Path:
    return LEADS_DIR / f"{person.strip().lower()}_leads_{date_suffix(incorporated_from, incorporated_to)}.csv"


@st.cache_data(show_spinner=False)
def load_csv_cached(path_str: str, mtime: float, columns: Tuple[str, ...]) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame(columns=list(columns))
    return pd.read_csv(path, dtype="string").fillna("")


def load_generic_csv(path: Path, columns: List[str]) -> pd.DataFrame:
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return load_csv_cached(str(path), mtime, tuple(columns))


def save_csv(df: pd.DataFrame, path: Path, columns: List[str]) -> None:
    out = df.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = ""
    out = out[columns]
    out.to_csv(path, index=False)


def fetch_discovery_pages(
    api_keys: List[str],
    incorporated_from: str,
    incorporated_to: str,
    pages_to_fetch: int,
) -> pd.DataFrame:
    url = "https://api.company-information.service.gov.uk/advanced-search/companies"
    rows = []

    for page_number in range(pages_to_fetch):
        start_index = page_number * DISCOVERY_PAGE_SIZE
        params = {
            "incorporated_from": incorporated_from,
            "incorporated_to": incorporated_to,
            "size": str(DISCOVERY_PAGE_SIZE),
            "start_index": str(start_index),
        }

        response = fetch_with_rotation(url, params, api_keys)
        payload = response.json()
        items = payload.get("items", []) or []

        discovered_at = now_uk_str()

        for item in items:
            company_number = str(item.get("company_number", "")).strip()
            company_name = str(item.get("company_name", "")).strip()
            if not company_number:
                continue

            rows.append(
                {
                    "company_number": company_number,
                    "company_name": company_name,
                    "discovered_at": discovered_at,
                    "page_number": str(page_number),
                }
            )

        if len(items) < DISCOVERY_PAGE_SIZE:
            break

    if not rows:
        return pd.DataFrame(columns=DISCOVERY_COLUMNS)

    return pd.DataFrame(rows, columns=DISCOVERY_COLUMNS)


def merge_discovery(discovery_df: pd.DataFrame, newly_found_df: pd.DataFrame) -> pd.DataFrame:
    if discovery_df.empty:
        merged = newly_found_df.copy()
    else:
        merged = pd.concat([newly_found_df, discovery_df], ignore_index=True)

    merged = (
        merged.sort_values(["discovered_at"], ascending=[False], kind="stable")
        .drop_duplicates(subset=["company_number"], keep="first")
        .reset_index(drop=True)
    )
    return merged


def merge_queue(queue_df: pd.DataFrame, discovery_df: pd.DataFrame) -> pd.DataFrame:
    if queue_df.empty:
        queue_df = pd.DataFrame(columns=QUEUE_COLUMNS)

    queue_df = queue_df.copy()

    for col in QUEUE_COLUMNS:
        if col not in queue_df.columns:
            queue_df[col] = ""

    existing_status = {}
    if not queue_df.empty:
        existing_status = (
            queue_df.drop_duplicates(subset=["company_number"], keep="first")
            .set_index("company_number")["status"]
            .astype(str)
            .to_dict()
        )

    discovery_rows = []
    for rank, (_, row) in enumerate(discovery_df.iterrows()):
        company_number = str(row.get("company_number", "")).strip()
        if not company_number:
            continue

        already_status = existing_status.get(company_number, "")

        if already_status in {"matched", "not_matched", "error"}:
            continue

        if already_status == "pending":
            continue

        discovery_rows.append(
            {
                "company_number": company_number,
                "company_name": str(row.get("company_name", "")).strip(),
                "discovered_at": str(row.get("discovered_at", "")).strip(),
                "last_seen_at": str(row.get("discovered_at", "")).strip(),
                "status": "pending",
                "matched_country": "",
                "processed_at": "",
                "error_message": "",
                "priority_rank": str(rank),
            }
        )

    if discovery_rows:
        queue_df = pd.concat(
            [queue_df, pd.DataFrame(discovery_rows, columns=QUEUE_COLUMNS)],
            ignore_index=True,
        )

    discovery_lookup = (
        discovery_df.drop_duplicates(subset=["company_number"], keep="first")
        .set_index("company_number")[["company_name", "discovered_at"]]
    )

    for idx, row in queue_df.iterrows():
        company_number = str(row.get("company_number", "")).strip()
        if company_number in discovery_lookup.index:
            queue_df.at[idx, "last_seen_at"] = str(discovery_lookup.at[company_number, "discovered_at"])
            if not str(row.get("company_name", "")).strip():
                queue_df.at[idx, "company_name"] = str(discovery_lookup.at[company_number, "company_name"])

    queue_df = (
        queue_df.sort_values(["discovered_at"], ascending=[False], kind="stable")
        .drop_duplicates(subset=["company_number"], keep="first")
        .reset_index(drop=True)
    )

    discovery_rank_map = {
        str(row["company_number"]): idx
        for idx, (_, row) in enumerate(
            discovery_df.drop_duplicates(subset=["company_number"], keep="first").iterrows()
        )
    }

    queue_df["priority_rank"] = queue_df["company_number"].map(discovery_rank_map).fillna(999999).astype(int).astype(str)

    return queue_df


def fetch_company_officers(company_number: str, api_keys: List[str]) -> List[dict]:
    url = f"https://api.company-information.service.gov.uk/company/{company_number}/officers"
    params = {"items_per_page": "35"}
    response = fetch_with_rotation(url, params, api_keys)
    payload = response.json()
    return payload.get("items", []) or []


def get_matching_director_countries(company_number: str, api_keys: List[str]) -> Set[str]:
    officers = fetch_company_officers(company_number, api_keys)
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


def process_queue_batch(
    queue_df: pd.DataFrame,
    api_keys: List[str],
    batch_size: int,
) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    if queue_df.empty:
        return queue_df, pd.DataFrame(columns=RESULT_COLUMNS), 0

    queue_df = queue_df.copy()
    matched_rows = []
    processed_count = 0

    already_screened_statuses = {"matched", "not_matched", "error"}

    pending_df = queue_df[
        ~queue_df["status"].astype(str).isin(already_screened_statuses)
    ].copy()

    pending_df = pending_df[pending_df["status"].astype(str) == "pending"].copy()

    if pending_df.empty:
        return queue_df, pd.DataFrame(columns=RESULT_COLUMNS), 0

    pending_df["priority_rank_num"] = pd.to_numeric(
        pending_df["priority_rank"], errors="coerce"
    ).fillna(999999)

    pending_df = pending_df.sort_values(
        ["priority_rank_num", "discovered_at"],
        ascending=[True, False],
        kind="stable",
    ).head(batch_size)

    for pull_order, (idx, row) in enumerate(pending_df.iterrows()):
        company_number = str(row.get("company_number", "")).strip()
        if not company_number:
            continue

        try:
            matching_countries = get_matching_director_countries(company_number, api_keys)
            matched_country = classify_country_match(matching_countries)

            if matched_country:
                queue_df.at[idx, "status"] = "matched"
                queue_df.at[idx, "matched_country"] = matched_country
                matched_rows.append(
                    {
                        "company_number": company_number,
                        "company_name": str(row.get("company_name", "")).strip(),
                        "sector": matched_country,
                        "time_added_to_table": now_uk_str(),
                        "pull_order": pull_order,
                    }
                )
            else:
                queue_df.at[idx, "status"] = "not_matched"
                queue_df.at[idx, "matched_country"] = ""

            queue_df.at[idx, "processed_at"] = now_uk_str()
            queue_df.at[idx, "error_message"] = ""
            processed_count += 1

        except RuntimeError as e:
            queue_df.at[idx, "status"] = "pending"
            queue_df.at[idx, "error_message"] = str(e)
            if "rate limit" in str(e).lower() or "403" in str(e).lower() or "429" in str(e).lower():
                break

        except Exception as e:
            queue_df.at[idx, "status"] = "error"
            queue_df.at[idx, "processed_at"] = now_uk_str()
            queue_df.at[idx, "error_message"] = str(e)
            processed_count += 1

    matched_df = (
        pd.DataFrame(matched_rows, columns=RESULT_COLUMNS)
        if matched_rows
        else pd.DataFrame(columns=RESULT_COLUMNS)
    )

    queue_df = queue_df.drop(columns=["priority_rank_num"], errors="ignore")
    return queue_df, matched_df, processed_count


def merge_preserving_timestamps(fetched_df: pd.DataFrame, existing_df: pd.DataFrame) -> pd.DataFrame:
    if fetched_df.empty:
        return existing_df.copy() if not existing_df.empty else pd.DataFrame(columns=RESULT_COLUMNS)

    if existing_df.empty:
        return fetched_df.copy()

    existing_lookup = existing_df.set_index("company_number")[["time_added_to_table", "pull_order"]]
    existing_numbers = set(existing_df["company_number"].astype(str))

    new_rows = fetched_df[~fetched_df["company_number"].astype(str).isin(existing_numbers)].copy()
    known_rows = fetched_df[fetched_df["company_number"].astype(str).isin(existing_numbers)].copy()

    known_rows["time_added_to_table"] = known_rows["company_number"].map(existing_lookup["time_added_to_table"])
    known_rows["pull_order"] = known_rows["company_number"].map(existing_lookup["pull_order"].astype(int))

    merged = pd.concat([new_rows, known_rows, existing_df], ignore_index=True)
    return (
        merged.sort_values(["time_added_to_table", "pull_order"], ascending=[False, False], kind="stable")
        .drop_duplicates(subset=["company_number"], keep="first")
        .reset_index(drop=True)
    )


def load_results(path: Path) -> pd.DataFrame:
    return load_generic_csv(path, RESULT_COLUMNS)


def load_leads(person: str, incorporated_from: str, incorporated_to: str) -> pd.DataFrame:
    return load_generic_csv(lead_file_path(person, incorporated_from, incorporated_to), LEAD_COLUMNS)


def load_queue(incorporated_from: str, incorporated_to: str) -> pd.DataFrame:
    return load_generic_csv(queue_file_path(incorporated_from, incorporated_to), QUEUE_COLUMNS)


def load_discovery(incorporated_from: str, incorporated_to: str) -> pd.DataFrame:
    return load_generic_csv(discovery_file_path(incorporated_from, incorporated_to), DISCOVERY_COLUMNS)


def save_state(
    matched_df: pd.DataFrame,
    queue_df: pd.DataFrame,
    discovery_df: pd.DataFrame,
    incorporated_from: str,
    incorporated_to: str,
) -> None:
    save_csv(matched_df, snapshot_path(incorporated_from, incorporated_to), RESULT_COLUMNS)
    save_csv(queue_df, queue_file_path(incorporated_from, incorporated_to), QUEUE_COLUMNS)
    save_csv(discovery_df, discovery_file_path(incorporated_from, incorporated_to), DISCOVERY_COLUMNS)


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


def render_quick_add(
    df: pd.DataFrame,
    person: str,
    incorporated_from: str,
    incorporated_to: str,
    existing_leads: pd.DataFrame,
) -> None:
    st.subheader(f"Quick add to {person}'s leads")

    if df.empty:
        st.info("No matched companies available to add.")
        return

    existing_numbers = (
        set(existing_leads["company_number"].astype(str))
        if not existing_leads.empty
        else set()
    )

    for idx, (_, row) in enumerate(df.iterrows()):
        company_number = str(row.get("company_number", "")).strip()
        already_added = company_number in existing_numbers

        c1, c2, c3, c4 = st.columns([5, 1.8, 2, 0.9])
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
    st.title("International Directors Queue")
    st.caption("Discovery queue + processing queue with already screened names excluded from re-checking.")

    api_keys = get_api_keys()
    if not api_keys:
        st.error("Add COMPANIES_HOUSE_API_KEYS or CH_API_KEY_1/2/3 to Streamlit secrets before running the app.")
        st.stop()

    st.sidebar.header("Controls")
    selected_user = st.sidebar.selectbox("Working as", TEAM_MEMBERS, index=0)

    default_date = today_uk_date()
    start_date = st.sidebar.date_input("Incorporated from", value=default_date)
    end_date = st.sidebar.date_input("Incorporated to", value=default_date)
    pages_per_run = st.sidebar.number_input(
        "Discovery pages per refresh",
        min_value=1,
        max_value=5,
        value=DISCOVERY_PAGES_PER_RUN,
        step=1,
    )
    process_batch_size = st.sidebar.number_input(
        "Processing batch size",
        min_value=1,
        max_value=50,
        value=PROCESS_BATCH_SIZE_DEFAULT,
        step=1,
    )

    if start_date > end_date:
        st.sidebar.error("'Incorporated from' must be on or before 'Incorporated to'.")
        st.stop()

    incorporated_from = start_date.isoformat()
    incorporated_to = end_date.isoformat()

    st.sidebar.caption(
        "Countries: France, Germany, Spain, Norway, Italy, Sweden, Netherlands, Belgium, "
        "Finland, Denmark, Poland, Portugal, USA, India, Hong Kong"
    )

    refresh = st.sidebar.button("Run discovery + processing", type="primary")

    matched_path = snapshot_path(incorporated_from, incorporated_to)
    matched_df_existing = load_results(matched_path)
    queue_df = load_queue(incorporated_from, incorporated_to)
    discovery_df = load_discovery(incorporated_from, incorporated_to)

    if refresh:
        try:
            new_discovery_df = fetch_discovery_pages(
                api_keys,
                incorporated_from,
                incorporated_to,
                int(pages_per_run),
            )
            discovery_df = merge_discovery(discovery_df, new_discovery_df)
            queue_df = merge_queue(queue_df, discovery_df)

            queue_df, new_matched_df, processed_count = process_queue_batch(
                queue_df,
                api_keys,
                int(process_batch_size),
            )

            matched_df = merge_preserving_timestamps(new_matched_df, matched_df_existing)
            save_state(matched_df, queue_df, discovery_df, incorporated_from, incorporated_to)

            st.session_state["latest_df"] = matched_df
            st.session_state["sorted_df"] = get_sorted_current_df(matched_df)
            st.session_state["queue_df"] = queue_df
            st.session_state["discovery_df"] = discovery_df
            st.session_state["processed_count"] = processed_count
            st.session_state["last_refresh"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

        except Exception as e:
            st.error(str(e))
            st.stop()
    else:
        matched_df = matched_df_existing
        st.session_state.setdefault("latest_df", matched_df)
        st.session_state.setdefault("sorted_df", get_sorted_current_df(matched_df))
        st.session_state.setdefault("queue_df", queue_df)
        st.session_state.setdefault("discovery_df", discovery_df)
        st.session_state.setdefault("processed_count", 0)
        st.session_state.setdefault("last_refresh", "Not refreshed in this session")

    matched_df = st.session_state.get("latest_df", pd.DataFrame(columns=RESULT_COLUMNS))
    sorted_df = st.session_state.get("sorted_df", pd.DataFrame(columns=RESULT_COLUMNS))
    queue_df = st.session_state.get("queue_df", pd.DataFrame(columns=QUEUE_COLUMNS))
    discovery_df = st.session_state.get("discovery_df", pd.DataFrame(columns=DISCOVERY_COLUMNS))
    leads_df = load_leads(selected_user, incorporated_from, incorporated_to)

    pending_count = int((queue_df["status"] == "pending").sum()) if not queue_df.empty else 0
    matched_count = int((queue_df["status"] == "matched").sum()) if not queue_df.empty else 0
    not_matched_count = int((queue_df["status"] == "not_matched").sum()) if not queue_df.empty else 0
    error_count = int((queue_df["status"] == "error").sum()) if not queue_df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Discovered", int(len(discovery_df)))
    c2.metric("Pending queue", pending_count)
    c3.metric("Matched", matched_count)
    c4.metric("Not matched", not_matched_count)
    c5.metric("Errors", error_count)

    st.caption(
        f"Working as {selected_user} | Range: {incorporated_from} to {incorporated_to} | "
        f"Last refresh: {st.session_state.get('last_refresh', 'Unknown')}"
    )

    st.caption(f"Processed this run: {int(st.session_state.get('processed_count', 0))}")

    newest_df = sorted_df.head(15).reset_index(drop=True) if not sorted_df.empty else sorted_df
    render_quick_add(newest_df, selected_user, incorporated_from, incorporated_to, leads_df)

    with st.expander("Queue status", expanded=True):
        if queue_df.empty:
            st.info("No queue items yet.")
        else:
            queue_preview = queue_df.copy()
            queue_preview = queue_preview.sort_values(
                ["status", "priority_rank", "discovered_at"],
                ascending=[True, True, False],
                kind="stable",
            )
            st.dataframe(queue_preview, use_container_width=True, hide_index=True)

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
                file_name=f"{selected_user.lower()}_leads_{date_suffix(incorporated_from, incorporated_to)}.csv",
                mime="text/csv",
                key=f"download_{selected_user.lower()}_leads",
            )

    with st.expander("Matched results CSV", expanded=False):
        if not matched_df.empty:
            st.download_button(
                label="Download matched results as CSV",
                data=convert_results_csv_bytes(matched_df),
                file_name=f"matched_companies_{date_suffix(incorporated_from, incorporated_to)}.csv",
                mime="text/csv",
                key="download_results_csv",
            )
        else:
            st.info("No matched results available yet.")

    with st.expander("Matched table", expanded=False):
        if matched_df.empty:
            st.info("No matched companies to show yet.")
        else:
            preview_df = sorted_df[["company_name", "sector", "time_added_to_table"]].rename(
                columns={
                    "company_name": "Company Name",
                    "sector": "Matched Director Country",
                    "time_added_to_table": "Time Added To Table",
                }
            )
            st.dataframe(preview_df, use_container_width=True, hide_index=True)

    with st.expander("Notes", expanded=False):
        st.write(
            "Already screened companies are retained in the queue with status values like matched, "
            "not_matched, or error, and are not sent for officer lookup again."
        )
        st.write(
            "This prevents repeated screening of the same names, although full-day progress will improve further "
            "once a deeper discovery backfill cursor is added."
        )


if __name__ == "__main__":
    main()
