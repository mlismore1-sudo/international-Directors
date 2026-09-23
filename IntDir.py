import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Companies House New Incorporations Screener",
    page_icon="🏢",
    layout="wide",
)

BASE_URL = "https://api.company-information.service.gov.uk"
DB_PATH = "companies_house_screening.db"
SEARCH_PAGE_SIZE = 100
OFFICERS_PAGE_SIZE = 100
PSC_PAGE_SIZE = 100
MAX_SEARCH_PAGES = 500

ALLOWED_SIC_CODES = [
    "62012", "62020", "63120", "47910", "46190", "46499", "70229", "73110", "74909", "68209",
    "64209", "68100", "32990", "10890", "86900", "93130", "96040", "82990", "72110", "56101",
]
TARGET_SIC_CODES = {"62012", "72110", "56101"}
BONUS_STAR_COUNTRIES = {"sweden", "norway", "united states"}
ALLOWED_COMPANY_TYPES = [
    "ltd",
    "llp",
    "private-limited-guarant-nsc",
    "private-limited-shares-section-30-exemption",
]
COUNTRY_TERMS = {
    "usa", "united states", "united states of america", "france", "germany", "belgium", "norway",
    "sweden", "finland", "denmark", "austria", "poland", "spain", "portugal", "greece", "italy",
    "hungary", "croatia", "ireland", "china", "netherlands", "india", "hong kong", "singapore",
}
NATIONALITY_TO_COUNTRY = {
    "american": "united states",
    "us": "united states",
    "united states": "united states",
    "french": "france",
    "german": "germany",
    "belgian": "belgium",
    "norwegian": "norway",
    "swedish": "sweden",
    "finnish": "finland",
    "danish": "denmark",
    "austrian": "austria",
    "polish": "poland",
    "spanish": "spain",
    "portuguese": "portugal",
    "greek": "greece",
    "italian": "italy",
    "hungarian": "hungary",
    "croatian": "croatia",
    "irish": "ireland",
    "chinese": "china",
    "indian": "india",
    "hong kong": "hong kong",
    "hongkong": "hong kong",
    "singaporean": "singapore",
    "dutch": "netherlands",
    "netherlands": "netherlands",
}
COUNTRY_FLAG_MAP = {
    "united states": "🇺🇸",
    "france": "🇫🇷",
    "germany": "🇩🇪",
    "belgium": "🇧🇪",
    "norway": "🇳🇴",
    "sweden": "🇸🇪",
    "finland": "🇫🇮",
    "denmark": "🇩🇰",
    "austria": "🇦🇹",
    "poland": "🇵🇱",
    "spain": "🇪🇸",
    "portugal": "🇵🇹",
    "greece": "🇬🇷",
    "italy": "🇮🇹",
    "hungary": "🇭🇺",
    "croatia": "🇭🇷",
    "ireland": "🇮🇪",
    "china": "🇨🇳",
    "netherlands": "🇳🇱",
    "india": "🇮🇳",
    "hong kong": "🇭🇰",
    "singapore": "🇸🇬",
}
COMPANY_OWNER_KINDS = {
    "corporate-entity-person-with-significant-control",
    "legal-person-person-with-significant-control",
    "super-secure-person-with-significant-control",
}
SIGNAL_OPTIONS = ["International Director", "International Shareholder", "Owned By A Company"]


def apply_custom_css() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"][aria-expanded="true"] > div:first-child { width: 360px; }
        div[data-testid="metric-container"] {
            border: 1px solid rgba(120, 120, 120, 0.18);
            padding: 14px 16px;
            border-radius: 14px;
            background: linear-gradient(180deg, rgba(14,17,23,0.03), rgba(14,17,23,0.01));
        }
        .app-note {
            padding: 0.9rem 1rem;
            border-radius: 12px;
            border: 1px solid rgba(120, 120, 120, 0.18);
            background: rgba(49, 51, 63, 0.04);
            margin-bottom: 1rem;
        }
        .signal-legend { display: flex; gap: 10px; flex-wrap: wrap; margin: 0.5rem 0; }
        .signal-pill {
            border: 1px solid rgba(120, 120, 120, 0.2);
            border-radius: 999px;
            padding: 6px 10px;
            font-size: 0.85rem;
            background: rgba(49, 51, 63, 0.04);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "usa": "united states",
        "u s a": "united states",
        "united states of america": "united states",
        "the netherlands": "netherlands",
    }
    return aliases.get(text, text)


