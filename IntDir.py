import base64
import json
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="International Directors Universe", layout="wide")

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
FRONT_SCAN_PAGES_DEFAULT = 2
BACKFILL_PAGES_PER_RUN_DEFAULT = 1
PROCESS_BATCH_SIZE_DEFAULT = 15

REQUEST_PAUSE_SECONDS = 0.12
RATE_LIMIT_PER_KEY = 599
RATE_LIMIT_WINDOW_SECONDS = 300

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

LEADS_DIR = DATA_DIR / "leads"
LEADS_DIR.mkdir(exist_ok=True)

UNIVERSE_DIR = DATA_DIR / "universe"
UNIVERSE_DIR.mkdir(exist_ok=True)

STATUS_DIR = DATA_DIR / "status"
STATUS_DIR.mkdir(exist_ok=True)

STATE_DIR = DATA_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

MATCHED_DIR = DATA_DIR / "matched"
MATCHED_DIR.mkdir(exist_ok=True)

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

UNIVERSE_COLUMNS = [
    "company_number",
    "company_name",
    "first_discovered_at",
    "last_discovered_at",
    "latest_source",
]

STATUS_COLUMNS = [
    "company_number",
    "company_name",
    "status",
    "matched_country",
    "processed_at",
    "error_message",
    "first_discovered_at",
    "last_discovered_at",
]

_key_request_log: Dict[str, Deque[float]] = {}


def today_uk_date() -> date:
    return datetime.now().astimezone().date()


def now_uk_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def date_suffix(incorporated_from: str, incorporated_to: str) -> str:
    return f"{incorporated_from}_to_{incorporated_to}"


def universe_file_path(incorporated_from: str, incorporated_to: str) -> Path:
    return UNIVERSE_DIR / f"universe_{date_suffix(incorporated_from, incorporated_to)}.csv"


def status_file_path(incorporated_from: str, incorporated_to: str) -> Path:
    return STATUS_DIR / f"status_{date_suffix(incorporated_from, incorporated_to)}.csv"


def matched_file_path(incorporated_from: str, incorporated_to: str) -> Path:
    return MATCHED_DIR / f"matched_{date_suffix(incorporated_from, incorporated_to)}.csv"


def state_file_path(incorporated_from: str, incorporated_to: str) -> Path:
    return STATE_DIR / f"state_{date_suffix(incorporated_from, incorporated_to)}.json"


def lead_file_path(person: str, incorporated_from: str, incorporated_to: str) -> Path:
    return LEADS_DIR / f"{person.strip().lower()}_leads_{date_suffix(incorporated_from, incorporated_to)}.csv"


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
        "User-Agent": "streamlit-international-directors-universe-app",
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


def load_state(incorporated_from: str, incorporated_to: str) -> Dict[str, int]:
    path = state_file_path(incorporated_from, incorporated_to)
    if not path.exists():
        return {"next_backfill_start_index": FRONT_SCAN_PAGES_DEFAULT * DISCOVERY_PAGE_SIZE}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"next_backfill_start_index": FRONT_SCAN_PAGES_DEFAULT * DISCOVERY_PAGE_SIZE}


def save_state_json(incorporated_from: str, incorporated_to: str, state: Dict[str, int]) -> None:
    path = state_file_path(incorporated_from, incorporated_to)
    path.write_text(json.dumps(state))


def fetch_advanced_search_page(
    api_keys: List[str],
    incorporated_from: str,
    incorporated_to: str,
    start_index: int,
    size: int,
    source_label: str,
) -> pd.DataFrame:
    url = "https://api.company-information.service.gov.uk/advanced-search/companies"
    params = {
        "incorporated_from": incorporated_from,
        "incorporated_to": incorporated_to,
        "size": str(size),
        "start_index": str(start_index),
    }

    response = fetch_with_rotation(url, params, api_keys)
    payload = response.json()
    items = payload.get("items", []) or []

    discovered_at = now_uk_str()
    rows = []

    for item in items:
        company_number = str(item.get("company_number", "")).strip()
        company_name = str(item.get("company_name", "")).strip()
        if not company_number:
            continue

        rows.append(
            {
                "company_number": company_number,
                "company_name": company_name,
                "first_discovered_at": discovered_at,
                "last_discovered_at": discovered_at,
                "latest_source": source_label,
            }
        )

    if not rows:
        return pd.DataFrame(columns=UNIVERSE_COLUMNS)

    return pd.DataFrame(rows, columns=UNIVERSE_COLUMNS)


