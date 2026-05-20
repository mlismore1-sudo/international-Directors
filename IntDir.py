"""
Companies House Screening Tool
================================
Run:     streamlit run companies_house_screening.py
Secrets: COMPANIES_HOUSE_API_KEY_1 / _2 / _3 in .streamlit/secrets.toml
Install: pip install streamlit requests pandas
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from itertools import cycle
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

import pandas as pd
import requests
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CH Screening Tool",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# SPEED OPTIMISATION 1 — Persistent disk cache (screened companies never re-fetched)
# Each screened company is written to a per-company JSON file under .ch_cache/
# On startup the entire cache is loaded into memory. A company is only re-fetched
# if the user explicitly hits "Refresh Cache".
# ─────────────────────────────────────────────────────────────────────────────
RESULTS_FILE  = Path("ch_screening_results_saved.json")
DISK_CACHE_DIR = Path(".ch_cache")
DISK_CACHE_DIR.mkdir(exist_ok=True)


def _cache_path(cn: str) -> Path:
    return DISK_CACHE_DIR / f"{cn}.json"


def _write_disk_cache(cn: str, row: Dict) -> None:
    try:
        import json
        _cache_path(cn).write_text(json.dumps(row), encoding="utf-8")
    except Exception:
        pass


def _read_disk_cache(cn: str) -> Optional[Dict]:
    p = _cache_path(cn)
    if p.exists():
        try:
            import json
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _load_all_disk_cache() -> Dict[str, Dict]:
    """Load every cached company into memory on startup — O(1) lookups thereafter."""
    import json
    cache: Dict[str, Dict] = {}
    for f in DISK_CACHE_DIR.glob("*.json"):
        try:
            cache[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return cache


def _save_results(df: pd.DataFrame) -> None:
    if df.empty:
        return
    RESULTS_FILE.write_text(
        df.to_json(orient="records", date_format="iso"), encoding="utf-8"
    )


def _load_saved_results() -> pd.DataFrame:
    if RESULTS_FILE.exists():
        try:
            return pd.read_json(RESULTS_FILE, orient="records")
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
COMPANIES_HOUSE_BASE = "https://api.company-information.service.gov.uk"
REQUEST_TIMEOUT      = 20

# SPEED OPTIMISATION 2 — Shared persistent HTTP session with keep-alive
# One TCP connection per API key is reused across all requests rather than
# opening a new connection per call.
_HTTP_SESSIONS: Dict[str, requests.Session] = {}

TARGET_SICS: Set[str] = {
    "62012", "62020", "63120", "47910", "46190", "46499",
    "70229", "73110", "74909", "68209", "64209", "68100",
    "32990", "10890", "86900", "93130", "96040", "82990",
    "72110",
}

ALWAYS_PUBLISH_SICS: Set[str] = {"62012", "72110"}

TARGET_COUNTRIES: Set[str] = {
    "china", "france", "germany", "belgium", "netherlands",
    "spain", "portugal", "lithuania", "poland", "norway",
    "finland", "denmark", "sweden", "united states",
    "india", "singapore", "hong kong",
}

EXCLUDED_COUNTRIES: Set[str] = {"nigeria", "pakistan", "turkey"}

ACCEPTED_COMPANY_TYPES: Set[str] = {
    "ltd", "llp",
    "private-limited-company",
    "limited-liability-partnership",
}

_COUNTRY_ALIASES: Dict[str, str] = {
    "turkiye":                    "turkey",
    "türkiye":                    "turkey",
    "uae":                        "united arab emirates",
    "u.a.e.":                     "united arab emirates",
    "england":                    "united kingdom",
    "scotland":                   "united kingdom",
    "wales":                      "united kingdom",
    "northern ireland":           "united kingdom",
    "usa":                        "united states",
    "u.s.a.":                     "united states",
    "united states of america":   "united states",
    "us":                         "united states",
    "hk":                         "hong kong",
    "prc":                        "china",
    "peoples republic of china":  "china",
    "people's republic of china": "china",
    "holland":                    "netherlands",
    "the netherlands":            "netherlands",
}

_LEGAL_KIND_MARKERS: List[str] = [
    "corporate-entity", "legal-person", "firm", "super-secure",
]

_CORPORATE_NAME_MARKERS: List[str] = [
    " ltd", " limited", " llp", " plc", " inc",
    " gmbh", " sarl", " bv", " ag", " oy", " spa",
    " srl", " as ", " ab ", " nv ",
]

# ─────────────────────────────────────────────────────────────────────────────
# API KEY ROTATION
# ─────────────────────────────────────────────────────────────────────────────
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
            st.error(
                "**No API keys found.**\n\n"
                "Add to your Streamlit secrets:\n"
                "```toml\n"
                'COMPANIES_HOUSE_API_KEY_1 = "your_first_key"\n'
                'COMPANIES_HOUSE_API_KEY_2 = "your_second_key"\n'
                'COMPANIES_HOUSE_API_KEY_3 = "your_third_key"\n'
                "```"
            )
            st.stop()
        st.session_state.key_rotator      = cycle(keys)
        st.session_state.active_key_count = len(keys)
    return st.session_state.key_rotator


def _next_key() -> str:
    return next(_get_key_rotator())


def _get_http_session(api_key: str) -> requests.Session:
    """OPTIMISATION 2 — reuse one session per key (TCP keep-alive)."""
    if api_key not in _HTTP_SESSIONS:
        s = requests.Session()
        s.auth = (api_key, "")
        s.headers.update({"Accept": "application/json"})
        # SPEED OPTIMISATION 3 — retry with backoff on transient failures
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        _HTTP_SESSIONS[api_key] = s
    return _HTTP_SESSIONS[api_key]

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
def _init_session() -> None:
    defaults: Dict[str, Any] = {
        "refresh_token":     0,
        "last_refreshed_at": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

    # SPEED OPTIMISATION 1 — load entire disk cache into memory once on startup
    if "disk_cache" not in st.session_state:
        st.session_state.disk_cache = _load_all_disk_cache()

    if "results_df" not in st.session_state:
        loaded = _load_saved_results()
        if not loaded.empty and "_added_at" in loaded.columns:
            loaded = loaded.sort_values("_added_at", ascending=False).reset_index(drop=True)
        st.session_state.results_df = loaded

# ─────────────────────────────────────────────────────────────────────────────
# COUNTRY NORMALISATION
# ─────────────────────────────────────────────────────────────────────────────
def _normalise(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value).strip().lower())
    return _COUNTRY_ALIASES.get(cleaned, cleaned)


_NORM_TARGETS:  Set[str] = {_normalise(c) for c in TARGET_COUNTRIES}
_NORM_EXCLUDED: Set[str] = {_normalise(c) for c in EXCLUDED_COUNTRIES}


def _is_target(value: Optional[str]) -> bool:
    return _normalise(value) in _NORM_TARGETS


def _is_excluded(value: Optional[str]) -> bool:
    return _normalise(value) in _NORM_EXCLUDED

# ─────────────────────────────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _get(url: str, params: Optional[Dict] = None) -> Dict:
    api_key = _next_key()
    resp    = _get_http_session(api_key).get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _paginated(url: str, items_key: str = "items", page: int = 100) -> List[Dict]:
    out: List[Dict] = []
    start = 0
    while True:
        data  = _get(url, {"items_per_page": page, "start_index": start})
        batch = data.get(items_key) or []
        out.extend(batch)
        if len(batch) < page:
            break
        start += page
    return out


def api_search_by_date(incorporated_from: date) -> List[Dict]:
    # SPEED OPTIMISATION 4 — search all SIC codes concurrently with a thread pool
    all_results: List[Dict] = []
    date_str = incorporated_from.strftime("%Y-%m-%d")

    def _fetch_sic(sic: str) -> List[Dict]:
        sic_results: List[Dict] = []
        start = 0
        while True:
            try:
                data = _get(
                    f"{COMPANIES_HOUSE_BASE}/advanced-search/companies",
                    params={
                        "incorporated_from": date_str,
                        "company_status":    "active",
                        "company_type":      "ltd,llp",
                        "sic_codes":         sic,
                        "items_per_page":    100,
                        "start_index":       start,
                    },
                )
            except requests.HTTPError:
                break
            batch = data.get("items") or []
            sic_results.extend(batch)
            total = data.get("total_results", 0)
            start += len(batch)
            if not batch or start >= total:
                break
        return sic_results

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
    return _paginated(
        f"{COMPANIES_HOUSE_BASE}/company/{cn}/persons-with-significant-control",
    )

# ─────────────────────────────────────────────────────────────────────────────
# SCREENING LOGIC
# ─────────────────────────────────────────────────────────────────────────────
def _psc_is_legal_entity(psc: Dict) -> bool:
    kind = str(psc.get("kind", "")).lower()
    name = str(psc.get("name", "")).strip()
    if any(m in kind for m in _LEGAL_KIND_MARKERS):
        return True
    if any(m in f" {name.lower()}" for m in _CORPORATE_NAME_MARKERS):
        return True
    natures = [str(x).lower() for x in psc.get("natures_of_control") or []]
    return (
        any("ownership-of-shares" in n or "voting-rights" in n for n in natures)
        and not psc.get("nationality")
    )


def screen_pscs(pscs: List[Dict]) -> Dict:
    legal_entities:   List[str] = []
    target_nationals: List[str] = []
    excluded_found = False

    for psc in pscs:
        if _psc_is_legal_entity(psc):
            legal_entities.append(str(psc.get("name", "Unknown entity")))
        nat = psc.get("nationality")
        if _is_target(nat):
            target_nationals.append(f"{psc.get('name', 'Unknown')} ({nat})")
        if _is_excluded(nat):
            excluded_found = True

    return {
        "owned_by_company":        bool(legal_entities),
        "owning_company_names":    "; ".join(legal_entities),
        "psc_from_target_country": bool(target_nationals),
        "psc_target_details":      "; ".join(target_nationals),
        "psc_excluded":            excluded_found,
    }


def screen_officers(officers: List[Dict]) -> Dict:
    target_nat:    List[str] = []
    target_res:    List[str] = []
    excluded_found = False

    for o in officers:
        if str(o.get("officer_role", "")).lower() != "director":
            continue
        res = o.get("country_of_residence") or o.get("usual_residential_country")
        nat = o.get("nationality")
        name = o.get("name", "Unknown")
        if _is_target(nat):
            target_nat.append(f"{name} ({nat})")
        if _is_target(res):
            target_res.append(f"{name} ({res})")
        if _is_excluded(res) or _is_excluded(nat):
            excluded_found = True

    return {
        "director_target_nationality":  bool(target_nat),
        "director_nat_details":         "; ".join(target_nat),
        "director_target_residency":    bool(target_res),
        "director_res_details":         "; ".join(target_res),
        "director_excluded":            excluded_found,
    }


def decide_publish(psc: Dict, officers: Dict, sic_codes: Set[str]) -> Dict:
    always_publish = bool(sic_codes & ALWAYS_PUBLISH_SICS)
    excluded       = psc["psc_excluded"] or officers["director_excluded"]
    any_flag       = (
        psc["owned_by_company"]
        or psc["psc_from_target_country"]
        or officers["director_target_nationality"] or officers["director_target_residency"]
    )

    if excluded:
        return {"should_publish": False, "publish_reason": "Excluded country detected"}
    if always_publish:
        return {"should_publish": True,  "publish_reason": "SIC 62012/72110 retained"}
    if any_flag:
        return {"should_publish": True,  "publish_reason": "Screening criteria matched"}
    return     {"should_publish": False, "publish_reason": "No criteria matched"}


def build_row(cn: str, profile: Dict, psc: Dict, off: Dict, pub: Dict) -> Dict:
    sics = profile.get("sic_codes") or []
    return {
        "Company Name":              profile.get("company_name", ""),
        "SIC Codes":                 ", ".join(map(str, sics)),
        "Director Nationality":         "🌍 " + off["director_nat_details"] if off["director_target_nationality"] else "—",
        "Director Residency":           "🌍 " + off["director_res_details"] if off["director_target_residency"] else "—",
        "Owned by Another Company":  "👨‍👧 " + psc["owning_company_names"] if psc["owned_by_company"] else "No",
        "PSC from Target Country":   "🌍 " + psc["psc_target_details"] if psc["psc_from_target_country"] else "—",
        "Publishable":               "✅ Yes" if pub["should_publish"] else "❌ No",
        # Hidden reference fields (used in CSV export, not shown in table)
        "_company_number":           cn,
        "_incorporated":             profile.get("date_of_creation", ""),
        "_ch_url":                   f"https://find-and-update.company-information.service.gov.uk/company/{cn}",
        "_publish_reason":           pub["publish_reason"],
        "_added_at":                 datetime.utcnow().isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────────────
# SPEED OPTIMISATION 1+5 — Two-tier cache: memory dict → disk → API
# A company is NEVER re-fetched unless the user clicks "Refresh Cache".
# ─────────────────────────────────────────────────────────────────────────────
def enrich_one(cn: str) -> Optional[Dict]:
    token = st.session_state.refresh_token

    # Tier 1: in-memory cache (fastest — no I/O)
    mem = st.session_state.disk_cache.get(cn)
    if mem and mem.get("_cache_token") == token:
        return mem

    # Tier 2: disk cache (fast — already screened in a previous session)
    disk = _read_disk_cache(cn)
    if disk and disk.get("_cache_token") == token:
        st.session_state.disk_cache[cn] = disk
        return disk

    # Tier 3: live API call — filter on authoritative profile data first
    profile = api_profile(cn)

    status = str(profile.get("company_status", "")).lower()
    ctype  = str(profile.get("type", "")).lower()
    if status != "active" or ctype not in ACCEPTED_COMPANY_TYPES:
        return None

    # SPEED OPTIMISATION 6 — fetch officers and PSCs concurrently (2 calls at once)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_officers = pool.submit(api_officers, cn)
        f_pscs     = pool.submit(api_pscs, cn)
        officers   = f_officers.result()
        pscs       = f_pscs.result()

    sics      = {str(c) for c in (profile.get("sic_codes") or [])}
    psc_flags = screen_pscs(pscs)
    off_flags = screen_officers(officers)
    pub_flags = decide_publish(psc_flags, off_flags, sics)
    row       = build_row(cn, profile, psc_flags, off_flags, pub_flags)
    row["_cache_token"] = token

    # Write to both tiers
    st.session_state.disk_cache[cn] = row
    _write_disk_cache(cn, row)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# SPEED OPTIMISATION 7 — parallel company enrichment with a thread pool
# Companies are enriched 10 at a time concurrently rather than one by one.
# ─────────────────────────────────────────────────────────────────────────────
def enrich_all(search_rows: List[Dict]) -> pd.DataFrame:
    # Deduplicate by company number
    seen:   Set[str]  = set()
    unique: List[str] = []
    for r in search_rows:
        cn = str(r.get("company_number", "")).strip()
        if cn and cn not in seen:
            seen.add(cn)
            unique.append(cn)

    if not unique:
        st.warning("No company numbers returned by search.")
        return pd.DataFrame()

    # SPEED OPTIMISATION 8 — skip companies already in disk cache for this token
    # Only new/uncached companies need an API call — massively reduces work on repeat runs.
    token      = st.session_state.refresh_token
    to_fetch:  List[str] = []
    cached_rows: List[Dict] = []

    for cn in unique:
        cached = st.session_state.disk_cache.get(cn) or _read_disk_cache(cn)
        if cached and cached.get("_cache_token") == token and cached.get("Company Name", "").strip():
            cached_rows.append(cached)
        else:
            to_fetch.append(cn)

    total     = len(to_fetch)
    new_rows: List[Dict] = []

    if total:
        progress  = st.progress(0)
        status_el = st.empty()
        completed = 0

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(enrich_one, cn): cn for cn in to_fetch}
            for future in as_completed(futures):
                completed += 1
                progress.progress(completed / total)
                status_el.caption(f"Enriching {completed} / {total} new companies…")
                try:
                    row = future.result()
                    if row and row.get("Company Name", "").strip():
                        new_rows.append(row)
                except Exception:
                    pass

        progress.empty()
        status_el.empty()

    all_rows = cached_rows + new_rows
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
# UI COMPONENTS
# ─────────────────────────────────────────────────────────────────────────────
def render_sidebar() -> Dict[str, Any]:
    with st.sidebar:
        st.header("📅 Incorporation date")
        incorporated_from = st.date_input(
            "Companies incorporated from",
            value=date.today(),
            max_value=date.today(),
            help="Only companies incorporated on or after this date will be screened.",
        )

        st.divider()
        st.header("ℹ️ Active configuration")
        st.caption(
            f"**Target SIC codes:** {len(TARGET_SICS)}  \n"
            f"**Target countries:** {len(TARGET_COUNTRIES)}  \n"
            f"**Excluded:** Nigeria, Pakistan, Turkey  \n"
            f"**Types:** Private Ltd, LLP (Active only)"
        )

        st.divider()
        run = st.button("🔍 Run new search", use_container_width=True, type="primary")

        st.divider()
        col1, col2 = st.columns(2)
        refresh = col1.button("🔄 Refresh cache", use_container_width=True,
                              help="Forces all companies to be re-fetched from the API on next run.")
        clear   = col2.button("🗑 Clear results", use_container_width=True,
                              help="Removes all saved results and clears the screen.")

        if refresh:
            st.session_state.refresh_token    += 1
            st.session_state.last_refreshed_at = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
            # Clear in-memory cache so new token takes effect immediately
            st.session_state.disk_cache = {}
            st.success(f"Cache refreshed. Token: {st.session_state.refresh_token}")

        if clear:
            st.session_state.results_df = pd.DataFrame()
            if RESULTS_FILE.exists():
                RESULTS_FILE.unlink()
            for f in DISK_CACHE_DIR.glob("*.json"):
                f.unlink()
            st.session_state.disk_cache = {}
            st.success("All results and cache cleared.")

        st.divider()
        cached_count = len(list(DISK_CACHE_DIR.glob("*.json")))
        st.caption(
            f"🔑 Keys loaded: `{st.session_state.get('active_key_count', '—')}`  \n"
            f"💾 Companies in cache: `{cached_count}`  \n"
            f"Cache token: `{st.session_state.refresh_token}`  \n"
            f"Last refresh: {st.session_state.last_refreshed_at or 'never'}"
        )

        if RESULTS_FILE.exists():
            mtime = datetime.fromtimestamp(RESULTS_FILE.stat().st_mtime)
            st.caption(f"Results last saved: {mtime.strftime('%d %b %Y %H:%M')}")

    return {"incorporated_from": incorporated_from, "run": run}


def render_kpis(df: pd.DataFrame) -> None:
    if df.empty:
        return
    publishable = int((df["Publishable"] == "✅ Yes").sum()) if "Publishable" in df.columns else 0
    owned_co    = int(df["Owned by Another Company"].str.startswith("👨").sum()) if "Owned by Another Company" in df.columns else 0
    psc_target  = int((df["PSC from Target Country"] != "—").sum()) if "PSC from Target Country" in df.columns else 0
    dir_target  = int(((df.get("Director Nationality", "—") != "—") | (df.get("Director Residency", "—") != "—")).sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Companies screened",         len(df))
    c2.metric("Publishable",                publishable)
    c3.metric("Owned by a company",         owned_co)
    c4.metric("PSC target country",         psc_target)
    c5.metric("Director target match", dir_target)


_TABLE_COLS = [
    "Company Name",
    "Director Nationality",
    "Director Residency",
    "Owned by Another Company",
    "PSC from Target Country",
    "SIC Codes",
]


def render_results(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No results yet — set an incorporation date and press **Run new search**.")
        return

    st.subheader(f"Results — {len(df)} companies")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        show_pub = st.selectbox("Publishable", ["All", "Publishable only", "Not publishable only"])
    with fc2:
        show_owned = st.selectbox("Owned by another company", ["All", "Owned by company only", "Not owned by company"])
    with fc3:
        show_target = st.selectbox("PSC / Director nationality", ["All", "Target nationality present", "No target nationality"])

    view = df.copy()
    if show_pub == "Publishable only":
        view = view[view["Publishable"] == "✅ Yes"]
    elif show_pub == "Not publishable only":
        view = view[view["Publishable"] == "❌ No"]

    if show_owned == "Owned by company only":
        view = view[view["Owned by Another Company"].str.startswith("👨")]
    elif show_owned == "Not owned by company":
        view = view[view["Owned by Another Company"] == "No"]

    if show_target == "Target nationality present":
        view = view[(view["PSC from Target Country"] != "—") | (view["Director Nationality"] != "—") | (view["Director Residency"] != "—")]
    elif show_target == "No target nationality":
        view = view[(view["PSC from Target Country"] == "—") & (view["Director Nationality"] == "—") & (view["Director Residency"] == "—")]

    cols = [c for c in _TABLE_COLS if c in view.columns]
    st.dataframe(view[cols], use_container_width=True, height=560)
    st.caption(f"Showing {len(view)} of {len(df)} companies after filters.")

    # CSV export includes all fields including hidden reference columns
    st.download_button(
        "⬇ Download as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="ch_screening_results.csv",
        mime="text/csv",
    )


def render_rules() -> None:
    with st.expander("📋 Speed optimisations active"):
        st.markdown("""
