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

st.set_page_config(page_title="Companies House New Incorporations Screener", page_icon="🏢", layout="wide")

BASE_URL = "https://api.company-information.service.gov.uk"
DB_PATH = "companies_house_screening.db"
SEARCH_PAGE_SIZE = 100
OFFICERS_PAGE_SIZE = 100
PSC_PAGE_SIZE = 100
MAX_SEARCH_PAGES = 500

ALLOWED_SIC_CODES = ["62012", "62020", "63120", "47910", "46190", "46499", "70229", "73110", "74909", "68209", "64209", "68100", "32990", "10890", "86900", "93130", "96040", "82990", "72110", "56101"]
TARGET_SIC_CODES = {"62012", "72110", "56101"}
BONUS_STAR_COUNTRIES = {"sweden", "norway", "united states"}
ALLOWED_COMPANY_TYPES = ["ltd", "llp", "private-limited-guarant-nsc", "private-limited-shares-section-30-exemption"]

COUNTRY_TERMS = {"usa", "united states", "united states of america", "france", "germany", "belgium", "norway", "sweden", "finland", "denmark", "austria", "poland", "spain", "portugal", "greece", "italy", "hungary", "croatia", "ireland", "china", "netherlands", "india", "hong kong", "singapore"}

NATIONALITY_TO_COUNTRY = {
    "american": "united states", "us": "united states", "united states": "united states",
    "french": "france", "german": "germany", "belgian": "belgium", "norwegian": "norway",
    "swedish": "sweden", "finnish": "finland", "danish": "denmark", "austrian": "austria",
    "polish": "poland", "spanish": "spain", "portuguese": "portugal", "greek": "greece",
    "italian": "italy", "hungarian": "hungary", "croatian": "croatia", "irish": "ireland",
    "chinese": "china", "indian": "india", "hong kong": "hong kong", "hongkong": "hong kong",
    "singaporean": "singapore", "dutch": "netherlands", "netherlands": "netherlands",
}

COUNTRY_FLAG_MAP = {
    "united states": "🇺🇸", "france": "🇫🇷", "germany": "🇩🇪", "belgium": "🇧🇪",
    "norway": "🇳🇴", "sweden": "🇸🇪", "finland": "🇫🇮", "denmark": "🇩🇰",
    "austria": "🇦🇹", "poland": "🇵🇱", "spain": "🇪🇸", "portugal": "🇵🇹",
    "greece": "🇬🇷", "italy": "🇮🇹", "hungary": "🇭🇺", "croatia": "🇭🇷",
    "ireland": "🇮🇪", "china": "🇨🇳", "netherlands": "🇳🇱", "india": "🇮🇳",
    "hong kong": "🇭🇰", "singapore": "🇸🇬",
}

COMPANY_OWNER_KINDS = {"corporate-entity-person-with-significant-control", "legal-person-person-with-significant-control", "super-secure-person-with-significant-control"}
SIGNAL_OPTIONS = ["International Director", "International Shareholder", "Owned By A Company"]


def apply_custom_css():
    st.markdown("""
    <style>
    [data-testid="stSidebar"][aria-expanded="true"] > div:first-child { width: 360px; }
    div[data-testid="metric-container"] { border: 1px solid rgba(120,120,120,0.18); padding: 14px 16px; border-radius: 14px; background: linear-gradient(180deg, rgba(14,17,23,0.03), rgba(14,17,23,0.01)); }
    .app-note { padding: 0.9rem 1rem; border-radius: 12px; border: 1px solid rgba(120,120,120,0.18); background: rgba(49,51,63,0.04); margin-bottom: 1rem; }
    .signal-legend { display: flex; gap: 10px; flex-wrap: wrap; margin: 0.5rem 0; }
    .signal-pill { border: 1px solid rgba(120,120,120,0.2); border-radius: 999px; padding: 6px 10px; font-size: 0.85rem; background: rgba(49,51,63,0.04); }
    </style>
    """, unsafe_allow_html=True)


def normalize_text(value):
    if value is None:
        return ""
    text = str(value).strip().lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {"usa": "united states", "u s a": "united states", "united states of america": "united states", "the netherlands": "netherlands"}
    return aliases.get(text, text)


NORMALIZED_COUNTRY_TERMS = {normalize_text(v) for v in COUNTRY_TERMS}
NORMALIZED_ALLOWED_COMPANY_TYPES = {normalize_text(v) for v in ALLOWED_COMPANY_TYPES}