def discover_companies(
    api_keys: List[str],
    incorporated_from: str,
    incorporated_to: str,
    front_scan_pages: int,
    backfill_pages_per_run: int,
) -> Tuple[pd.DataFrame, Dict[str, int], int]:
    universe_df = load_generic_csv(universe_file_path(incorporated_from, incorporated_to), UNIVERSE_COLUMNS)
    state = load_state(incorporated_from, incorporated_to)

    fetched_frames = []
    total_new_added = 0

    for page in range(front_scan_pages):
        start_index = page * DISCOVERY_PAGE_SIZE
        page_df = fetch_advanced_search_page(
            api_keys,
            incorporated_from,
            incorporated_to,
            start_index,
            DISCOVERY_PAGE_SIZE,
            f"front_{page}",
        )
        fetched_frames.append(page_df)

    next_backfill_start_index = int(state.get("next_backfill_start_index", front_scan_pages * DISCOVERY_PAGE_SIZE))

    for _ in range(backfill_pages_per_run):
        page_df = fetch_advanced_search_page(
            api_keys,
            incorporated_from,
            incorporated_to,
            next_backfill_start_index,
            DISCOVERY_PAGE_SIZE,
            f"backfill_{next_backfill_start_index}",
        )
        fetched_frames.append(page_df)

        if page_df.empty:
            next_backfill_start_index = front_scan_pages * DISCOVERY_PAGE_SIZE
            break
        else:
            next_backfill_start_index += DISCOVERY_PAGE_SIZE

    state["next_backfill_start_index"] = next_backfill_start_index

    if fetched_frames:
        new_fetch_df = pd.concat(fetched_frames, ignore_index=True)
    else:
        new_fetch_df = pd.DataFrame(columns=UNIVERSE_COLUMNS)

    if universe_df.empty:
        merged = new_fetch_df.copy()
        total_new_added = len(merged)
    else:
        existing_numbers = set(universe_df["company_number"].astype(str))
        total_new_added = int((~new_fetch_df["company_number"].astype(str).isin(existing_numbers)).sum()) if not new_fetch_df.empty else 0

        combined = pd.concat([new_fetch_df, universe_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["company_number"], keep="first").reset_index(drop=True)

        if not new_fetch_df.empty:
            latest_lookup = new_fetch_df.drop_duplicates(subset=["company_number"], keep="first").set_index("company_number")
            existing_lookup = universe_df.drop_duplicates(subset=["company_number"], keep="first").set_index("company_number")

            rows = []
            for company_number in combined["company_number"].astype(str):
                if company_number in latest_lookup.index and company_number in existing_lookup.index:
                    latest_row = latest_lookup.loc[company_number]
                    existing_row = existing_lookup.loc[company_number]
                    rows.append(
                        {
                            "company_number": company_number,
                            "company_name": str(latest_row.get("company_name", existing_row.get("company_name", ""))),
                            "first_discovered_at": str(existing_row.get("first_discovered_at", latest_row.get("first_discovered_at", ""))),
                            "last_discovered_at": str(latest_row.get("last_discovered_at", existing_row.get("last_discovered_at", ""))),
                            "latest_source": str(latest_row.get("latest_source", existing_row.get("latest_source", ""))),
                        }
                    )
                elif company_number in latest_lookup.index:
                    latest_row = latest_lookup.loc[company_number]
                    rows.append(
                        {
                            "company_number": company_number,
                            "company_name": str(latest_row.get("company_name", "")),
                            "first_discovered_at": str(latest_row.get("first_discovered_at", "")),
                            "last_discovered_at": str(latest_row.get("last_discovered_at", "")),
                            "latest_source": str(latest_row.get("latest_source", "")),
                        }
                    )
                else:
                    existing_row = existing_lookup.loc[company_number]
                    rows.append(
                        {
                            "company_number": company_number,
                            "company_name": str(existing_row.get("company_name", "")),
                            "first_discovered_at": str(existing_row.get("first_discovered_at", "")),
                            "last_discovered_at": str(existing_row.get("last_discovered_at", "")),
                            "latest_source": str(existing_row.get("latest_source", "")),
                        }
                    )

            merged = pd.DataFrame(rows, columns=UNIVERSE_COLUMNS)
        else:
            merged = universe_df.copy()

    merged = merged.sort_values(["last_discovered_at"], ascending=[False], kind="stable").reset_index(drop=True)
    save_csv(merged, universe_file_path(incorporated_from, incorporated_to), UNIVERSE_COLUMNS)
    save_state_json(incorporated_from, incorporated_to, state)

    return merged, state, total_new_added


def build_or_update_status(universe_df: pd.DataFrame, incorporated_from: str, incorporated_to: str) -> pd.DataFrame:
    status_df = load_generic_csv(status_file_path(incorporated_from, incorporated_to), STATUS_COLUMNS)

    if status_df.empty:
        status_df = pd.DataFrame(columns=STATUS_COLUMNS)

    for col in STATUS_COLUMNS:
        if col not in status_df.columns:
            status_df[col] = ""

    status_lookup = (
        status_df.drop_duplicates(subset=["company_number"], keep="first").set_index("company_number")
        if not status_df.empty else pd.DataFrame()
    )

    rows = []
    for _, uni in universe_df.iterrows():
        company_number = str(uni.get("company_number", "")).strip()
        if not company_number:
            continue

        if not status_df.empty and company_number in status_lookup.index:
            old = status_lookup.loc[company_number]
            rows.append(
                {
                    "company_number": company_number,
                    "company_name": str(uni.get("company_name", old.get("company_name", ""))),
                    "status": str(old.get("status", "pending")) or "pending",
                    "matched_country": str(old.get("matched_country", "")),
                    "processed_at": str(old.get("processed_at", "")),
                    "error_message": str(old.get("error_message", "")),
                    "first_discovered_at": str(old.get("first_discovered_at", uni.get("first_discovered_at", ""))),
                    "last_discovered_at": str(uni.get("last_discovered_at", old.get("last_discovered_at", ""))),
                }
            )
        else:
            rows.append(
                {
                    "company_number": company_number,
                    "company_name": str(uni.get("company_name", "")).strip(),
                    "status": "pending",
                    "matched_country": "",
                    "processed_at": "",
                    "error_message": "",
                    "first_discovered_at": str(uni.get("first_discovered_at", "")),
                    "last_discovered_at": str(uni.get("last_discovered_at", "")),
                }
            )

    updated_status_df = pd.DataFrame(rows, columns=STATUS_COLUMNS)
    updated_status_df = updated_status_df.drop_duplicates(subset=["company_number"], keep="first").reset_index(drop=True)
    save_csv(updated_status_df, status_file_path(incorporated_from, incorporated_to), STATUS_COLUMNS)
    return updated_status_df


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


def process_unscreened_batch(
    status_df: pd.DataFrame,
    api_keys: List[str],
    batch_size: int,
    incorporated_from: str,
    incorporated_to: str,
) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    if status_df.empty:
        return status_df, pd.DataFrame(columns=RESULT_COLUMNS), 0

    status_df = status_df.copy()

    pending_df = status_df[status_df["status"].astype(str) == "pending"].copy()
    if pending_df.empty:
        save_csv(status_df, status_file_path(incorporated_from, incorporated_to), STATUS_COLUMNS)
        matched_existing = load_generic_csv(matched_file_path(incorporated_from, incorporated_to), RESULT_COLUMNS)
        return status_df, matched_existing, 0

    pending_df = pending_df.sort_values(
        ["last_discovered_at", "first_discovered_at"],
        ascending=[False, False],
        kind="stable",
    ).head(batch_size)

    matched_existing = load_generic_csv(matched_file_path(incorporated_from, incorporated_to), RESULT_COLUMNS)
    new_matched_rows = []
    processed_count = 0

    for pull_order, (idx, row) in enumerate(pending_df.iterrows()):
        company_number = str(row.get("company_number", "")).strip()
        company_name = str(row.get("company_name", "")).strip()

        try:
            matching_countries = get_matching_director_countries(company_number, api_keys)
            matched_country = classify_country_match(matching_countries)

            if matched_country:
                status_df.at[idx, "status"] = "matched"
                status_df.at[idx, "matched_country"] = matched_country

                if matched_existing.empty or company_number not in set(matched_existing["company_number"].astype(str)):
                    new_matched_rows.append(
                        {
                            "company_number": company_number,
                            "company_name": company_name,
                            "sector": matched_country,
                            "time_added_to_table": now_uk_str(),
                            "pull_order": pull_order,
                        }
                    )
            else:
                status_df.at[idx, "status"] = "not_matched"
                status_df.at[idx, "matched_country"] = ""

            status_df.at[idx, "processed_at"] = now_uk_str()
            status_df.at[idx, "error_message"] = ""
            processed_count += 1

        except RuntimeError as e:
            status_df.at[idx, "status"] = "pending"
            status_df.at[idx, "error_message"] = str(e)
            if "rate limit" in str(e).lower() or "403" in str(e).lower() or "429" in str(e).lower():
                break

        except Exception as e:
            status_df.at[idx, "status"] = "error"
            status_df.at[idx, "processed_at"] = now_uk_str()
            status_df.at[idx, "error_message"] = str(e)
            processed_count += 1

    new_matched_df = pd.DataFrame(new_matched_rows, columns=RESULT_COLUMNS) if new_matched_rows else pd.DataFrame(columns=RESULT_COLUMNS)

    if matched_existing.empty:
        matched_df = new_matched_df.copy()
    else:
        matched_df = pd.concat([new_matched_df, matched_existing], ignore_index=True)

    if not matched_df.empty:
        matched_df = (
            matched_df.sort_values(["time_added_to_table", "pull_order"], ascending=[False, False], kind="stable")
            .drop_duplicates(subset=["company_number"], keep="first")
            .reset_index(drop=True)
        )

    save_csv(status_df, status_file_path(incorporated_from, incorporated_to), STATUS_COLUMNS)
    save_csv(matched_df, matched_file_path(incorporated_from, incorporated_to), RESULT_COLUMNS)

    return status_df, matched_df, processed_count


def load_leads(person: str, incorporated_from: str, incorporated_to: str) -> pd.DataFrame:
    return load_generic_csv(lead_file_path(person, incorporated_from, incorporated_to), LEAD_COLUMNS)


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

    existing_numbers = set(existing_leads["company_number"].astype(str)) if not existing_leads.empty else set()

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
    st.title("International Directors Universe")
    st.caption("Local universe + screening-status model with newest discoveries prioritised and no re-screening.")

    api_keys = get_api_keys()
    if not api_keys:
        st.error("Add COMPANIES_HOUSE_API_KEYS or CH_API_KEY_1/2/3 to Streamlit secrets before running the app.")
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

    front_scan_pages = st.sidebar.number_input(
        "Front scan pages per refresh",
        min_value=1,
        max_value=5,
        value=FRONT_SCAN_PAGES_DEFAULT,
        step=1,
    )
    backfill_pages_per_run = st.sidebar.number_input(
        "Backfill pages per refresh",
        min_value=0,
        max_value=5,
        value=BACKFILL_PAGES_PER_RUN_DEFAULT,
        step=1,
    )
    process_batch_size = st.sidebar.number_input(
        "Officer checks per refresh",
        min_value=1,
        max_value=50,
        value=PROCESS_BATCH_SIZE_DEFAULT,
        step=1,
    )

    refresh = st.sidebar.button("Run refresh", type="primary")

    st.sidebar.caption(
        "Discovery uses advanced-search for the selected date, then screening only processes names still marked pending."
    )

    universe_df = load_generic_csv(universe_file_path(incorporated_from, incorporated_to), UNIVERSE_COLUMNS)
    status_df = load_generic_csv(status_file_path(incorporated_from, incorporated_to), STATUS_COLUMNS)
    matched_df = load_generic_csv(matched_file_path(incorporated_from, incorporated_to), RESULT_COLUMNS)
    state = load_state(incorporated_from, incorporated_to)

    if refresh:
        try:
            universe_df, state, new_discovered_count = discover_companies(
                api_keys,
                incorporated_from,
                incorporated_to,
                int(front_scan_pages),
                int(backfill_pages_per_run),
            )
            status_df = build_or_update_status(universe_df, incorporated_from, incorporated_to)
            status_df, matched_df, processed_count = process_unscreened_batch(
                status_df,
                api_keys,
                int(process_batch_size),
                incorporated_from,
                incorporated_to,
            )

            st.session_state["universe_df"] = universe_df
            st.session_state["status_df"] = status_df
            st.session_state["matched_df"] = matched_df
            st.session_state["state_json"] = state
            st.session_state["new_discovered_count"] = new_discovered_count
            st.session_state["processed_count"] = processed_count
            st.session_state["last_refresh"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

        except Exception as e:
            st.error(str(e))
            st.stop()
    else:
        st.session_state.setdefault("universe_df", universe_df)
        st.session_state.setdefault("status_df", status_df)
        st.session_state.setdefault("matched_df", matched_df)
        st.session_state.setdefault("state_json", state)
        st.session_state.setdefault("new_discovered_count", 0)
        st.session_state.setdefault("processed_count", 0)
        st.session_state.setdefault("last_refresh", "Not refreshed in this session")

    universe_df = st.session_state.get("universe_df", pd.DataFrame(columns=UNIVERSE_COLUMNS))
    status_df = st.session_state.get("status_df", pd.DataFrame(columns=STATUS_COLUMNS))
    matched_df = st.session_state.get("matched_df", pd.DataFrame(columns=RESULT_COLUMNS))
    state = st.session_state.get("state_json", {"next_backfill_start_index": front_scan_pages * DISCOVERY_PAGE_SIZE})

    leads_df = load_leads(selected_user, incorporated_from, incorporated_to)

    pending_count = int((status_df["status"] == "pending").sum()) if not status_df.empty else 0
    matched_count = int((status_df["status"] == "matched").sum()) if not status_df.empty else 0
    not_matched_count = int((status_df["status"] == "not_matched").sum()) if not status_df.empty else 0
    error_count = int((status_df["status"] == "error").sum()) if not status_df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Universe size", int(len(universe_df)))
    c2.metric("New discovered this run", int(st.session_state.get("new_discovered_count", 0)))
    c3.metric("Pending", pending_count)
    c4.metric("Matched", matched_count)
    c5.metric("Processed this run", int(st.session_state.get("processed_count", 0)))

    st.caption(
        f"Working as {selected_user} | Range: {incorporated_from} to {incorporated_to} | "
        f"Last refresh: {st.session_state.get('last_refresh', 'Unknown')}"
    )

    st.caption(
        f"Next backfill start_index: {int(state.get('next_backfill_start_index', int(front_scan_pages) * DISCOVERY_PAGE_SIZE))}"
    )

    newest_matched_df = matched_df.head(15).reset_index(drop=True) if not matched_df.empty else matched_df
    render_quick_add(newest_matched_df, selected_user, incorporated_from, incorporated_to, leads_df)

    with st.expander("Universe", expanded=False):
        if universe_df.empty:
            st.info("No discovered companies yet.")
        else:
            st.dataframe(universe_df, use_container_width=True, hide_index=True)

    with st.expander("Screening status", expanded=True):
        if status_df.empty:
            st.info("No status rows yet.")
        else:
            status_preview = status_df.sort_values(
                ["status", "last_discovered_at"],
                ascending=[True, False],
                kind="stable",
            )
            st.dataframe(status_preview, use_container_width=True, hide_index=True)

    with st.expander("Matched companies", expanded=False):
        if matched_df.empty:
            st.info("No matched companies yet.")
        else:
            preview_df = matched_df.rename(
                columns={
                    "company_name": "Company Name",
                    "sector": "Matched Director Country",
                    "time_added_to_table": "Time Added To Table",
                }
            )
            st.dataframe(preview_df, use_container_width=True, hide_index=True)
            st.download_button(
                label="Download matched results CSV",
                data=convert_results_csv_bytes(matched_df),
                file_name=f"matched_companies_{date_suffix(incorporated_from, incorporated_to)}.csv",
                mime="text/csv",
                key="download_results_csv",
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
                file_name=f"{selected_user.lower()}_leads_{date_suffix(incorporated_from, incorporated_to)}.csv",
                mime="text/csv",
                key=f"download_{selected_user.lower()}_leads",
            )

    with st.expander("Notes", expanded=False):
        st.write(
            "This version keeps a persistent universe of discovered companies for the chosen date range, "
            "a separate persistent screening-status table, and only officer-checks rows still marked pending."
        )
        st.write(
            "Discovery combines a front scan for newest companies and a backfill cursor for deeper pages, "
            "so it can keep finding new names without repeatedly looping over only the first slice."
        )


if __name__ == "__main__":
    main()