NORMALIZED_COUNTRY_TERMS = {normalize_text(value) for value in COUNTRY_TERMS}
NORMALIZED_ALLOWED_COMPANY_TYPES = {normalize_text(value) for value in ALLOWED_COMPANY_TYPES}


def canonical_country_from_value(value: Any) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    if normalized in NORMALIZED_COUNTRY_TERMS:
        return normalized
    return NATIONALITY_TO_COUNTRY.get(normalized, "")


def dedupe_preserve_order(values: List[str]) -> List[str]:
    output: List[str] = []
    seen: Set[str] = set()
    for value in values:
        normalized = normalize_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(value)
    return output


def country_label(country: str) -> str:
    if country == "united states":
        return "USA"
    if country == "hong kong":
        return "Hong Kong"
    return country.title()


def format_flagged_countries(values: List[str]) -> str:
    countries = dedupe_preserve_order([
        canonical_country_from_value(value)
        for value in values
        if canonical_country_from_value(value)
    ])
    return " | ".join(
        f"✓ {COUNTRY_FLAG_MAP.get(country, '🌍')} {country_label(country)}"
        for country in countries
    )


def make_company_profile_url(company_number: str, company_name: str) -> str:
    return (
        "https://find-and-update.company-information.service.gov.uk/company/"
        f"{company_number}#{quote(company_name or 'company')}"
    )


class CHClient:
    def __init__(self, api_keys: List[str]):
        self.api_keys = [key.strip() for key in api_keys if str(key).strip()]
        if not self.api_keys:
            raise ValueError("No Companies House API keys supplied.")
        self.key_index = 0
        self.session = requests.Session()

    def _auth(self) -> Tuple[str, str]:
        return self.api_keys[self.key_index % len(self.api_keys)], ""

    def _rotate_key(self) -> None:
        self.key_index = (self.key_index + 1) % len(self.api_keys)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_error = "Unknown request error"
        attempts = max(len(self.api_keys) * 3, 3)
        for _ in range(attempts):
            try:
                response = self.session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    auth=self._auth(),
                    timeout=30,
                    headers={"Accept": "application/json"},
                )
                if response.status_code == 404:
                    return {}
                if response.status_code in {401, 403, 429}:
                    last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    self._rotate_key()
                    time.sleep(1)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = str(exc)
                self._rotate_key()
                time.sleep(1)
        raise RuntimeError(f"Companies House API request failed after retries: {last_error}")


def safe_add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()
    except Exception:
        pass


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screened_companies (
            company_number TEXT PRIMARY KEY,
            company_name TEXT,
            sic_code TEXT,
            incorporation_date TEXT,
            company_type TEXT,
            international_director INTEGER,
            international_shareholder INTEGER,
            owned_by_company INTEGER,
            pulled_at TEXT,
            raw_json TEXT,
            director_count INTEGER DEFAULT 0
        )
        """
    )
    
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screening_runs (
            incorporation_date TEXT PRIMARY KEY,
            api_total_results INTEGER NOT NULL DEFAULT 0,
            stored_company_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'in_progress',
            last_page_start_index INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    safe_add_column(conn, "screened_companies", "international_director_detail", "TEXT")
    safe_add_column(conn, "screened_companies", "international_shareholder_detail", "TEXT")
    safe_add_column(conn, "screened_companies", "owner_company_name", "TEXT")
    safe_add_column(conn, "screened_companies", "profile_url", "TEXT")
    safe_add_column(conn, "screened_companies", "shortlisted", "INTEGER DEFAULT 0")
    safe_add_column(conn, "screened_companies", "target_sic", "INTEGER DEFAULT 0")
    safe_add_column(conn, "screened_companies", "director_count", "INTEGER DEFAULT 0")
    
    return conn


def utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def validate_api_keys() -> List[str]:
    if "COMPANIES_HOUSE_API_KEYS" not in st.secrets:
        raise ValueError("Missing COMPANIES_HOUSE_API_KEYS in .streamlit/secrets.toml")
    keys = [str(key).strip() for key in list(st.secrets["COMPANIES_HOUSE_API_KEYS"]) if str(key).strip()]
    if not keys:
        raise ValueError("COMPANIES_HOUSE_API_KEYS is empty")
    return keys


def get_run_state(conn: sqlite3.Connection, target_date: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT incorporation_date, api_total_results, stored_company_count, status,
               last_page_start_index, completed_at, updated_at
        FROM screening_runs
        WHERE incorporation_date = ?
        """,
        (target_date,),
    ).fetchone()
    if not row:
        return None
    columns = [
        "incorporation_date", "api_total_results", "stored_company_count", "status",
        "last_page_start_index", "completed_at", "updated_at",
    ]
    return dict(zip(columns, row))


