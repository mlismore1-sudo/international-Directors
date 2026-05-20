import base64
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from pandas.errors import EmptyDataError, ParserError
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

TARGET_COUNTRY_MAP = {
    "france": "France",
    "germany": "Germany",
    "spain": "Spain",
    "portugal": "Portugal",
    "belgium": "Belgium",
    "netherlands": "Netherlands",
    "the netherlands": "Netherlands",
    "poland": "Poland",
    "italy": "Italy",
    "austria": "Austria",
    "norway": "Norway",
    "sweden": "Sweden",
    "denmark": "Denmark",
    "finland": "Finland",
    "ireland": "Ireland",
    "croatia": "Croatia",
    "usa": "United States",
    "u.s.a.": "United States",
    "u.s.": "United States",
    "u s a": "United States",
    "united states": "United States",
    "united states of america": "United States",
    "us": "United States",
    "hong kong": "Hong Kong",
    "india": "India",
}

UK_COUNTRY_ALIASES = {
    "uk", "u.k.", "united kingdom", "england", "scotland", "wales",
    "northern ireland", "great britain", "britain"
}

ALL_INTERNATIONAL_COUNTRY_ALIASES = dict(TARGET_COUNTRY_MAP)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
LEADS_DIR = DATA_DIR / "leads"
LEADS_DIR.mkdir(exist_ok=True)

TEAM_MEMBERS = ["Brad", "James"]
QUICK_ADD_DEFAULT = 15
MAX_NEW_COMPANIES_PER_REFRESH = 25

RESULT_COLUMNS = [
    "company_number",
    "company_name",
    "sector",
    "time_added_to_table",
    "pull_order",

    "director_target_country_match",
    "director_target_countries",
    "director_non_uk_signal",
    "director_uk_present",

    "psc_individual_target_match",
    "psc_individual_target_countries",
    "psc_individual_uk_present",

    "psc_entity_target_match",
    "psc_entity_target_countries",
    "psc_entity_non_uk_signal",
    "psc_entity_uk_present",

    "psc_statement_present",
    "psc_statement_types",

    "any_target_country_signal",
    "any_international_signal",
    "match_confidence",
]

LEAD_COLUMNS = RESULT_COLUMNS + ["added_by", "added_at"]


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


def clean_country(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(str(value).strip().lower().split())


def normalize_target_country(value: Optional[str]) -> str:
    cleaned = clean_country(value)
    return TARGET_COUNTRY_MAP.get(cleaned, "")


def is_uk_country(value: Optional[str]) -> bool:
    return clean_country(value) in UK_COUNTRY_ALIASES


def is_non_uk_country_text(value: Optional[str]) -> bool:
    cleaned = clean_country(value)
    return bool(cleaned) and cleaned not in UK_COUNTRY_ALIASES


def get_session() -> requests.Session:
    if "http_session" not in st.session_state:
        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=30, pool_maxsize=30)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        st.session_state["http_session"] = session
    return st.session_state["http_session"]


def fetch_with_rotation(
    url: str,
    params: Dict[str, str],
    api_keys: List[str],
    timeout: Tuple[float, float] = (3.05, 20),
    allow_not_found: bool = False,
) -> requests.Response:
    session = get_session()
    last_response = None

    for api_key in api_keys:
        response = session.get(url, headers=auth_header(api_key), params=params, timeout=timeout)

        if response.status_code in (401, 429):
            last_response = response
            continue

        if allow_not_found and response.status_code == 404:
            return response

        response.raise_for_status()
        return response

    if last_response is not None:
        if allow_not_found and last_response.status_code == 404:
            return last_response
        last_response.raise_for_status()

    raise RuntimeError("No valid Companies House API keys were available.")