def canonical_country_from_value(value):
    normalized = normalize_text(value)
    if not normalized:
        return ""
    if normalized in NORMALIZED_COUNTRY_TERMS:
        return normalized
    return NATIONALITY_TO_COUNTRY.get(normalized, "")


def dedupe_preserve_order(values):
    output = []
    seen = set()
    for value in values:
        normalized = normalize_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(value)
    return output


def country_label(country):
    if country == "united states":
        return "USA"
    if country == "hong kong":
        return "Hong Kong"
    return country.title()


def format_flagged_countries(values):
    countries = dedupe_preserve_order([canonical_country_from_value(v) for v in values if canonical_country_from_value(v)])
    return " | ".join(f"✓ {COUNTRY_FLAG_MAP.get(c, '🌍')} {country_label(c)}" for c in countries)


def make_company_profile_url(company_number, company_name):
    return f"https://find-and-update.company-information.service.gov.uk/company/{company_number}#{quote(company_name or 'company')}"


class CHClient:
    def __init__(self, api_keys):
        self.api_keys = [k.strip() for k in api_keys if str(k).strip()]
        if not self.api_keys:
            raise ValueError("No Companies House API keys supplied.")
        self.key_index = 0
        self.session = requests.Session()

    def _auth(self):
        return (self.api_keys[self.key_index % len(self.api_keys)], "")

    def _rotate_key(self):
        self.key_index = (self.key_index + 1) % len(self.api_keys)

    def get(self, path, params=None):
        last_error = "Unknown request error"
        for _ in range(max(len(self.api_keys) * 3, 3)):
            try:
                response = self.session.get(f"{BASE_URL}{path}", params=params, auth=self._auth(), timeout=30, headers={"Accept": "application/json"})
                if response.status_code == 404:
                    return {}
                if response.status_code in {401, 403, 429}:
                    last_error = f"HTTP {response.status_code}"
                    self._rotate_key()
                    time.sleep(1)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = str(exc)
                self._rotate_key()
                time.sleep(1)
        raise RuntimeError(f"Companies House API request failed: {last_error}")


def safe_add_column(conn, table, column, definition):
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()
    except Exception:
        pass