def save_run_state(
    conn: sqlite3.Connection,
    target_date: str,
    api_total_results: int,
    stored_company_count: int,
    status: str,
    last_page_start_index: int,
    completed_at: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO screening_runs (
            incorporation_date, api_total_results, stored_company_count, status,
            last_page_start_index, completed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(incorporation_date) DO UPDATE SET
            api_total_results = excluded.api_total_results,
            stored_company_count = excluded.stored_company_count,
            status = excluded.status,
            last_page_start_index = excluded.last_page_start_index,
            completed_at = excluded.completed_at,
            updated_at = excluded.updated_at
        """,
        (
            target_date,
            api_total_results,
            stored_company_count,
            status,
            last_page_start_index,
            completed_at,
            utc_now(),
        ),
    )
    conn.commit()


def mark_date_complete(
    conn: sqlite3.Connection,
    target_date: str,
    api_total_results: int,
    stored_company_count: int,
) -> None:
    save_run_state(
        conn=conn,
        target_date=target_date,
        api_total_results=api_total_results,
        stored_company_count=stored_company_count,
        status="complete",
        last_page_start_index=api_total_results,
        completed_at=utc_now(),
    )


def get_screened_numbers_for_date(conn: sqlite3.Connection, target_date: str) -> Set[str]:
    rows = conn.execute(
        "SELECT company_number FROM screened_companies WHERE incorporation_date = ?",
        (target_date,),
    ).fetchall()
    return {row[0] for row in rows}


def count_screened_for_date(conn: sqlite3.Connection, target_date: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM screened_companies WHERE incorporation_date = ?",
        (target_date,),
    ).fetchone()[0])


def upsert_company(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO screened_companies (
            company_number, company_name, sic_code, incorporation_date, company_type,
            international_director, international_director_detail,
            international_shareholder, international_shareholder_detail,
            owned_by_company, owner_company_name, pulled_at, raw_json,
            profile_url, shortlisted, target_sic, director_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_number) DO UPDATE SET
            company_name = excluded.company_name,
            sic_code = excluded.sic_code,
            incorporation_date = excluded.incorporation_date,
            company_type = excluded.company_type,
            international_director = excluded.international_director,
            international_director_detail = excluded.international_director_detail,
            international_shareholder = excluded.international_shareholder,
            international_shareholder_detail = excluded.international_shareholder_detail,
            owned_by_company = excluded.owned_by_company,
            owner_company_name = excluded.owner_company_name,
            pulled_at = excluded.pulled_at,
            raw_json = excluded.raw_json,
            profile_url = excluded.profile_url,
            target_sic = excluded.target_sic,
            director_count = excluded.director_count
        """,
        (
            row["company_number"],
            row["company_name"],
            row["sic_code"],
            row["incorporation_date"],
            row["company_type"],
            int(row["international_director"]),
            row.get("international_director_detail", ""),
            int(row["international_shareholder"]),
            row.get("international_shareholder_detail", ""),
            int(row["owned_by_company"]),
            row.get("owner_company_name", ""),
            row["pulled_at"],
            json.dumps(row.get("raw_json", {})),
            row.get("profile_url", ""),
            int(row.get("shortlisted", False)),
            int(row.get("target_sic", False)),
            int(row.get("director_count", 0)),
        ),
    )
    conn.commit()


def set_shortlisted_state(conn: sqlite3.Connection, company_number: str, shortlisted: bool) -> None:
    conn.execute(
        "UPDATE screened_companies SET shortlisted = ? WHERE company_number = ?",
        (int(shortlisted), company_number),
    )
    conn.commit()


def read_db_rows(conn: sqlite3.Connection, start_date: str, end_date: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT *
        FROM screened_companies
        WHERE incorporation_date BETWEEN ? AND ?
        ORDER BY incorporation_date DESC, pulled_at DESC
        """,
        conn,
        params=(start_date, end_date),
    )


def get_range_run_status(conn: sqlite3.Connection, start_date: str, end_date: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT incorporation_date, api_total_results, stored_company_count, status,
               last_page_start_index, completed_at, updated_at
        FROM screening_runs
        WHERE incorporation_date BETWEEN ? AND ?
        ORDER BY incorporation_date
        """,
        conn,
        params=(start_date, end_date),
    )


def is_allowed_company_type(value: Any) -> bool:
    return normalize_text(value) in NORMALIZED_ALLOWED_COMPANY_TYPES


def request_search_page(client: CHClient, target_date: str, start_index: int) -> Dict[str, Any]:
    return client.get(
        "/advanced-search/companies",
        params={
            "incorporated_from": target_date,
            "incorporated_to": target_date,
            "company_status": "active",
            "company_type": ",".join(ALLOWED_COMPANY_TYPES),
            "sic_codes": ",".join(ALLOWED_SIC_CODES),
            "start_index": start_index,
            "size": SEARCH_PAGE_SIZE,
        },
    )


def get_all_officers(client: CHClient, company_number: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start_index = 0
    while True:
        payload = client.get(
            f"/company/{company_number}/officers",
            params={"start_index": start_index, "items_per_page": OFFICERS_PAGE_SIZE},
        )
        batch = payload.get("items", []) or []
        items.extend(batch)
        total = int(payload.get("total_results", len(items)) or len(items))
        if not batch or start_index + OFFICERS_PAGE_SIZE >= total:
            break
        start_index += OFFICERS_PAGE_SIZE
    return items


def get_all_pscs(client: CHClient, company_number: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start_index = 0
    while True:
        payload = client.get(
            f"/company/{company_number}/persons-with-significant-control",
            params={"start_index": start_index, "items_per_page": PSC_PAGE_SIZE},
        )
        batch = payload.get("items", []) or []
        items.extend(batch)
        total = int(payload.get("total_results", len(items)) or len(items))
        if not batch or start_index + PSC_PAGE_SIZE >= total:
            break
        start_index += PSC_PAGE_SIZE
    return items


def collect_international_director_details(client: CHClient, company_number: str) -> Tuple[bool, List[str], int]:
    countries: List[str] = []
    director_count = 0
    for officer in get_all_officers(client, company_number):
        role = normalize_text(officer.get("officer_role"))
        if "director" not in role and role != "designated member":
            continue
        director_count += 1
        for value in (
            officer.get("country_of_residence"),
            (officer.get("address") or {}).get("country"),
            officer.get("nationality"),
        ):
            if canonical_country_from_value(value):
                countries.append(str(value))
    countries = dedupe_preserve_order(countries)
    return bool(countries), countries, director_count


def analyse_psc_flags(client: CHClient, company_number: str) -> Tuple[bool, List[str], bool, List[str]]:
    shareholder_countries: List[str] = []
    owner_names: List[str] = []
    for psc in get_all_pscs(client, company_number):
        kind = str(psc.get("kind", ""))
        for value in (
            psc.get("country_of_residence"),
            (psc.get("address") or {}).get("country"),
            psc.get("nationality"),
        ):
            if canonical_country_from_value(value):
                shareholder_countries.append(str(value))
        if kind in COMPANY_OWNER_KINDS or "corporate" in kind or "legal-person" in kind:
            owner_name = str(psc.get("name") or "").strip()
            if owner_name:
                owner_names.append(owner_name)
    shareholder_countries = dedupe_preserve_order(shareholder_countries)
    owner_names = dedupe_preserve_order(owner_names)
    return bool(shareholder_countries), shareholder_countries, bool(owner_names), owner_names


def parse_matching_sic(item: Dict[str, Any]) -> str:
    item_sics = [str(code) for code in (item.get("sic_codes") or [])]
    matching_codes = [code for code in item_sics if code in ALLOWED_SIC_CODES]
    return ", ".join(matching_codes or item_sics[:1])


def is_target_sic(item: Dict[str, Any]) -> bool:
    return any(str(code) in TARGET_SIC_CODES for code in (item.get("sic_codes") or []))


def has_bonus_star(values: List[str]) -> bool:
    countries = {canonical_country_from_value(value) for value in values}
    return bool(countries & BONUS_STAR_COUNTRIES)


def build_rating(
    international_director: bool,
    international_shareholder: bool,
    owned_by_company: bool,
    target_sic: bool,
    director_details: List[str],
    shareholder_details: List[str],
) -> str:
    score = sum([
        international_director,
        international_shareholder,
        owned_by_company,
        target_sic,
        has_bonus_star(director_details) or has_bonus_star(shareholder_details),
    ])
    return "⭐" * score


def process_company(client: CHClient, item: Dict[str, Any], incorporation_date: str) -> Dict[str, Any]:
    company_number = str(item.get("company_number") or "")
    company_name = str(item.get("company_name") or item.get("title") or "")
    international_director, director_details, director_count = collect_international_director_details(client, company_number)
    international_shareholder, shareholder_details, owned_by_company, owner_names = analyse_psc_flags(client, company_number)
    return {
        "company_number": company_number,
        "company_name": company_name,
        "sic_code": parse_matching_sic(item),
        "incorporation_date": incorporation_date,
        "company_type": item.get("company_type", ""),
        "international_director": international_director,
        "international_director_detail": format_flagged_countries(director_details),
        "international_shareholder": international_shareholder,
        "international_shareholder_detail": format_flagged_countries(shareholder_details),
        "owned_by_company": owned_by_company,
        "owner_company_name": " | ".join(f"✓ {name}" for name in owner_names),
        "pulled_at": utc_now(),
        "raw_json": item,
        "profile_url": make_company_profile_url(company_number, company_name),
        "shortlisted": False,
        "target_sic": is_target_sic(item),
        "director_count": director_count,
    }


def screen_date_until_complete(
    client: CHClient,
    conn: sqlite3.Connection,
    target_date: str,
    log: Any,
    progress: Any,
) -> Dict[str, int]:
    prior_state = get_run_state(conn, target_date)
    if prior_state and prior_state["status"] == "complete":
        return {
            "api_total": int(prior_state["api_total_results"]),
            "new_companies": 0,
            "skipped_companies": int(prior_state["stored_company_count"]),
            "pages": 0,
            "complete": 1,
        }

    screened_numbers = get_screened_numbers_for_date(conn, target_date)
    start_index = 0
    pages = 0
    api_total: Optional[int] = None
    new_companies = 0
    skipped_companies = 0

    while pages < MAX_SEARCH_PAGES:
        payload = request_search_page(client, target_date, start_index)
        batch = payload.get("items", []) or []
        api_total = int(payload.get("total_results", 0) or 0)

        if not batch:
            mark_date_complete(conn, target_date, api_total, count_screened_for_date(conn, target_date))
            break

        pages += 1
        page_new = 0
        page_skipped = 0

        for item in batch:
            company_number = str(item.get("company_number") or "")
            if not company_number:
                continue
            if company_number in screened_numbers:
                page_skipped += 1
                skipped_companies += 1
                continue
            try:
                row = process_company(client, item, target_date)
                upsert_company(conn, row)
                screened_numbers.add(company_number)
                page_new += 1
                new_companies += 1
            except Exception as exc:
                log.warning(f"{target_date} - {company_number} could not be enriched: {exc}")

        next_index = start_index + len(batch)
        stored_count = count_screened_for_date(conn, target_date)
        is_last_page = len(batch) < SEARCH_PAGE_SIZE or next_index >= api_total
        save_run_state(
            conn=conn,
            target_date=target_date,
            api_total_results=api_total,
            stored_company_count=stored_count,
            status="complete" if is_last_page else "in_progress",
            last_page_start_index=next_index,
            completed_at=utc_now() if is_last_page else None,
        )
        log.write(
            f"{target_date} - page {pages}: API rows {start_index + 1:,}-{next_index:,} of {api_total:,}; "
            f"new {page_new:,}; already screened {page_skipped:,}."
        )
        progress.progress(min(next_index / max(api_total, 1), 1.0))

        if is_last_page:
            break

        start_index = next_index
        time.sleep(0.15)

    if pages >= MAX_SEARCH_PAGES:
        raise RuntimeError(f"Stopped after {MAX_SEARCH_PAGES} pages for {target_date}; date was not marked complete.")

    return {
        "api_total": int(api_total or 0),
        "new_companies": new_companies,
        "skipped_companies": skipped_companies,
        "pages": pages,
        "complete": 1,
    }


def build_display_df(db_df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "Shortlist", "Incorporated", "Target SIC", "Rating", "Directors", "Company Name", "SIC Code",
        "Signals", "International Director", "International Shareholder", "Owned By A Company",
        "Profile", "Pulled At", "company_number",
    ]
    if db_df.empty:
        return pd.DataFrame(columns=columns)

    records: List[Dict[str, Any]] = []
    for _, row in db_df.iterrows():
        international_director = bool(int(row.get("international_director", 0) or 0))
        international_shareholder = bool(int(row.get("international_shareholder", 0) or 0))
        owned_by_company = bool(int(row.get("owned_by_company", 0) or 0))
        target_sic = bool(int(row.get("target_sic", 0) or 0))
        signals = []
        if international_director:
            signals.append("Director 🌍")
        if international_shareholder:
            signals.append("Shareholder 🌍")
        if owned_by_company:
            signals.append("Company owner 🏢")
        director_details = [part.strip() for part in str(row.get("international_director_detail", "")).split("|") if part.strip()]
        shareholder_details = [part.strip() for part in str(row.get("international_shareholder_detail", "")).split("|") if part.strip()]
        records.append({
            "Shortlist": bool(int(row.get("shortlisted", 0) or 0)),
            "Incorporated": row.get("incorporation_date", ""),
            "Target SIC": "🎯" if target_sic else "",
            "Rating": build_rating(
                international_director,
                international_shareholder,
                owned_by_company,
                target_sic,
                director_details,
                shareholder_details,
            ),
            "Directors": int(row.get("director_count", 0) or 0),
            "Company Name": row.get("company_name", ""),
            "SIC Code": row.get("sic_code", ""),
            "Signals": " · ".join(signals),
            "International Director": row.get("international_director_detail", "") or "",
            "International Shareholder": row.get("international_shareholder_detail", "") or "",
            "Owned By A Company": row.get("owner_company_name", "") or "",
            "Profile": row.get("profile_url", "") or "",
            "Pulled At": row.get("pulled_at", ""),
            "company_number": row.get("company_number", ""),
        })
    return pd.DataFrame(records, columns=columns)


def apply_filters(
    df: pd.DataFrame,
    only_flagged: bool,
    selected_signals: List[str],
    sic_search: str,
    company_name_search: str,
    shortlisted_only: bool,
    min_directors: int,
    max_directors: int,
) -> pd.DataFrame:
    filtered = df.copy()
    if shortlisted_only:
        filtered = filtered[filtered["Shortlist"]]
    if only_flagged:
        mask = pd.Series(False, index=filtered.index)
        if "International Director" in selected_signals:
            mask |= filtered["International Director"].astype(str).str.startswith("✓", na=False)
        if "International Shareholder" in selected_signals:
            mask |= filtered["International Shareholder"].astype(str).str.startswith("✓", na=False)
        if "Owned By A Company" in selected_signals:
            mask |= filtered["Owned By A Company"].astype(str).str.startswith("✓", na=False)
        filtered = filtered[mask]
    if sic_search.strip():
        filtered = filtered[filtered["SIC Code"].astype(str).str.contains(re.escape(sic_search.strip()), case=False, na=False)]
    if company_name_search.strip():
        filtered = filtered[filtered["Company Name"].astype(str).str.contains(re.escape(company_name_search.strip()), case=False, na=False)]
    return filtered[(filtered["Directors"] >= min_directors) & (filtered["Directors"] <= max_directors)].copy()


def render_kpis(display_df: pd.DataFrame) -> None:
    total = len(display_df)
    flagged = 0 if display_df.empty else int((
        display_df["International Director"].astype(str).str.startswith("✓", na=False)
        | display_df["International Shareholder"].astype(str).str.startswith("✓", na=False)
        | display_df["Owned By A Company"].astype(str).str.startswith("✓", na=False)
    ).sum())
    international_directors = 0 if display_df.empty else int(display_df["International Director"].astype(str).str.startswith("✓", na=False).sum())
    international_shareholders = 0 if display_df.empty else int(display_df["International Shareholder"].astype(str).str.startswith("✓", na=False).sum())
    target_sics = 0 if display_df.empty else int(display_df["Target SIC"].eq("🎯").sum())
    shortlisted = 0 if display_df.empty else int(display_df["Shortlist"].sum())
    average_directors = 0 if display_df.empty else round(float(display_df["Directors"].mean()), 1)
    cols = st.columns(7)
    cols[0].metric("Total Results", f"{total:,}")
    cols[1].metric("Flagged Rows", f"{flagged:,}")
    cols[2].metric("Intl Directors", f"{international_directors:,}")
    cols[3].metric("Intl Shareholders", f"{international_shareholders:,}")
    cols[4].metric("Target SICs", f"{target_sics:,}")
    cols[5].metric("Shortlisted", f"{shortlisted:,}")
    cols[6].metric("Avg Directors", str(average_directors))


def render_sidebar(default_start: date, default_end: date):
    with st.sidebar:
        st.header("Screening controls")
        selected_range = st.date_input(
            "Incorporation date range",
            value=(default_start, default_end),
            format="YYYY-MM-DD",
        )
        if isinstance(selected_range, tuple) and len(selected_range) == 2:
            start_date, end_date = selected_range
        else:
            start_date = end_date = selected_range
        run = st.button("Start / resume screening", type="primary", use_container_width=True)
        force_refresh = st.checkbox(
            "Re-screen completed dates",
            value=False,
        )
        st.divider()
        st.subheader("Result filters")
        only_flagged = st.checkbox("Show only flagged rows", value=False)
        selected_signals = st.multiselect("Signals", SIGNAL_OPTIONS, default=SIGNAL_OPTIONS)
        director_range = st.slider("Number of directors", min_value=0, max_value=20, value=(0, 20))
        sic_search = st.text_input("Filter by SIC code", placeholder="e.g. 62012")
        company_name_search = st.text_input("Filter by company name", placeholder="e.g. Labs")
        shortlisted_only = st.checkbox("Show shortlisted only", value=False)
    return start_date, end_date, run, force_refresh, selected_signals, sic_search, company_name_search, shortlisted_only, director_range[0], director_range[1]


def main() -> None:
    apply_custom_css()
    st.title("Companies House New Incorporations Screener")
    st.caption("Screen every Companies House search page for each selected incorporation date.")
    st.markdown(
        """
        <div class="app-note">
        <strong>How it works:</strong> The app screens all companies for each date in your range,
        saves progress continuously, and marks dates complete when done. Completed dates are skipped on subsequent runs.
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        api_keys = validate_api_keys()
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    conn = init_db()
    client = CHClient(api_keys)
    today = date.today()
    default_end = today - timedelta(days=1)
    default_start = default_end - timedelta(days=6)
    
    result = render_sidebar(default_start, default_end)
    start_date = result[0]
    end_date = result[1]
    run = result[2]
    force_refresh = result[3]
    selected_signals = result[4]
    sic_search = result[5]
    company_name_search = result[6]
    shortlisted_only = result[7]
    min_directors = result[8]
    max_directors = result[9]

    if start_date > end_date:
        st.error("The start date must be on or before the end date.")
        st.stop()

    start_date_str = start_date.isoformat()
    end_date_str = end_date.isoformat()
    date_label = start_date_str if start_date == end_date else f"{start_date_str} to {end_date_str}"

    st.subheader("Screening status")
    run_status = get_range_run_status(conn, start_date_str, end_date_str)
    if run_status.empty:
        st.info("No screening state saved for this date range yet.")
    else:
        status_display = run_status.rename(columns={
            "incorporation_date": "Date",
            "api_total_results": "API results",
            "stored_company_count": "Stored",
            "last_page_start_index": "Rows read",
            "completed_at": "Completed at",
            "updated_at": "Last updated",
        })
        st.dataframe(status_display, use_container_width=True, hide_index=True)

    if run:
        dates = []
        cursor = start_date
        while cursor <= end_date:
            dates.append(cursor.isoformat())
            cursor += timedelta(days=1)

        with st.status(f"Screening {date_label}...", expanded=True) as overall_status:
            log = st.empty()
            day_progress = st.progress(0)
            completed_dates = 0
            processed_dates = 0
            new_total = 0
            skipped_total = 0
            errors: List[str] = []

            for index, target_date in enumerate(dates, start=1):
                existing_state = get_run_state(conn, target_date)
                if existing_state and existing_state["status"] == "complete" and not force_refresh:
                    completed_dates += 1
                    log.write(
                        f"{target_date} - already complete; skipped "
                        f"({int(existing_state['stored_company_count']):,} stored of "
                        f"{int(existing_state['api_total_results']):,} API results)."
                    )
                    day_progress.progress(index / len(dates))
                    continue

                processed_dates += 1
                st.write(f"### {target_date}")
                per_date_progress = st.progress(0)
                try:
                    summary = screen_date_until_complete(client, conn, target_date, log, per_date_progress)
                    new_total += summary["new_companies"]
                    skipped_total += summary["skipped_companies"]
                    log.success(
                        f"{target_date} complete - {summary['pages']} page(s), "
                        f"{summary['api_total']:,} API results, {summary['new_companies']:,} newly enriched, "
                        f"{summary['skipped_companies']:,} already screened."
                    )
                except Exception as exc:
                    errors.append(f"{target_date}: {exc}")
                    log.error(f"{target_date} stopped with an error: {exc}")
                day_progress.progress(index / len(dates))

            if errors:
                overall_status.update(label="Screening stopped with some errors", state="error")
                st.error("\n".join(errors[:20]))
            else:
                overall_status.update(label="Selected date range is complete", state="complete")
                st.success(
                    f"Completed dates skipped: {completed_dates:,}; dates processed: {processed_dates:,}; "
                    f"new companies enriched: {new_total:,}; already-screened companies skipped: {skipped_total:,}."
                )

    db_df = read_db_rows(conn, start_date_str, end_date_str)
    display_df = build_display_df(db_df)
    render_kpis(display_df)

    st.markdown(
        """
        <div class="signal-legend">
            <div class="signal-pill">Director 🌍 = international director match</div>
            <div class="signal-pill">Shareholder 🌍 = international PSC match</div>
            <div class="signal-pill">Company owner 🏢 = corporate PSC match</div>
            <div class="signal-pill">Target SIC 🎯 = target SIC code</div>
            <div class="signal-pill">Rating ⭐ = signal-based lead score</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    filtered_df = apply_filters(
        display_df,
        only_flagged,
        selected_signals,
        sic_search,
        company_name_search,
        shortlisted_only,
        min_directors,
        max_directors,
    )

    results_tab, shortlist_tab, settings_tab = st.tabs(["Results", "Shortlist", "Settings"])
    with results_tab:
        st.caption(f"{date_label} - {len(filtered_df):,} visible rows after filters.")
        editor_columns = [
            "Shortlist", "Incorporated", "Target SIC", "Rating", "Directors", "Company Name", "SIC Code",
            "Signals", "International Director", "International Shareholder", "Owned By A Company",
            "Profile", "Pulled At", "company_number",
        ]
        edited_df = st.data_editor(
            filtered_df[editor_columns],
            key=f"results_editor_{start_date_str}_{end_date_str}",
            use_container_width=True,
            hide_index=True,
            disabled=[column for column in editor_columns if column not in {"Shortlist"}],
            column_config={
                "Shortlist": st.column_config.CheckboxColumn("Shortlist", help="Mark a company for follow-up."),
                "Directors": st.column_config.NumberColumn("Directors", width="small"),
                "Profile": st.column_config.LinkColumn("Profile", display_text="Open record", width="small"),
                "company_number": None,
            },
        )

        if not edited_df.empty:
            changes = edited_df[["company_number", "Shortlist"]].merge(
                display_df[["company_number", "Shortlist"]],
                on="company_number",
                suffixes=("_new", "_old"),
                how="left",
            )
            changed_rows = changes[changes["Shortlist_new"] != changes["Shortlist_old"]]
            for _, change in changed_rows.iterrows():
                set_shortlisted_state(conn, str(change["company_number"]), bool(change["Shortlist_new"]))
            if not changed_rows.empty:
                st.success(f"Updated shortlist status for {len(changed_rows):,} companies.")
                st.rerun()

        st.download_button(
            "Download filtered CSV",
            data=filtered_df.drop(columns=["company_number"], errors="ignore").to_csv(index=False).encode("utf-8"),
            file_name=f"companies_house_screening_{start_date_str}_to_{end_date_str}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with shortlist_tab:
        shortlist_df = display_df[display_df["Shortlist"]].copy()
        if shortlist_df.empty:
            st.info("No companies have been shortlisted yet.")
        else:
            st.dataframe(
                shortlist_df.drop(columns=["company_number"], errors="ignore"),
                use_container_width=True,
                hide_index=True,
                column_config={"Profile": st.column_config.LinkColumn("Profile", display_text="Open record")},
            )
            st.download_button(
                "Download shortlist CSV",
                data=shortlist_df.drop(columns=["company_number"], errors="ignore").to_csv(index=False).encode("utf-8"),
                file_name=f"companies_house_shortlist_{start_date_str}_to_{end_date_str}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with settings_tab:
        st.markdown(
            f"""
- API keys loaded: {len(api_keys)}
- Search page size: {SEARCH_PAGE_SIZE}
- Maximum search pages per date: {MAX_SEARCH_PAGES}
- Allowed SIC codes: {len(ALLOWED_SIC_CODES)}
- Target SIC codes: {', '.join(sorted(TARGET_SIC_CODES))}
            """
        )


if __name__ == "__main__":
    main()