@st.cache_data(ttl=900, show_spinner=False)
def fetch_companies_incorporated_today(api_keys_tuple: tuple[str, ...], run_date: str) -> pd.DataFrame:
    api_keys = list(api_keys_tuple)
    url = "https://api.company-information.service.gov.uk/advanced-search/companies"
    start_index = 0
    page_size = 500
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
                "time_added_to_table": timestamp,
                "pull_order": pull_counter,
                "director_target_country_match": "No",
                "director_target_countries": "",
                "director_non_uk_signal": "No",
                "director_uk_present": "No",
                "psc_individual_target_match": "No",
                "psc_individual_target_countries": "",
                "psc_individual_uk_present": "No",
                "psc_entity_target_match": "No",
                "psc_entity_target_countries": "",
                "psc_entity_non_uk_signal": "No",
                "psc_entity_uk_present": "No",
                "psc_statement_present": "No",
                "psc_statement_types": "",
                "any_target_country_signal": "No",
                "any_international_signal": "No",
                "match_confidence": "Low",
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
def fetch_director_signals_cached(company_number: str, api_keys_tuple: tuple[str, ...]) -> Tuple[str, str, str, str]:
    api_keys = list(api_keys_tuple)
    url = f"https://api.company-information.service.gov.uk/company/{company_number}/officers"
    start_index = 0
    items_per_page = 100

    target_countries = set()
    uk_present = False
    non_uk_signal = False

    while True:
        params = {
            "items_per_page": str(items_per_page),
            "start_index": str(start_index),
        }

        try:
            response = fetch_with_rotation(
                url=url,
                params=params,
                api_keys=api_keys,
                allow_not_found=True,
            )

            if response.status_code == 404:
                return "No", "", "No", "No"

            payload = response.json()
            items = payload.get("items", []) or []

            for officer in items:
                role = str(officer.get("officer_role", "")).strip().lower()
                if role != "director":
                    continue
                if officer.get("resigned_on"):
                    continue

                country_of_residence = officer.get("country_of_residence", "")
                nationality = officer.get("nationality", "")

                normalized_country = normalize_target_country(country_of_residence)
                if normalized_country:
                    target_countries.add(normalized_country)

                if is_uk_country(country_of_residence):
                    uk_present = True

                if is_non_uk_country_text(country_of_residence) or is_non_uk_country_text(nationality):
                    non_uk_signal = True

            total_results = int(payload.get("total_results", len(items)))
            start_index += len(items)
            if not items or start_index >= total_results:
                break

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (400, 403, 404):
                return "No", "", "No", "No"
            raise
        except requests.RequestException:
            return "No", "", "No", "No"

    return (
        "Yes" if target_countries else "No",
        ", ".join(sorted(target_countries)),
        "Yes" if non_uk_signal else "No",
        "Yes" if uk_present else "No",
    )


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_psc_statement_signals_cached(company_number: str, api_keys_tuple: tuple[str, ...]) -> Tuple[str, str]:
    api_keys = list(api_keys_tuple)
    url = f"https://api.company-information.service.gov.uk/company/{company_number}/persons-with-significant-control-statements"
    start_index = 0
    items_per_page = 100

    statement_types = set()

    while True:
        params = {
            "items_per_page": str(items_per_page),
            "start_index": str(start_index),
        }

        try:
            response = fetch_with_rotation(
                url=url,
                params=params,
                api_keys=api_keys,
                allow_not_found=True,
            )
            if response.status_code == 404:
                return "No", ""

            payload = response.json()
            items = payload.get("items", []) or []

            for item in items:
                if item.get("ceased_on"):
                    continue
                for statement in item.get("statement", []) or []:
                    if statement:
                        statement_types.add(str(statement).strip())

            total_results = int(payload.get("total_results", len(items)))
            start_index += len(items)
            if not items or start_index >= total_results:
                break

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (400, 403, 404):
                return "No", ""
            raise
        except requests.RequestException:
            return "No", ""

    return ("Yes" if statement_types else "No", ", ".join(sorted(statement_types)))


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_psc_signals_cached(company_number: str, api_keys_tuple: tuple[str, ...]) -> Tuple[str, str, str, str, str, str, str, str]:
    api_keys = list(api_keys_tuple)
    url = f"https://api.company-information.service.gov.uk/company/{company_number}/persons-with-significant-control"
    start_index = 0
    items_per_page = 100

    individual_target_countries = set()
    entity_target_countries = set()

    individual_uk_present = False
    entity_uk_present = False
    entity_non_uk_signal = False

    individual_kinds = {
        "individual-person-with-significant-control",
        "individual-beneficial-owner",
    }
    entity_kinds = {
        "corporate-entity-person-with-significant-control",
        "legal-person-with-significant-control",
        "corporate-entity-beneficial-owner",
        "legal-person-beneficial-owner",
    }
    super_secure_kinds = {
        "super-secure-person-with-significant-control",
        "super-secure-beneficial-owner",
    }

    while True:
        params = {
            "items_per_page": str(items_per_page),
            "start_index": str(start_index),
            "register_view": "true",
        }

        try:
            response = fetch_with_rotation(
                url=url,
                params=params,
                api_keys=api_keys,
                allow_not_found=True,
            )

            if response.status_code == 404:
                statement_present, statement_types = fetch_psc_statement_signals_cached(company_number, tuple(api_keys))
                return "No", "", "No", "No", "", "No", statement_present, statement_types

            payload = response.json()
            items = payload.get("items", []) or []

            for psc in items:
                if psc.get("ceased") or psc.get("ceased_on"):
                    continue

                kind = str(psc.get("kind", "")).strip().lower()

                if kind in individual_kinds:
                    candidate_countries = [
                        psc.get("country_of_residence", ""),
                        (psc.get("address") or {}).get("country", ""),
                    ]
                    for candidate in candidate_countries:
                        normalized = normalize_target_country(candidate)
                        if normalized:
                            individual_target_countries.add(normalized)
                        if is_uk_country(candidate):
                            individual_uk_present = True

                elif kind in entity_kinds:
                    identification = psc.get("identification", {}) or {}
                    principal_office = psc.get("principal_office_address", {}) or {}
                    service_address = psc.get("address", {}) or {}
                    candidate_countries = [
                        identification.get("country_registered", ""),
                        principal_office.get("country", ""),
                        service_address.get("country", ""),
                    ]
                    for candidate in candidate_countries:
                        normalized = normalize_target_country(candidate)
                        if normalized:
                            entity_target_countries.add(normalized)
                        if is_uk_country(candidate):
                            entity_uk_present = True
                        if is_non_uk_country_text(candidate):
                            entity_non_uk_signal = True

                elif kind in super_secure_kinds:
                    pass

            total_results = int(payload.get("total_results", len(items)))
            start_index += len(items)
            if not items or start_index >= total_results:
                break

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (400, 403, 404):
                statement_present, statement_types = fetch_psc_statement_signals_cached(company_number, tuple(api_keys))
                return "No", "", "No", "No", "", "No", statement_present, statement_types
            raise
        except requests.RequestException:
            statement_present, statement_types = fetch_psc_statement_signals_cached(company_number, tuple(api_keys))
            return "No", "", "No", "No", "", "No", statement_present, statement_types

    statement_present, statement_types = fetch_psc_statement_signals_cached(company_number, tuple(api_keys))

    return (
        "Yes" if individual_target_countries else "No",
        ", ".join(sorted(individual_target_countries)),
        "Yes" if individual_uk_present else "No",
        "Yes" if entity_target_countries else "No",
        ", ".join(sorted(entity_target_countries)),
        "Yes" if entity_non_uk_signal else "No",
        statement_present,
        statement_types,
    )