| # | Optimisation | Effect |
|---|---|---|
| 1 | Per-company disk cache under `.ch_cache/` | Companies screened once — never re-fetched unless refreshed |
| 2 | Persistent HTTP sessions with TCP keep-alive | Eliminates connection overhead on every request |
| 3 | Automatic retry with exponential backoff | Recovers from 429/5xx without crashing |
| 4 | SIC code searches run concurrently (5 threads) | 19 SIC searches complete ~4× faster |
| 5 | Two-tier cache: memory dict → disk → API | Zero API calls for already-seen companies |
| 6 | Officers + PSC fetched concurrently per company | Cuts per-company API time roughly in half |
| 7 | Company enrichment runs 10 at a time in parallel | Bulk enrichment ~10× faster than sequential |
| 8 | Pre-flight cache check before thread pool | Only new companies hit the API — instant for repeat runs |
        """)

    with st.expander("📋 Screening rules reference"):
        st.markdown(f"""
**Target SIC codes ({len(TARGET_SICS)}):** `{", ".join(sorted(TARGET_SICS))}`

**Target countries ({len(TARGET_COUNTRIES)}):** `{", ".join(sorted(TARGET_COUNTRIES))}`

**Excluded countries:** Nigeria, Pakistan, Turkey

| Rule | Result |
|---|---|
| PSC tab contains a legal entity | 👨‍👧 Owned by Another Company = Yes |
| PSC nationality matches a target country | 🌍 PSC from Target Country populated |
| Director nationality/residency matches a target country | 🌍 Director Nationality populated |
| SIC 62012 or 72110, no excluded country | Always published |
| Excluded country found anywhere | Not published |
| Not Active or not Private Ltd / LLP | Skipped |
        """)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    _init_session()
    _get_key_rotator()

    st.title("🏢 Companies House Screening Tool")
    st.caption(
        f"Active · Private Ltd & LLP · {len(TARGET_SICS)} SIC codes · "
        f"{len(TARGET_COUNTRIES)} target countries · Nigeria / Pakistan / Turkey suppressed · "
        f"{st.session_state.get('active_key_count', '?')} API key(s) rotating · "
        f"8 speed optimisations active"
    )

    controls = render_sidebar()
    st.divider()

    if controls["run"]:
        with st.spinner(
            f"Searching for companies incorporated from "
            f"{controls['incorporated_from'].strftime('%d %b %Y')}…"
        ):
            try:
                raw = api_search_by_date(controls["incorporated_from"])
            except requests.HTTPError as exc:
                st.error(f"Search failed: {exc}")
                st.stop()

        if not raw:
            st.warning("No companies returned. Try an earlier incorporation date.")
        else:
            st.info(f"Found **{len(raw)}** raw results. Checking cache and enriching new companies…")
            new_df = enrich_all(raw)

            if not new_df.empty:
                existing = st.session_state.results_df
                if not existing.empty and "Company Name" in existing.columns:
                    combined = pd.concat([existing, new_df], ignore_index=True)
                    combined = combined.drop_duplicates(subset=["Company Name"], keep="last")
                else:
                    combined = new_df

                if "_added_at" in combined.columns:
                    combined = combined.sort_values("_added_at", ascending=False).reset_index(drop=True)
                st.session_state.results_df = combined
                _save_results(combined)
                st.success(f"✅ Done. {len(new_df)} companies processed.")

    render_kpis(st.session_state.results_df)
    render_results(st.session_state.results_df)
    render_rules()


if __name__ == "__main__":
    main()