def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS screened_companies (
        company_number TEXT PRIMARY KEY, company_name TEXT, sic_code TEXT, incorporation_date TEXT,
        company_type TEXT, international_director INTEGER, international_shareholder INTEGER,
        owned_by_company INTEGER, pulled_at TEXT, raw_json TEXT, director_count INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS screening_runs (
        incorporation_date TEXT PRIMARY KEY, api_total_results INTEGER NOT NULL DEFAULT 0,
        stored_company_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'in_progress',
        last_page_start_index INTEGER NOT NULL DEFAULT 0, completed_at TEXT, updated_at TEXT NOT NULL
    )""")
    conn.commit()
    for col in ["international_director_detail", "international_shareholder_detail", "owner_company_name", "profile_url", "shortlisted", "target_sic", "director_count"]:
        safe_add_column(conn, "screened_companies", col, "TEXT" if col not in ["shortlisted", "target_sic", "director_count"] else "INTEGER DEFAULT 0")
    return conn


def utc_now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def validate_api_keys():
    if "COMPANIES_HOUSE_API_KEYS" not in st.secrets:
        raise ValueError("Missing COMPANIES_HOUSE_API_KEYS in .streamlit/secrets.toml")
    keys = [str(k).strip() for k in list(st.secrets["COMPANIES_HOUSE_API_KEYS"]) if str(k).strip()]
    if not keys:
        raise ValueError("COMPANIES_HOUSE_API_KEYS is empty")
    return keys


def get_run_state(conn, target_date):
    row = conn.execute("SELECT incorporation_date, api_total_results, stored_company_count, status, last_page_start_index, completed_at, updated_at FROM screening_runs WHERE incorporation_date = ?", (target_date,)).fetchone()
    if not row:
        return None
    return dict(zip(["incorporation_date", "api_total_results", "stored_company_count", "status", "last_page_start_index", "completed_at", "updated_at"], row))


def save_run_state(conn, target_date, api_total_results, stored_company_count, status, last_page_start_index, completed_at=None):
    conn.execute("""INSERT INTO screening_runs (incorporation_date, api_total_results, stored_company_count, status, last_page_start_index, completed_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(incorporation_date) DO UPDATE SET
        api_total_results = excluded.api_total_results, stored_company_count = excluded.stored_company_count,
        status = excluded.status, last_page_start_index = excluded.last_page_start_index,
        completed_at = excluded.completed_at, updated_at = excluded.updated_at""",
        (target_date, api_total_results, stored_company_count, status, last_page_start_index, completed_at, utc_now()))
    conn.commit()


def mark_date_complete(conn, target_date, api_total_results, stored_company_count):
    save_run_state(conn, target_date, api_total_results, stored_company_count, "complete", api_total_results, utc_now())


def get_screened_numbers_for_date(conn, target_date):
    rows = conn.execute("SELECT company_number FROM screened_companies WHERE incorporation_date = ?", (target_date,)).fetchall()
    return {row[0] for row in rows}


def count_screened_for_date(conn, target_date):
    return int(conn.execute("SELECT COUNT(*) FROM screened_companies WHERE incorporation_date = ?", (target_date,)).fetchone()[0])


def upsert_company(conn, row):
    conn.execute("""INSERT INTO screened_companies (
        company_number, company_name, sic_code, incorporation_date, company_type,
        international_director, international_director_detail, international_shareholder,
        international_shareholder_detail, owned_by_company, owner_company_name, pulled_at,
        raw_json, profile_url, shortlisted, target_sic, director_count
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(company_number) DO UPDATE SET
        company_name = excluded.company_name, sic_code = excluded.sic_code, incorporation_date = excluded.incorporation_date,
        company_type = excluded.company_type, international_director = excluded.international_director,
        international_director_detail = excluded.international_director_detail,
        international_shareholder = excluded.international_shareholder,
        international_shareholder_detail = excluded.international_shareholder_detail,
        owned_by_company = excluded.owned_by_company, owner_company_name = excluded.owner_company_name,
        pulled_at = excluded.pulled_at, raw_json = excluded.raw_json, profile_url = excluded.profile_url,
        target_sic = excluded.target_sic, director_count = excluded.director_count""",
        (row["company_number"], row["company_name"], row["sic_code"], row["incorporation_date"], row["company_type"],
         int(row["international_director"]), row.get("international_director_detail", ""), int(row["international_shareholder"]),
         row.get("international_shareholder_detail", ""), int(row["owned_by_company"]), row.get("owner_company_name", ""),
         row["pulled_at"], json.dumps(row.get("raw_json", {})), row.get("profile_url", ""), int(row.get("shortlisted", False)),
         int(row.get("target_sic", False)), int(row.get("director_count", 0))))
    conn.commit()


def set_shortlisted_state(conn, company_number, shortlisted):
    conn.execute("UPDATE screened_companies SET shortlisted = ? WHERE company_number = ?", (int(shortlisted), company_number))
    conn.commit()


def read_db_rows(conn, start_date, end_date):
    return pd.read_sql_query("SELECT * FROM screened_companies WHERE incorporation_date BETWEEN ? AND ? ORDER BY incorporation_date DESC, pulled_at DESC", conn, params=(start_date, end_date))


def get_range_run_status(conn, start_date, end_date):
    return pd.read_sql_query("SELECT incorporation_date, api_total_results, stored_company_count, status, last_page_start_index, completed_at, updated_at FROM screening_runs WHERE incorporation_date BETWEEN ? AND ? ORDER BY incorporation_date", conn, params=(start_date, end_date))


def is_allowed_company_type(value):
    return normalize_text(value) in NORMALIZED_ALLOWED_COMPANY_TYPES


def request_search_page(client, target_date, start_index):
    return client.get("/advanced-search/companies", params={"incorporated_from": target_date, "incorporated_to": target_date, "company_status": "active", "company_type": ",".join(ALLOWED_COMPANY_TYPES), "sic_codes": ",".join(ALLOWED_SIC_CODES), "start_index": start_index, "size": SEARCH_PAGE_SIZE})


def get_all_officers(client, company_number):
    items = []
    start_index = 0
    while True:
        payload = client.get(f"/company/{company_number}/officers", params={"start_index": start_index, "items_per_page": OFFICERS_PAGE_SIZE})
        batch = payload.get("items", []) or []
        items.extend(batch)
        total = int(payload.get("total_results", len(items)) or len(items))
        if not batch or start_index + OFFICERS_PAGE_SIZE >= total:
            break
        start_index += OFFICERS_PAGE_SIZE
    return items


def get_all_pscs(client, company_number):
    items = []
    start_index = 0
    while True:
        payload = client.get(f"/company/{company_number}/persons-with-significant-control", params={"start_index": start_index, "items_per_page": PSC_PAGE_SIZE})
        batch = payload.get("items", []) or []
        items.extend(batch)
        total = int(payload.get("total_results", len(items)) or len(items))
        if not batch or start_index + PSC_PAGE_SIZE >= total:
            break
        start_index += PSC_PAGE_SIZE
    return items


def collect_international_director_details(client, company_number):
    countries = []
    director_count = 0
    for officer in get_all_officers(client, company_number):
        role = normalize_text(officer.get("officer_role"))
        if "director" not in role and role != "designated member":
            continue
        director_count += 1
        for value in [officer.get("country_of_residence"), (officer.get("address") or {}).get("country"), officer.get("nationality")]:
            if canonical_country_from_value(value):
                countries.append(str(value))
    countries = dedupe_preserve_order(countries)
    return bool(countries), countries, director_count


def analyse_psc_flags(client, company_number):
    shareholder_countries = []
    owner_names = []
    for psc in get_all_pscs(client, company_number):
        kind = str(psc.get("kind", ""))
        for value in [psc.get("country_of_residence"), (psc.get("address") or {}).get("country"), psc.get("nationality")]:
            if canonical_country_from_value(value):
                shareholder_countries.append(str(value))
        if kind in COMPANY_OWNER_KINDS or "corporate" in kind or "legal-person" in kind:
            owner_name = str(psc.get("name") or "").strip()
            if owner_name:
                owner_names.append(owner_name)
    shareholder_countries = dedupe_preserve_order(shareholder_countries)
    owner_names = dedupe_preserve_order(owner_names)
    return bool(shareholder_countries), shareholder_countries, bool(owner_names), owner_names


def parse_matching_sic(item):
    item_sics = [str(code) for code in (item.get("sic_codes") or [])]
    matching = [code for code in item_sics if code in ALLOWED_SIC_CODES]
    return ", ".join(matching or item_sics[:1])


def is_target_sic(item):
    return any(str(code) in TARGET_SIC_CODES for code in (item.get("sic_codes") or []))


def has_bonus_star(values):
    countries = {canonical_country_from_value(v) for v in values}
    return bool(countries & BONUS_STAR_COUNTRIES)


def build_rating(intl_dir, intl_sh, owned, target_sic, dir_details, sh_details):
    score = sum([intl_dir, intl_sh, owned, target_sic, has_bonus_star(dir_details) or has_bonus_star(sh_details)])
    return "⭐" * score


def process_company(client, item, incorporation_date):
    company_number = str(item.get("company_number") or "")
    company_name = str(item.get("company_name") or item.get("title") or "")
    intl_dir, dir_details, dir_count = collect_international_director_details(client, company_number)
    intl_sh, sh_details, owned, owner_names = analyse_psc_flags(client, company_number)
    return {
        "company_number": company_number, "company_name": company_name, "sic_code": parse_matching_sic(item),
        "incorporation_date": incorporation_date, "company_type": item.get("company_type", ""),
        "international_director": intl_dir, "international_director_detail": format_flagged_countries(dir_details),
        "international_shareholder": intl_sh, "international_shareholder_detail": format_flagged_countries(sh_details),
        "owned_by_company": owned, "owner_company_name": " | ".join(f"✓ {n}" for n in owner_names),
        "pulled_at": utc_now(), "raw_json": item, "profile_url": make_company_profile_url(company_number, company_name),
        "shortlisted": False, "target_sic": is_target_sic(item), "director_count": dir_count,
    }


def screen_date_until_complete(client, conn, target_date, log, progress):
    prior = get_run_state(conn, target_date)
    if prior and prior["status"] == "complete":
        return {"api_total": int(prior["api_total_results"]), "new_companies": 0, "skipped_companies": int(prior["stored_company_count"]), "pages": 0, "complete": 1}
    screened = get_screened_numbers_for_date(conn, target_date)
    start_index = 0
    pages = 0
    api_total = None
    new_count = 0
    skip_count = 0
    while pages < MAX_SEARCH_PAGES:
        payload = request_search_page(client, target_date, start_index)
        batch = payload.get("items", []) or []
        api_total = int(payload.get("total_results", 0) or 0)
        if not batch:
            mark_date_complete(conn, target_date, api_total, count_screened_for_date(conn, target_date))
            break
        pages += 1
        page_new = 0
        page_skip = 0
        for item in batch:
            cn = str(item.get("company_number") or "")
            if not cn:
                continue
            if cn in screened:
                page_skip += 1
                skip_count += 1
                continue
            try:
                row = process_company(client, item, target_date)
                upsert_company(conn, row)
                screened.add(cn)
                page_new += 1
                new_count += 1
            except Exception as exc:
                log.warning(f"{target_date} - {cn} failed: {exc}")
        next_idx = start_index + len(batch)
        stored = count_screened_for_date(conn, target_date)
        is_last = len(batch) < SEARCH_PAGE_SIZE or next_idx >= api_total
        save_run_state(conn, target_date, api_total, stored, "complete" if is_last else "in_progress", next_idx, utc_now() if is_last else None)
        log.write(f"{target_date} - page {pages}: {start_index+1:,}-{next_idx:,} of {api_total:,}; new {page_new:,}; skip {page_skip:,}")
        progress.progress(min(next_idx / max(api_total, 1), 1.0))
        if is_last:
            break
        start_index = next_idx
        time.sleep(0.15)
    if pages >= MAX_SEARCH_PAGES:
        raise RuntimeError(f"Stopped after {MAX_SEARCH_PAGES} pages for {target_date}")
    return {"api_total": int(api_total or 0), "new_companies": new_count, "skipped_companies": skip_count, "pages": pages, "complete": 1}


def build_display_df(db_df):
    cols = ["Shortlist", "Incorporated", "Target SIC", "Rating", "Directors", "Company Name", "SIC Code", "Signals", "International Director", "International Shareholder", "Owned By A Company", "Profile", "Pulled At", "company_number"]
    if db_df.empty:
        return pd.DataFrame(columns=cols)
    records = []
    for _, row in db_df.iterrows():
        intl_dir = bool(int(row.get("international_director", 0) or 0))
        intl_sh = bool(int(row.get("international_shareholder", 0) or 0))
        owned = bool(int(row.get("owned_by_company", 0) or 0))
        target = bool(int(row.get("target_sic", 0) or 0))
        signals = []
        if intl_dir:
            signals.append("Director 🌍")
        if intl_sh:
            signals.append("Shareholder 🌍")
        if owned:
            signals.append("Company owner 🏢")
        dir_det = [p.strip() for p in str(row.get("international_director_detail", "")).split("|") if p.strip()]
        sh_det = [p.strip() for p in str(row.get("international_shareholder_detail", "")).split("|") if p.strip()]
        records.append({
            "Shortlist": bool(int(row.get("shortlisted", 0) or 0)), "Incorporated": row.get("incorporation_date", ""),
            "Target SIC": "🎯" if target else "", "Rating": build_rating(intl_dir, intl_sh, owned, target, dir_det, sh_det),
            "Directors": int(row.get("director_count", 0) or 0), "Company Name": row.get("company_name", ""),
            "SIC Code": row.get("sic_code", ""), "Signals": " · ".join(signals),
            "International Director": row.get("international_director_detail", "") or "",
            "International Shareholder": row.get("international_shareholder_detail", "") or "",
            "Owned By A Company": row.get("owner_company_name", "") or "",
            "Profile": row.get("profile_url", "") or "", "Pulled At": row.get("pulled_at", ""),
            "company_number": row.get("company_number", ""),
        })
    return pd.DataFrame(records, columns=cols)


def apply_filters(df, only_flagged, sel_signals, sic_search, name_search, shortlisted_only, min_dir, max_dir):
    f = df.copy()
    if shortlisted_only:
        f = f[f["Shortlist"]]
    if only_flagged:
        mask = pd.Series(False, index=f.index)
        if "International Director" in sel_signals:
            mask |= f["International Director"].astype(str).str.startswith("✓", na=False)
        if "International Shareholder" in sel_signals:
            mask |= f["International Shareholder"].astype(str).str.startswith("✓", na=False)
        if "Owned By A Company" in sel_signals:
            mask |= f["Owned By A Company"].astype(str).str.startswith("✓", na=False)
        f = f[mask]
    if sic_search.strip():
        f = f[f["SIC Code"].astype(str).str.contains(re.escape(sic_search.strip()), case=False, na=False)]
    if name_search.strip():
        f = f[f["Company Name"].astype(str).str.contains(re.escape(name_search.strip()), case=False, na=False)]
    return f[(f["Directors"] >= min_dir) & (f["Directors"] <= max_dir)]


def render_kpis(df):
    total = len(df)
    flagged = 0 if df.empty else int((df["International Director"].astype(str).str.startswith("✓", na=False) | df["International Shareholder"].astype(str).str.startswith("✓", na=False) | df["Owned By A Company"].astype(str).str.startswith("✓", na=False)).sum())
    intl_dir = 0 if df.empty else int(df["International Director"].astype(str).str.startswith("✓", na=False).sum())
    intl_sh = 0 if df.empty else int(df["International Shareholder"].astype(str).str.startswith("✓", na=False).sum())
    target = 0 if df.empty else int(df["Target SIC"].eq("🎯").sum())
    short = 0 if df.empty else int(df["Shortlist"].sum())
    avg_dir = 0 if df.empty else round(float(df["Directors"].mean()), 1)
    c = st.columns(7)
    c[0].metric("Total", f"{total:,}")
    c[1].metric("Flagged", f"{flagged:,}")
    c[2].metric("Intl Dir", f"{intl_dir:,}")
    c[3].metric("Intl SH", f"{intl_sh:,}")
    c[4].metric("Target SIC", f"{target:,}")
    c[5].metric("Shortlist", f"{short:,}")
    c[6].metric("Avg Dir", str(avg_dir))


def render_sidebar(default_start, default_end):
    with st.sidebar:
        st.header("Controls")
        sel = st.date_input("Date range", value=(default_start, default_end), format="YYYY-MM-DD")
        if isinstance(sel, tuple) and len(sel) == 2:
            start_d, end_d = sel
        else:
            start_d = end_d = sel
        run_btn = st.button("Start screening", type="primary", use_container_width=True)
        force = st.checkbox("Re-screen completed", value=False)
        st.divider()
        st.subheader("Filters")
        only_fl = st.checkbox("Only flagged", value=False)
        sel_sig = st.multiselect("Signals", SIGNAL_OPTIONS, default=SIGNAL_OPTIONS)
        dir_rng = st.slider("Directors", 0, 20, (0, 20))
        sic_txt = st.text_input("SIC", placeholder="62012")
        name_txt = st.text_input("Name", placeholder="Labs")
        short_only = st.checkbox("Shortlisted only", value=False)
    return start_d, end_d, run_btn, force, sel_sig, sic_txt, name_txt, short_only, dir_rng[0], dir_rng[1]


def main():
    apply_custom_css()
    st.title("Companies House Screener")
    st.caption("Screen new incorporations with completion tracking.")
    st.markdown('<div class="app-note"><strong>How:</strong> App screens all companies per date, saves continuously, marks dates complete. Completed dates skipped on reruns.</div>', unsafe_allow_html=True)
    try:
        api_keys = validate_api_keys()
    except ValueError as e:
        st.error(str(e))
        st.stop()
    conn = init_db()
    client = CHClient(api_keys)
    today = date.today()
    def_end = today - timedelta(days=1)
    def_start = def_end - timedelta(days=6)
    res = render_sidebar(def_start, def_end)
    start_date = res[0]
    end_date = res[1]
    run_btn = res[2]
    force = res[3]
    sel_signals = res[4]
    sic_txt = res[5]
    name_txt = res[6]
    short_only = res[7]
    min_dir = res[8]
    max_dir = res[9]
    if start_date > end_date:
        st.error("Start must be <= end.")
        st.stop()
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()
    date_lbl = start_str if start_date == end_date else f"{start_str} to {end_str}"
    st.subheader("Status")
    status_df = get_range_run_status(conn, start_str, end_str)
    if status_df.empty:
        st.info("No state yet.")
    else:
        st.dataframe(status_df.rename(columns={"incorporation_date": "Date", "api_total_results": "API", "stored_company_count": "Stored", "last_page_start_index": "Read", "completed_at": "Done", "updated_at": "Updated"}), use_container_width=True, hide_index=True)
    if run_btn:
        dates = []
        cur = start_date
        while cur <= end_date:
            dates.append(cur.isoformat())
            cur += timedelta(days=1)
        with st.status(f"Screening {date_lbl}...", expanded=True) as stat:
            log = st.empty()
            prog = st.progress(0)
            done_cnt = 0
            proc_cnt = 0
            new_tot = 0
            skip_tot = 0
            errs = []
            for i, td in enumerate(dates, 1):
                ex = get_run_state(conn, td)
                if ex and ex["status"] == "complete" and not force:
                    done_cnt += 1
                    log.write(f"{td} - done, skip {ex['stored_company_count']:,} of {ex['api_total_results']:,}")
                    prog.progress(i / len(dates))
                    continue
                proc_cnt += 1
                st.write(f"### {td}")
                pp = st.progress(0)
                try:
                    summ = screen_date_until_complete(client, conn, td, log, pp)
                    new_tot += summ["new_companies"]
                    skip_tot += summ["skipped_companies"]
                    log.success(f"{td} done - {summ['pages']} pg, {summ['api_total']:,} API, {summ['new_companies']:,} new, {summ['skipped_companies']:,} skip")
                except Exception as e:
                    errs.append(f"{td}: {e}")
                    log.error(f"{td} error: {e}")
                prog.progress(i / len(dates))
            if errs:
                stat.update(label="Errors", state="error")
                st.error("\n".join(errs[:20]))
            else:
                stat.update(label="Done", state="complete")
                st.success(f"Done: {done_cnt} skip, {proc_cnt} proc, {new_tot} new, {skip_tot} skip")
    db_df = read_db_rows(conn, start_str, end_str)
    disp_df = build_display_df(db_df)
    render_kpis(disp_df)
    st.markdown('<div class="signal-legend"><div class="signal-pill">Dir 🌍</div><div class="signal-pill">SH 🌍</div><div class="signal-pill">Owner 🏢</div><div class="signal-pill">Target 🎯</div><div class="signal-pill">Rating ⭐</div></div>', unsafe_allow_html=True)
    filt_df = apply_filters(disp_df, False, sel_signals, sic_txt, name_txt, short_only, min_dir, max_dir)
    t1, t2, t3 = st.tabs(["Results", "Shortlist", "Settings"])
    with t1:
        st.caption(f"{date_lbl} - {len(filt_df):,} rows")
        ecols = ["Shortlist", "Incorporated", "Target SIC", "Rating", "Directors", "Company Name", "SIC Code", "Signals", "International Director", "International Shareholder", "Owned By A Company", "Profile", "Pulled At", "company_number"]
        edf = st.data_editor(
    filt_df[ecols], 
    key=f"ed_{start_str}_{end_str}", 
    use_container_width=True, 
    hide_index=True, 
    disabled=[c for c in ecols if c != "Shortlist"], 
    column_config={
        "Shortlist": st.column_config.CheckboxColumn("Shortlist"), 
        "Directors": st.column_config.NumberColumn("Directors"), 
        "Profile": st.column_config.LinkColumn("Profile"), 
        "company_number": None
    }
)
        if not edf.empty:
            chg = edf[["company_number", "Shortlist"]].merge(disp_df[["company_number", "Shortlist"]], on="company_number", suffixes=("_n", "_o"))
            diff = chg[chg["Shortlist_n"] != chg["Shortlist_o"]]
            for _, r in diff.iterrows():
                set_shortlisted_state(conn, str(r["company_number"]), bool(r["Shortlist_n"]))
            if not diff.empty:
                st.success(f"Updated {len(diff):,}")
                st.rerun()
        st.download_button("Download CSV", filt_df.drop(columns=["company_number"]).to_csv(index=False).encode(), f"screen_{start_str}_{end_str}.csv", "text/csv", use_container_width=True)
    with t2:
        sh_df = disp_df[disp_df["Shortlist"]]
        if sh_df.empty:
            st.info("None")
        else:
            st.dataframe(sh_df.drop(columns=["company_number"]), use_container_width=True, hide_index=True, column_config={"Profile": st.column_config.LinkColumn("Profile", "Open")})
    with t3:
        st.write(f"Keys: {len(api_keys)}")
        st.write(f"Page: {SEARCH_PAGE_SIZE}")
        st.write(f"Max pg: {MAX_SEARCH_PAGES}")
        st.write(f"SIC: {len(ALLOWED_SIC_CODES)}")
        st.write(f"Target: {', '.join(sorted(TARGET_SIC_CODES))}")


if __name__ == "__main__":
    main()