def compute_summary_signals(row: pd.Series) -> Tuple[str, str]:
    target_hit = any([
        str(row.get("director_target_country_match", "No")).lower() == "yes",
        str(row.get("psc_individual_target_match", "No")).lower() == "yes",
        str(row.get("psc_entity_target_match", "No")).lower() == "yes",
    ])

    international_hit = target_hit or any([
        str(row.get("director_non_uk_signal", "No")).lower() == "yes",
        str(row.get("psc_entity_non_uk_signal", "No")).lower() == "yes",
    ])

    confidence = "Low"
    if (
        str(row.get("director_target_country_match", "No")).lower() == "yes" or
        str(row.get("psc_individual_target_match", "No")).lower() == "yes"
    ):
        confidence = "High"
    elif (
        str(row.get("psc_entity_target_match", "No")).lower() == "yes" or
        str(row.get("director_non_uk_signal", "No")).lower() == "yes" or
        str(row.get("psc_entity_non_uk_signal", "No")).lower() == "yes"
    ):
        confidence = "Medium"

    if (
        confidence == "Low" and
        str(row.get("psc_statement_present", "No")).lower() == "yes"
    ):
        confidence = "Low"

    return ("Yes" if target_hit else "No", "Yes" if international_hit else "No"), confidence


def get_store_paths(run_date: str) -> Tuple[Path, Path]:
    snapshot_path = DATA_DIR / f"companies_broad_{run_date}.csv"
    seen_path = DATA_DIR / f"seen_broad_{run_date}.csv"
    return snapshot_path, seen_path


