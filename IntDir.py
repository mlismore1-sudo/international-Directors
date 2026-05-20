"""
Companies House Screening Tool
Run: streamlit run companies_house_screening.py
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from itertools import cycle
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(
    page_title="CH Screening Tool",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded",
)

RESULTS_FILE = Path("ch_screening_results_saved.json")
DISK_CACHE_DIR = Path(".ch_cache")
DISK_CACHE_DIR.mkdir(exist_ok=True)

COMPANIES_HOUSE_BASE = "https://api.company-information.service.gov.uk"
REQUEST_TIMEOUT = 20
HTTP_SESSIONS: Dict[str, requests.Session] = {}

TARGET_SICS: Set[str] = {
    "62012", "62020", "63120", "47910", "46190", "46499", "70229", "73110",
    "74909", "68209", "64209", "68100", "32990", "10890", "86900", "93130",
    "96040", "82990", "72110",
}

TECH_BIOTECH_SICS: Set[str] = {"62012", "72110"}

OTHER_HIGH_VALUE_SICS: Set[str] = TARGET_SICS - TECH_BIOTECH_SICS

TARGET_COUNTRIES: Set[str] = {
    "china", "france", "germany", "belgium", "netherlands", "spain", "portugal",
    "lithuania", "poland", "norway", "finland", "denmark", "sweden",
    "united states", "india", "singapore", "hong kong",
}

ACCEPTED_COMPANY_TYPES: Set[str] = {
    "ltd",
    "llp",
    "private-limited-company",
    "limited-liability-partnership",
}

_COUNTRY_ALIASES: Dict[str, str] = {
    "usa": "united states",
    "u.s.a.": "united states",
    "us": "united states",
    "united states of america": "united states",
    "hk": "hong kong",
    "holland": "netherlands",
    "the netherlands": "netherlands",
    "prc": "china",
    "peoples republic of china": "china",
    "people's republic of china": "china",
}

_LEGAL_KIND_MARKERS = ["corporate-entity", "legal-person", "firm", "super-secure"]
_CORPORATE_NAME_MARKERS = [
    " ltd", " limited", " llp", " plc", " inc", " gmbh", " sarl", " bv", " ag", " oy", " spa", " srl"
]


def _cache_path(cn: str) -> Path:
    return DISK_CACHE_DIR / f"{cn}.json"


def _write_disk_cache(cn: str, row: Dict) -> None:
    try:
        _cache_path(cn).write_text(json.dumps(row), encoding="utf-8")
    except Exception:
        pass


def _read_disk_cache(cn: str) -> Optional[Dict]:
    p = _cache_path(cn)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _load_all_disk_cache() -> Dict[str, Dict]:
    cache: Dict[str, Dict] = {}
    for f in DISK_CACHE_DIR.glob("*.json"):
        try:
            cache[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return cache


def _save_results(df: pd.DataFrame) -> None:
    if not df.empty:
        RESULTS_FILE.write_text(df.to_json(orient="records", date_format="iso"), encoding="utf-8")


def _load_saved_results() -> pd.DataFrame:
    if RESULTS_FILE.exists():
        try:
            return pd.read_json(RESULTS_FILE, orient="records")
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _load_api_keys() -> List[str]:
    keys = [
        st.secrets.get("COMPANIES_HOUSE_API_KEY_1", "").strip(),
        st.secrets.get("COMPANIES_HOUSE_API_KEY_2", "").strip(),
        st.secrets.get("COMPANIES_HOUSE_API_KEY_3", "").strip(),
    ]
    return [k for k in keys if k]


def _get_key_rotator() -> Iterator[str]:
    if "key_rotator" not in st.session_state:
        keys = _load_api_keys()
        if not keys:
            st.error("No API keys found in Streamlit secrets.")
            st.stop()
        st.session_state.key_rotator = cycle(keys)
        st.session_state.active_key_count = len(keys)
    return st.session_state.key_rotator


def _next_key() -> str:
    return next(_get_key_rotator())


def _get_http_session(api_key: str) -> requests.Session:
    if api_key not in HTTP_SESSIONS:
        s = requests.Session()
        s.auth = (api_key, "")
        s.headers.update({"Accept": "application/json"})
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        HTTP_SESSIONS[api_key] = s
    return HTTP_SESSIONS[api_key]


def _init_session() -> None:
    defaults = {
        "refresh_token": 0,
        "last_refreshed_at": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    if "disk_cache" not in st.session_state:
        st.session_state.disk_cache = _load_all_disk_cache()

    if "results_df" not in st.session_state:
        loaded = _load_saved_results()
        required_cols = {
            "Timestamp": "",
            "Company Name": "",
            "Reason": "",
            "Matched SIC": "",
            "SIC Codes": "",
            "Tech & Biotech": False,
            "High Value Lead": False,
            "_company_number": "",
            "_added_at": "",
            "_cache_token": 0,
        }
        for col, default in required_cols.items():
            if col not in loaded.columns:
                loaded[col] = default

        if not loaded.empty and "_added_at" in loaded.columns:
            loaded = loaded.sort_values("_added_at", ascending=False).reset_index(drop=True)

        st.session_state.results_df = loaded


def _normalise(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value).strip().lower())
    return _COUNTRY_ALIASES.get(cleaned, cleaned)


_NORM_TARGETS = {_normalise(c) for c in TARGET_COUNTRIES}


def _is_target(value: Optional[str]) -> bool:
    return _normalise(value) in _NORM_TARGETS


def _get(url: str, params: Optional[Dict] = None) -> Dict:
    api_key = _next_key()
    resp = _get_http_session(api_key).get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _paginated(url: str, items_key: str = "items", page: int = 100) -> List[Dict]:
    out: List[Dict] = []
    start = 0

    while True:
        data = _get(url, {"items_per_page": page, "start_index": start})
        batch = data.get(items_key) or []
        out.extend(batch)

        if len(batch) < page:
            break

        start += page

    return out


def api_search_by_date(incorporated_on: date) -> List[Dict]:
    all_results: List[Dict] = []
    date_str = incorporated_on.strftime("%Y-%m-%d")

    def _fetch_sic(sic: str) -> List[Dict]:
        results: List[Dict] = []
        start = 0

        while True:
            try:
                data = _get(
                    f"{COMPANIES_HOUSE_BASE}/advanced-search/companies",
                    params={
                        "incorporated_from": date_str,
                        "incorporated_to": date_str,
                        "company_status": "active",
                        "company_type": "private-limited-company,limited-liability-partnership",
                        "sic_codes": sic,
                        "items_per_page": 100,
                        "start_index": start,
                    },
                )
            except requests.HTTPError:
                break

            batch = data.get("items") or []
            results.extend(batch)
            total = data.get("total_results", 0)
            start += len(batch)

            if not batch or start >= total:
                break

        return results

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_sic, sic): sic for sic in TARGET_SICS}
        for future in as_completed(futures):
            try:
                all_results.extend(future.result())
            except Exception:
                pass

    return all_results


def api_profile(cn: str) -> Dict:
    return _get(f"{COMPANIES_HOUSE_BASE}/company/{cn}")


def api_officers(cn: str) -> List[Dict]:
    return _paginated(f"{COMPANIES_HOUSE_BASE}/company/{cn}/officers")


def api_pscs(cn: str) -> List[Dict]:
    return _paginated(f"{COMPANIES_HOUSE_BASE}/company/{cn}/persons-with-significant-control")


def _psc_is_legal_entity(psc: Dict) -> bool:
    kind = str(psc.get("kind", "")).lower()
    name = str(psc.get("name", "")).strip().lower()

    if any(marker in kind for marker in _LEGAL_KIND_MARKERS):
        return True

    if any(marker in f" {name}" for marker in _CORPORATE_NAME_MARKERS):
        return True

    identification = psc.get("identification") or {}
    if identification.get("registration_number"):
        return True

    return False


def screen_pscs(pscs: List[Dict]) -> Dict:
    owned_by_company = False
    psc_target_country = False

    for psc in pscs:
        if _psc_is_legal_entity(psc):
            owned_by_company = True

        if _is_target(psc.get("nationality")):
            psc_target_country = True

    return {
        "owned_by_company": owned_by_company,
        "psc_target_country": psc_target_country,
    }


def screen_officers(officers: List[Dict]) -> Dict:
    director_target_residency = False

    for o in officers:
        if str(o.get("officer_role", "")).lower() != "director":
            continue

        residence = o.get("country_of_residence")
        if _is_target(residence):
            director_target_residency = True
            break

    return {
        "director_target_residency": director_target_residency,
    }


def build_reason(sics: Set[str], psc: Dict, off: Dict, is_high_value: bool) -> str:
    reasons: List[str] = []

    if sics & TECH_BIOTECH_SICS:
        reasons.append("SIC Match")

    if off["director_target_residency"] or psc["psc_target_country"]:
        reasons.append("🌍 Country Match")

    if psc["owned_by_company"]:
        reasons.append("👨‍👧 Owned by Another Company")

    if is_high_value and not reasons:
        other_hits = sorted(sics & OTHER_HIGH_VALUE_SICS)
        if other_hits:
            reasons.append(f"High Value SIC: {', '.join(other_hits)}")

    return " | ".join(reasons)


def match_sic_label(sics: Set[str]) -> str:
    return ", ".join(sorted(sics & TARGET_SICS))


def build_row(cn: str, profile: Dict, psc: Dict, off: Dict, sics: Set[str]) -> Dict:
    tech_hit = bool(sics & TECH_BIOTECH_SICS)
    other_high_value_hit = bool(sics & OTHER_HIGH_VALUE_SICS)
    country_match = off["director_target_residency"] or psc["psc_target_country"]
    owned_by_company = psc["owned_by_company"]

    is_tech_biotech = tech_hit
    is_high_value = other_high_value_hit or (tech_hit and (country_match or owned_by_company))

    now_utc = datetime.utcnow()

    return {
        "Timestamp": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Company Name": profile.get("company_name", ""),
        "Reason": build_reason(sics, psc, off, is_high_value),
        "Matched SIC": match_sic_label(sics),
        "SIC Codes": ", ".join(sorted(sics & TARGET_SICS)),
        "Tech & Biotech": is_tech_biotech,
        "High Value Lead": is_high_value,
        "_company_number": cn,
        "_added_at": now_utc.isoformat(),
        "_cache_token": st.session_state.refresh_token,
    }


def enrich_one(cn: str) -> Optional[Dict]:
    token = st.session_state.refresh_token

    mem = st.session_state.disk_cache.get(cn)
    if mem and mem.get("_cache_token") == token:
        return mem

    disk = _read_disk_cache(cn)
    if disk and disk.get("_cache_token") == token:
        st.session_state.disk_cache[cn] = disk
        return disk

    profile = api_profile(cn)
    status = str(profile.get("company_status", "")).lower()
    ctype = str(profile.get("type", "")).lower()

    sic_field = profile.get("sic_codes") or profile.get("sic_code") or []
    if isinstance(sic_field, str):
        sics = {x.strip() for x in re.split(r"[;,]", sic_field) if x.strip()}
    else:
        sics = {str(x).strip() for x in sic_field if str(x).strip()}

    if status != "active" or ctype not in ACCEPTED_COMPANY_TYPES:
        return None

    if not (sics & TARGET_SICS):
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_officers = pool.submit(api_officers, cn)
        f_pscs = pool.submit(api_pscs, cn)

        officers = f_officers.result()
        pscs = f_pscs.result()

    psc_flags = screen_pscs(pscs)
    off_flags = screen_officers(officers)
    row = build_row(cn, profile, psc_flags, off_flags, sics)

    st.session_state.disk_cache[cn] = row
    _write_disk_cache(cn, row)
    return row


def enrich_all(search_rows: List[Dict]) -> pd.DataFrame:
    seen: Set[str] = set()
    unique: List[str] = []

    for r in search_rows:
        cn = str(r.get("company_number", "")).strip()
        if cn and cn not in seen:
            seen.add(cn)
            unique.append(cn)

    if not unique:
        return pd.DataFrame()

    token = st.session_state.refresh_token
    cached_rows: List[Dict] = []
    to_fetch: List[str] = []

    for cn in unique:
        cached = st.session_state.disk_cache.get(cn) or _read_disk_cache(cn)
        if cached and cached.get("_cache_token") == token:
            cached_rows.append(cached)
        else:
            to_fetch.append(cn)

    new_rows: List[Dict] = []
    total = len(to_fetch)

    if total:
        progress = st.progress(0)
        status_el = st.empty()

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(enrich_one, cn): cn for cn in to_fetch}
            for idx, future in enumerate(as_completed(futures), start=1):
                progress.progress(idx / total)
                status_el.caption(f"Enriching {idx} / {total} new companies…")
                try:
                    row = future.result()
                    if row:
                        new_rows.append(row)
                except Exception:
                    pass

        progress.empty()
        status_el.empty()

    df = pd.DataFrame(cached_rows + new_rows)

    if not df.empty and "_added_at" in df.columns:
        df = df.sort_values("_added_at", ascending=False).reset_index(drop=True)

    return df


def render_sidebar() -> Dict[str, Any]:
    with st.sidebar:
        st.header("📅 Search date")
        incorporated_from = st.date_input(
            "Companies incorporated on",
            value=date.today(),
            max_value=date.today(),
        )
        st.caption(
            f"Searching active Private Ltd and LLP companies across {len(TARGET_SICS)} SIC codes."
        )

        run = st.button("🔍 Run new search", use_container_width=True, type="primary")

        c1, c2 = st.columns(2)
        refresh = c1.button("🔄 Refresh cache", use_container_width=True)
        clear = c2.button("🗑 Clear results", use_container_width=True)

        if refresh:
            st.session_state.refresh_token += 1
            st.session_state.last_refreshed_at = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
            st.session_state.disk_cache = {}
            st.success("Cache refresh enabled for next run.")

        if clear:
            st.session_state.results_df = pd.DataFrame()
            if RESULTS_FILE.exists():
                RESULTS_FILE.unlink()
            for f in DISK_CACHE_DIR.glob("*.json"):
                f.unlink()
            st.session_state.disk_cache = {}
            st.success("Saved results and cache cleared.")

    return {"incorporated_from": incorporated_from, "run": run}


def render_tables(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No results yet — choose a date and run the search.")
        return

    tech = df[df["Tech & Biotech"] == True].copy()
    high = df[df["High Value Lead"] == True].copy()

    display_cols = ["Timestamp", "Company Name", "Reason", "Matched SIC"]

    st.subheader(f"Tech & Biotech — {len(tech)} companies")
    st.dataframe(
        tech[display_cols] if not tech.empty else pd.DataFrame(columns=display_cols),
        use_container_width=True,
        height=300,
    )

    st.subheader(f"High Value Leads — {len(high)} companies")
    st.dataframe(
        high[display_cols] if not high.empty else pd.DataFrame(columns=display_cols),
        use_container_width=True,
        height=420,
    )

    st.download_button(
        "⬇ Download all results as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="ch_screening_results.csv",
        mime="text/csv",
    )


def main() -> None:
    _init_session()
    _get_key_rotator()

    st.title("🏢 Companies House Screening Tool")
    st.caption(
        "Two outputs: Tech & Biotech and High Value Leads. Results stay cached unless you use Refresh cache."
    )

    controls = render_sidebar()
    st.divider()

    if controls["run"]:
        with st.spinner(
            f"Searching companies incorporated on {controls['incorporated_from'].strftime('%d %b %Y')}…"
        ):
            try:
                raw = api_search_by_date(controls["incorporated_from"])
            except requests.HTTPError as exc:
                st.error(f"Search failed: {exc}")
                st.stop()

        if not raw:
            st.warning("No companies returned for that date.")
        else:
            st.info(f"Found {len(raw)} raw results. Checking cache and enriching companies…")
            new_df = enrich_all(raw)

            if not new_df.empty:
                existing = st.session_state.results_df

                if not existing.empty and "_company_number" in existing.columns:
                    combined = pd.concat([existing, new_df], ignore_index=True)
                    combined = combined.sort_values("_added_at", ascending=False)
                    combined = combined.drop_duplicates(subset=["_company_number"], keep="first")
                    combined = combined.reset_index(drop=True)
                else:
                    combined = new_df.sort_values("_added_at", ascending=False).reset_index(drop=True)

                st.session_state.results_df = combined
                _save_results(combined)
                st.success(f"Done. {len(new_df)} companies processed.")

    render_tables(st.session_state.results_df)


if __name__ == "__main__":
    main()