def lead_file_path(person: str, run_date: str) -> Path:
    return LEADS_DIR / f"{person.strip().lower()}_broad_leads_{run_date}.csv"


@st.cache_data(show_spinner=False)
def load_results_csv(path_str: str, mtime: float) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    try:
        df = pd.read_csv(path, dtype="string").fillna("")
    except EmptyDataError:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    for col in RESULT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[RESULT_COLUMNS]


@st.cache_data(show_spinner=False)
def load_leads_csv(path_str: str, mtime: float) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=LEAD_COLUMNS)

    try:
        df = pd.read_csv(path, dtype="string", on_bad_lines="skip").fillna("")
    except (EmptyDataError, ParserError):
        return pd.DataFrame(columns=LEAD_COLUMNS)
    except Exception:
        return pd.DataFrame(columns=LEAD_COLUMNS)

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
    for transient_col in ["is_tech_biotech", "tech_biotech_international_match"]:
        if transient_col in save_df.columns:
            save_df = save_df.drop(columns=[transient_col])

    save_df.to_csv(snapshot_path, index=False)
    save_df.to_csv(seen_path, index=False)
    load_results_csv.clear()


def clear_today_results(run_date: str, selected_user: str) -> None:
    snapshot_path, seen_path = get_store_paths(run_date)
    lead_path = lead_file_path(selected_user, run_date)

    for path in [snapshot_path, seen_path, lead_path]:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    load_results_csv.clear()
    load_leads_csv.clear()
    fetch_companies_incorporated_today.clear()
    fetch_director_signals_cached.clear()
    fetch_psc_signals_cached.clear()
    fetch_psc_statement_signals_cached.clear()

    st.session_state["latest_df"] = pd.DataFrame(columns=RESULT_COLUMNS)
    st.session_state["sorted_df"] = pd.DataFrame(columns=RESULT_COLUMNS)
    st.session_state["new_df"] = pd.DataFrame(columns=RESULT_COLUMNS)
    st.session_state["last_refresh"] = "Cleared - waiting for refresh"

    st.rerun()


def add_company_to_leads(
    person: str,
    run_date: str,
    row: pd.Series,
    existing_leads: pd.DataFrame,
) -> bool:
    path = lead_file_path(person, run_date)
    company_number = str(row.get("company_number", "")).strip()
    if not company_number:
        return False

    existing_numbers = set(existing_leads["company_number"].astype(str)) if not existing_leads.empty else set()
    if company_number in existing_numbers:
        return False

    new_row = pd.DataFrame([{
        **{col: str(row.get(col, "")).strip() for col in RESULT_COLUMNS},
        "added_by": person,
        "added_at": now_uk_str(),
    }], columns=LEAD_COLUMNS)

    file_exists = path.exists() and path.stat().st_size > 0
    new_row.to_csv(path, mode="a", index=False, header=not file_exists)
    load_leads_csv.clear()
    return True


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if df.empty:
        df["is_tech_biotech"] = pd.Series(dtype="bool")
        df["tech_biotech_international_match"] = pd.Series(dtype="bool")
        return df

    df["sector"] = df["sector"].fillna("").astype(str)
    df["any_international_signal"] = df["any_international_signal"].fillna("No").astype(str)
    df["match_confidence"] = df["match_confidence"].fillna("Low").astype(str)

    df["is_tech_biotech"] = df["sector"].str.split(",").apply(
        lambda parts: bool({p.strip() for p in parts if p.strip()} & TECH_BIOTECH_CODES)
    )

    df["tech_biotech_international_match"] = (
        df["is_tech_biotech"] &
        df["any_international_signal"].str.strip().str.lower().eq("yes")
    )

    return df


def split_result_tables(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        empty_df = pd.DataFrame(columns=df.columns)
        return empty_df, empty_df

    tech_biotech_df = df[df["is_tech_biotech"]].reset_index(drop=True)
    matched_df = df[~df["is_tech_biotech"]].reset_index(drop=True)
    return tech_biotech_df, matched_df


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
        existing_lookup = pd.DataFrame(columns=[c for c in RESULT_COLUMNS if c != "company_number"])
        existing_numbers = set()
    else:
        tmp = existing_df.copy()
        tmp["company_number"] = tmp["company_number"].astype(str).str.strip()
        tmp["pull_order"] = pd.to_numeric(tmp["pull_order"], errors="coerce").fillna(-1).astype(int)
        tmp = tmp.drop_duplicates(subset=["company_number"], keep="first")
        existing_lookup = tmp.set_index("company_number")
        existing_numbers = set(existing_lookup.index)

    known_mask = screened_df["company_number"].isin(existing_numbers)

    if known_mask.any():
        for col in [c for c in RESULT_COLUMNS if c != "company_number"]:
            if col in existing_lookup.columns:
                screened_df.loc[known_mask, col] = (
                    screened_df.loc[known_mask, "company_number"].map(existing_lookup[col]).fillna(screened_df.loc[known_mask, col])
                )

    new_mask = ~known_mask
    if new_mask.any():
        all_new_company_numbers = screened_df.loc[new_mask, "company_number"].tolist()
        company_numbers_to_process = all_new_company_numbers[:MAX_NEW_COMPANIES_PER_REFRESH]

        progress = st.progress(0, text=f"Screening {len(company_numbers_to_process)} new companies...")
        flags_lookup = {}

        for i, company_number in enumerate(company_numbers_to_process, start=1):
            try:
                (
                    director_target_match,
                    director_target_countries,
                    director_non_uk_signal,
                    director_uk_present,
                ) = fetch_director_signals_cached(company_number, tuple(api_keys))

                (
                    psc_individual_target_match,
                    psc_individual_target_countries,
                    psc_individual_uk_present,
                    psc_entity_target_match,
                    psc_entity_target_countries,
                    psc_entity_non_uk_signal,
                    psc_statement_present,
                    psc_statement_types,
                ) = fetch_psc_signals_cached(company_number, tuple(api_keys))

                temp_row = pd.Series({
                    "director_target_country_match": director_target_match,
                    "director_target_countries": director_target_countries,
                    "director_non_uk_signal": director_non_uk_signal,
                    "director_uk_present": director_uk_present,
                    "psc_individual_target_match": psc_individual_target_match,
                    "psc_individual_target_countries": psc_individual_target_countries,
                    "psc_individual_uk_present": psc_individual_uk_present,
                    "psc_entity_target_match": psc_entity_target_match,
                    "psc_entity_target_countries": psc_entity_target_countries,
                    "psc_entity_non_uk_signal": psc_entity_non_uk_signal,
                    "psc_entity_uk_present": "Yes" if (
                        psc_entity_target_match == "Yes" and
                        any(clean_country(x) in UK_COUNTRY_ALIASES for x in psc_entity_target_countries.split(", "))
                    ) else "No",
                    "psc_statement_present": psc_statement_present,
                    "psc_statement_types": psc_statement_types,
                })

                (any_target_country_signal, any_international_signal), confidence = compute_summary_signals(temp_row)

                flags_lookup[company_number] = {
                    "director_target_country_match": director_target_match,
                    "director_target_countries": director_target_countries,
                    "director_non_uk_signal": director_non_uk_signal,
                    "director_uk_present": director_uk_present,
                    "psc_individual_target_match": psc_individual_target_match,
                    "psc_individual_target_countries": psc_individual_target_countries,
                    "psc_individual_uk_present": psc_individual_uk_present,
                    "psc_entity_target_match": psc_entity_target_match,
                    "psc_entity_target_countries": psc_entity_target_countries,
                    "psc_entity_non_uk_signal": psc_entity_non_uk_signal,
                    "psc_entity_uk_present": temp_row["psc_entity_uk_present"],
                    "psc_statement_present": psc_statement_present,
                    "psc_statement_types": psc_statement_types,
                    "any_target_country_signal": any_target_country_signal,
                    "any_international_signal": any_international_signal,
                    "match_confidence": confidence,
                }
            except Exception:
                flags_lookup[company_number] = {
                    "director_target_country_match": "No",
                    "director_target_countries": "",
                    "director_non_uk_signal": "No",
                    "director_uk_present": "No",
                    "psc_individual_target_match": "No",
                    "psc_individual_target_countries": "",
                    "psc_individual_uk_present": "No",
                    "psc_entity_target_match": "No",
                    "psc_entity_target_countries": "",
                    "psc_entity_non_uk_signal": "No",
                    "psc_entity_uk_present": "No",
                    "psc_statement_present": "No",
                    "psc_statement_types": "",
                    "any_target_country_signal": "No",
                    "any_international_signal": "No",
                    "match_confidence": "Low",
                }

            progress.progress(i / len(company_numbers_to_process), text=f"Screening {i}/{len(company_numbers_to_process)} new companies...")

        progress.empty()

        process_mask = screened_df["company_number"].isin(company_numbers_to_process)
        for col in [
            "director_target_country_match",
            "director_target_countries",
            "director_non_uk_signal",
            "director_uk_present",
            "psc_individual_target_match",
            "psc_individual_target_countries",
            "psc_individual_uk_present",
            "psc_entity_target_match",
            "psc_entity_target_countries",
            "psc_entity_non_uk_signal",
            "psc_entity_uk_present",
            "psc_statement_present",
            "psc_statement_types",
            "any_target_country_signal",
            "any_international_signal",
            "match_confidence",
        ]:
            screened_df.loc[process_mask, col] = screened_df.loc[process_mask, "company_number"].map(
                lambda cn: flags_lookup.get(cn, {}).get(col, screened_df.loc[screened_df["company_number"] == cn, col].iloc[0])
            )

    screened_df["pull_order"] = pd.to_numeric(screened_df["pull_order"], errors="coerce").fillna(-1).astype(int)

    return (
        screened_df
        .drop_duplicates(subset=["company_number"], keep="first")
        .reset_index(drop=True)
    )


def render_quick_add(
    df: pd.DataFrame,
    person: str,
    run_date: str,
    existing_leads: pd.DataFrame,
) -> None:
    st.subheader(f"Quick add to {person}'s leads")

    if df.empty:
        st.info("No companies available to add.")
        return

    existing_numbers = set(existing_leads["company_number"].astype(str)) if not existing_leads.empty else set()

    for idx, row in enumerate(df.itertuples(index=False)):
        company_number = str(row.company_number).strip()
        already_added = company_number in existing_numbers

        c1, c2, c3, c4, c5, c6, c7 = st.columns([4.0, 1.1, 1.4, 1.4, 1.6, 1.6, 0.9])
        c1.write(f"**{row.company_name}**")
        c2.write(str(row.sector))
        c3.write(str(row.any_target_country_signal))
        c4.write(str(row.any_international_signal))
        c5.write(str(row.match_confidence))
        c6.write(str(row.time_added_to_table))

        if already_added:
            c7.caption("Added")
        else:
            if c7.button("Add", key=f"add_{person}_{company_number}_{idx}"):
                added = add_company_to_leads(
                    person,
                    run_date,
                    pd.Series({col: getattr(row, col) for col in RESULT_COLUMNS}),
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
    show_target_only = st.sidebar.checkbox("Show only target-country signals", value=True)
    show_international_only = st.sidebar.checkbox("Show only international signals", value=False)
    confidence_filter = st.sidebar.multiselect(
        "Confidence",
        ["High", "Medium", "Low"],
        default=["High", "Medium", "Low"],
    )

    refresh = st.sidebar.button("Refresh now", type="primary")

    st.sidebar.subheader("Reset")
    if st.sidebar.button("Clear today's results and start again", type="secondary"):
        clear_today_results(run_date, selected_user)

    if refresh or not snapshot_path.exists():
        with st.spinner("Refreshing Companies House data with broad director + PSC screening..."):
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

        st.session_state["latest_df"] = current_df
        st.session_state["sorted_df"] = get_sorted_current_df(current_df)
        st.session_state.setdefault("new_df", pd.DataFrame(columns=RESULT_COLUMNS))
        st.session_state.setdefault("last_refresh", "Not refreshed in this session")

    current_df = st.session_state.get("latest_df", pd.DataFrame(columns=RESULT_COLUMNS))
    sorted_df = st.session_state.get("sorted_df", pd.DataFrame(columns=RESULT_COLUMNS))
    leads_df = load_leads(selected_user, run_date)

    if not sorted_df.empty:
        if show_target_only:
            sorted_df = sorted_df[sorted_df["any_target_country_signal"].astype(str).str.lower() == "yes"]
        if show_international_only:
            sorted_df = sorted_df[sorted_df["any_international_signal"].astype(str).str.lower() == "yes"]
        if confidence_filter:
            sorted_df = sorted_df[sorted_df["match_confidence"].isin(confidence_filter)]

    tech_biotech_df, matched_df = split_result_tables(sorted_df)

    total_pulled = int(len(current_df))
    total_target_signal = int((current_df["any_target_country_signal"].astype(str).str.lower() == "yes").sum()) if not current_df.empty else 0
    total_international_signal = int((current_df["any_international_signal"].astype(str).str.lower() == "yes").sum()) if not current_df.empty else 0
    total_leads = int(len(leads_df)) if not leads_df.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total pulled today", total_pulled)
    c2.metric("Any target-country signal", total_target_signal)
    c3.metric("Any international signal", total_international_signal)
    c4.metric(f"{selected_user}'s leads today", total_leads)

    st.caption(
        f"Working as {selected_user} | Last refresh: {st.session_state.get('last_refresh', 'Unknown')}"
    )

    newest_df = matched_df.head(QUICK_ADD_DEFAULT).reset_index(drop=True)
    render_quick_add(newest_df, selected_user, run_date, leads_df)

    display_columns = [
        "company_name",
        "sector",
        "director_target_country_match",
        "director_target_countries",
        "director_non_uk_signal",
        "psc_individual_target_match",
        "psc_individual_target_countries",
        "psc_entity_target_match",
        "psc_entity_target_countries",
        "psc_entity_non_uk_signal",
        "psc_statement_present",
        "psc_statement_types",
        "any_target_country_signal",
        "any_international_signal",
        "match_confidence",
        "time_added_to_table",
    ]

    with st.expander("Tech & Biotech Leads", expanded=True):
        if tech_biotech_df.empty:
            st.info("No tech or biotech leads to show yet.")
        else:
            tech_display = tech_biotech_df[display_columns].rename(columns={
                "company_name": "Company Name",
                "sector": "SIC Code(s)",
                "director_target_country_match": "Director Target Match",
                "director_target_countries": "Director Target Countries",
                "director_non_uk_signal": "Director Non-UK Signal",
                "psc_individual_target_match": "PSC Individual Target Match",
                "psc_individual_target_countries": "PSC Individual Target Countries",
                "psc_entity_target_match": "PSC Entity Target Match",
                "psc_entity_target_countries": "PSC Entity Target Countries",
                "psc_entity_non_uk_signal": "PSC Entity Non-UK Signal",
                "psc_statement_present": "PSC Statement Present",
                "psc_statement_types": "PSC Statement Types",
                "any_target_country_signal": "Any Target-Country Signal",
                "any_international_signal": "Any International Signal",
                "match_confidence": "Match Confidence",
                "time_added_to_table": "Time Added To Table",
            })
            st.dataframe(tech_display, use_container_width=True, hide_index=True)

    with st.expander("Matched Results", expanded=False):
        if matched_df.empty:
            st.info("No matched companies to show yet.")
        else:
            matched_display = matched_df[display_columns].rename(columns={
                "company_name": "Company Name",
                "sector": "SIC Code(s)",
                "director_target_country_match": "Director Target Match",
                "director_target_countries": "Director Target Countries",
                "director_non_uk_signal": "Director Non-UK Signal",
                "psc_individual_target_match": "PSC Individual Target Match",
                "psc_individual_target_countries": "PSC Individual Target Countries",
                "psc_entity_target_match": "PSC Entity Target Match",
                "psc_entity_target_countries": "PSC Entity Target Countries",
                "psc_entity_non_uk_signal": "PSC Entity Non-UK Signal",
                "psc_statement_present": "PSC Statement Present",
                "psc_statement_types": "PSC Statement Types",
                "any_target_country_signal": "Any Target-Country Signal",
                "any_international_signal": "Any International Signal",
                "match_confidence": "Match Confidence",
                "time_added_to_table": "Time Added To Table",
            })
            st.dataframe(matched_display, use_container_width=True, hide_index=True)

    with st.expander("Today's results CSV", expanded=False):
        if current_df.empty:
            st.info("No results available yet.")
        else:
            st.download_button(
                label="Download all current results CSV",
                data=current_df.to_csv(index=False).encode("utf-8"),
                file_name=f"broad_screen_results_{run_date}.csv",
                mime="text/csv",
                key="download_all_results_csv",
            )


if __name__ == "__main__":
    main()
