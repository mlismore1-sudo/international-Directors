"""
Companies House Screening Tool
================================
Run:     streamlit run companies_house_screening.py
Env vars:
    COMPANIES_HOUSE_API_KEY_1=<key one>
    COMPANIES_HOUSE_API_KEY_2=<key two>
    COMPANIES_HOUSE_API_KEY_3=<key three>
Install: pip install streamlit requests pandas
"""

import os
import re
import time
from datetime import datetime
from itertools import cycle
from typing import Any, Dict, Iterator, List, Optional, Set

import pandas as pd
import requests
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG  (must be the very first Streamlit call)
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CH Screening Tool",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
COMPANIES_HOUSE_BASE  = "https://api.company-information.service.gov.uk"
REQUEST_TIMEOUT       = 20
CACHE_TTL_SECONDS     = 43_200   # 12 hours

_DEFAULT_TARGET_COUNTRIES: Set[str] = {
    "nigeria", "pakistan", "turkey",
    "india", "china", "russia",
    "united arab emirates",
}

_PRIORITY_EXCEPTION_COUNTRIES: Set[str] = {"nigeria", "pakistan", "turkey"}
_ALWAYS_PUBLISH_SICS: Set[str]           = {"62012", "72110"}

_COUNTRY_ALIASES: Dict[str, str] = {
    "turkiye":          "turkey",
    "türkiye":          "turkey",
    "uae":              "united arab emirates",
    "u.a.e.":           "united arab emirates",
    "england":          "united kingdom",
    "scotland":         "united kingdom",
    "wales":            "united kingdom",
    "northern ireland": "united kingdom",
}

_LEGAL_KIND_MARKERS: List[str] = [
    "corporate-entity", "legal-person", "firm", "super-secure",
]

_CORPORATE_NAME_MARKERS: List[str] = [
    " ltd", " limited", " llp", " plc", " inc",
    " gmbh", " sarl", " bv", " ag", " oy", " spa",
]

# ─────────────────────────────────────────────────────────────────────────────
# API KEY ROTATION
# Reads up to 3 keys from environment variables.
# All 3 must be set; requests rotate through them round-robin so rate limits
# are spread evenly across keys.
# ─────────────────────────────────────────────────────────────────────────────
def _load_api_keys() -> List[str]:
    keys = [
        os.environ.get("COMPANIES_HOUSE_API_KEY_1", "").strip(),
        os.environ.get("COMPANIES_HOUSE_API_KEY_2", "").strip(),
        os.environ.get("COMPANIES_HOUSE_API_KEY_3", "").strip(),
    ]
    return [k for k in keys if k]


def _get_key_rotator() -> Iterator[str]:
    """Returns a persistent round-robin iterator stored in session state."""
    if "key_rotator" not in st.session_state:
        keys = _load_api_keys()
        if not keys:
            st.error(
                "**No API keys found.**\n\n"
                "Set at least one of the following environment variables before launching:\n"
                "```\n"
                "export COMPANIES_HOUSE_API_KEY_1=your_first_key\n"
                "export COMPANIES_HOUSE_API_KEY_2=your_second_key\n"
                "export COMPANIES_HOUSE_API_KEY_3=your_third_key\n"
                "```"
            )
            st.stop()
        st.session_state.key_rotator = cycle(keys)
        st.session_state.active_key_count = len(keys)
    return st.session_state.key_rotator


def _next_key() -> str:
    return next(_get_key_rotator())

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
def _init_session() -> None:
    defaults: Dict[str, Any] = {
        "company_cache":           {},
        "refresh_token":           0,
        "results_df":              pd.DataFrame(),
        "last_refreshed_at":       None,
        "active_target_countries": _DEFAULT_TARGET_COUNTRIES.copy(),
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

# ─────────────────────────────────────────────────────────────────────────────
# COUNTRY NORMALISATION
# ─────────────────────────────────────────────────────────────────────────────
def _normalise(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value).strip().lower())
    return _COUNTRY_ALIASES.get(cleaned, cleaned)


def _norm_set(countries: Set[str]) -> Set[str]:
    return {_normalise(c) for c in countries}


def _is_target(value: Optional[str]) -> bool:
    targets = _norm_set(st.session_state.get("active_target_countries", _DEFAULT_TARGET_COUNTRIES))
    return _normalise(value) in targets


def _is_priority(value: Optional[str]) -> bool:
    return _normalise(value) in _norm_set(_PRIORITY_EXCEPTION_COUNTRIES)

# ─────────────────────────────────────────────────────────────────────────────
# COMPANIES HOUSE API HELPERS
# Each call pulls the next key from the rotator automatically.
# ─────────────────────────────────────────────────────────────────────────────
def _make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.auth = (api_key, "")
    s.headers.update({"Accept": "application/json"})
    return s


def _get(url: str, params: Optional[Dict] = None) -> Dict:
    """Single GET using the next key in rotation."""
    api_key = _next_key()
    resp    = _make_session(api_key).get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def _paginated(url: str, items_key: str = "items", page: int = 100) -> List[Dict]:
    """Paginated GET; each page request rotates to the next key."""
    out: List[Dict] = []
    start = 0
    while True:
        data  = _get(url, {"items_per_page": page, "start_index": start})
        batch = data.get(items_key) or []
        out.extend(batch)
        if len(batch) < page:
            break
        start += page
        time.sleep(0.05)
    return out


def api_search(query: str, max_results: int) -> List[Dict]:
    data = _get(
        f"{COMPANIES_HOUSE_BASE}/search/companies",
        {"q": query, "items_per_page": min(max_results, 100)},
    )
    return data.get("items") or []


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
    legal_flag     = False
    nat_flag       = False
    legal_names:   List[str] = []
    nationalities: List[str] = []
    priority:      Optional[str] = None

    for psc in pscs:
        if _psc_is_legal_entity(psc):
            legal_flag = True
            legal_names.append(str(psc.get("name", "Unknown PSC")))
        nat = psc.get("nationality")
        if _is_target(nat):
            nat_flag = True
            nationalities.append(str(nat))
            if _is_priority(nat) and not priority:
                priority = str(nat)

    return {
        "psc_legal_entity_flag":     legal_flag,
        "psc_legal_entity_names":    "; ".join(legal_names),
        "psc_nationality_flag":      nat_flag,
        "psc_matched_nationalities": "; ".join(nationalities),
        "psc_priority_country":      priority,
    }


def screen_officers(officers: List[Dict]) -> Dict:
    names:       List[str] = []
    residencies: List[str] = []
    priority:    Optional[str] = None

    for o in officers:
        if str(o.get("officer_role", "")).lower() != "director":
            continue
        res = o.get("country_of_residence") or o.get("usual_residential_country")
        if _is_target(res):
            names.append(str(o.get("name", "Unknown")))
            residencies.append(str(res))
            if _is_priority(res) and not priority:
                priority = str(res)

    return {
        "director_residency_flag":      bool(names),
        "director_matched_names":       "; ".join(names),
        "director_matched_residencies": "; ".join(residencies),
        "director_priority_country":    priority,
    }


def decide_publish(profile: Dict, psc: Dict, officers: Dict) -> Dict:
    sics           = {str(c) for c in (profile.get("sic_codes") or [])}
    always_publish = bool(sics & _ALWAYS_PUBLISH_SICS)
    priority       = psc.get("psc_priority_country") or officers.get("director_priority_country")
    any_flag       = (
        psc["psc_legal_entity_flag"]
        or psc["psc_nationality_flag"]
        or officers["director_residency_flag"]
    )

    if always_publish and not priority:
        return {
            "should_publish":           True,
            "publish_reason":           "SIC 62012/72110 retained — no priority-exception country",
            "priority_exception":       False,
            "matched_priority_country": None,
        }
    if priority:
        return {
            "should_publish":           any_flag,
            "publish_reason":           f"Priority exception country: {priority}",
            "priority_exception":       True,
            "matched_priority_country": priority,
        }
    return {
        "should_publish":           any_flag,
        "publish_reason":           "Screening rule matched" if any_flag else "No rule matched — not published",
        "priority_exception":       False,
        "matched_priority_country": None,
    }


def build_row(cn: str, profile: Dict, psc: Dict, off: Dict, pub: Dict) -> Dict:
    sics = profile.get("sic_codes") or []
    addr = profile.get("registered_office_address") or {}
    return {
        "company_number":               cn,
        "company_name":                 profile.get("company_name", ""),
        "status":                       profile.get("company_status", ""),
        "incorporated":                 profile.get("date_of_creation", ""),
        "sic_codes":                    ", ".join(map(str, sics)),
        "address":                      ", ".join(str(v) for v in addr.values() if v),
        "psc_legal_entity_flag":        psc["psc_legal_entity_flag"],
        "psc_legal_entity_names":       psc["psc_legal_entity_names"],
        "psc_nationality_flag":         psc["psc_nationality_flag"],
        "psc_matched_nationalities":    psc["psc_matched_nationalities"],
        "director_residency_flag":      off["director_residency_flag"],
        "director_matched_names":       off["director_matched_names"],
        "director_matched_residencies": off["director_matched_residencies"],
        "priority_exception":           pub["priority_exception"],
        "matched_priority_country":     pub["matched_priority_country"] or "",
        "should_publish":               pub["should_publish"],
        "publish_reason":               pub["publish_reason"],
        "ch_url":                       f"https://find-and-update.company-information.service.gov.uk/company/{cn}",
    }

# ─────────────────────────────────────────────────────────────────────────────
# CACHE-GUARDED ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────
def enrich_one(cn: str) -> Dict:
    cache = st.session_state.company_cache
    token = st.session_state.refresh_token
    entry = cache.get(cn)

    if entry and entry.get("token") == token and (time.time() - entry["ts"]) < CACHE_TTL_SECONDS:
        return entry["data"]

    profile  = api_profile(cn)
    officers = api_officers(cn)
    pscs     = api_pscs(cn)

    psc_flags = screen_pscs(pscs)
    off_flags = screen_officers(officers)
    pub_flags = decide_publish(profile, psc_flags, off_flags)
    row       = build_row(cn, profile, psc_flags, off_flags, pub_flags)

    cache[cn] = {"token": token, "ts": time.time(), "data": row}
    return row


def enrich_all(search_rows: List[Dict]) -> pd.DataFrame:
    seen:   Set[str]  = set()
    unique: List[str] = []
    for r in search_rows:
        cn = str(r.get("company_number", "")).strip()
        if cn and cn not in seen:
            seen.add(cn)
            unique.append(cn)

    if not unique:
        st.warning("No valid company numbers returned by search.")
        return pd.DataFrame()

    total    = len(unique)
    progress = st.progress(0)
    status   = st.empty()
    rows:    List[Dict] = []

    for idx, cn in enumerate(unique, 1):
        status.caption(f"Enriching {idx} / {total} — {cn}")
        try:
            rows.append(enrich_one(cn))
        except requests.HTTPError as exc:
            rows.append({
                "company_number": cn, "company_name": "", "status": "",
                "incorporated": "", "sic_codes": "", "address": "",
                "psc_legal_entity_flag": False, "psc_legal_entity_names": "",
                "psc_nationality_flag": False, "psc_matched_nationalities": "",
                "director_residency_flag": False, "director_matched_names": "",
                "director_matched_residencies": "", "priority_exception": False,
                "matched_priority_country": "", "should_publish": False,
                "publish_reason": f"API error: {exc}",
                "ch_url": f"https://find-and-update.company-information.service.gov.uk/company/{cn}",
            })
        progress.progress(idx / total)

    progress.empty()
    status.empty()
    return pd.DataFrame(rows)

# ─────────────────────────────────────────────────────────────────────────────
# UI COMPONENTS
# ─────────────────────────────────────────────────────────────────────────────
def render_sidebar() -> Dict[str, Any]:
    with st.sidebar:
        st.header("🔍 Search")
        query       = st.text_input("Company name or keyword", value="software development")
        max_results = st.slider("Max results to enrich", 10, 100, 30, step=10)

        st.header("🌍 Target countries")
        all_country_opts = sorted(
            _DEFAULT_TARGET_COUNTRIES | {"united kingdom", "france", "germany", "spain"}
        )
        selected_countries = st.multiselect(
            "Countries",
            options=all_country_opts,
            default=sorted(_DEFAULT_TARGET_COUNTRIES),
            help="PSC nationality and director country of residence are matched against these.",
        )

        st.header("🏷 SIC codes")
        sic_filter = st.multiselect(
            "Restrict search to SIC codes",
            options=["62012", "72110", "62020", "62090", "63110"],
            default=["62012", "72110"],
            help="62012 and 72110 are always published unless a priority-exception country is found.",
        )

        st.divider()
        col1, col2 = st.columns(2)
        run     = col1.button("Search",  use_container_width=True, type="primary")
        refresh = col2.button("Refresh", use_container_width=True,
                              help="Bumps the cache token so all companies are re-fetched on next search.")
        if refresh:
            st.session_state.refresh_token     += 1
            st.session_state.last_refreshed_at  = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
            st.success(f"Cache cleared — token is now {st.session_state.refresh_token}.")

        st.divider()
        key_count = st.session_state.get("active_key_count", "—")
        st.caption(
            f"🔑 API keys loaded: `{key_count}`  \n"
            f"Cache token: `{st.session_state.refresh_token}`  \n"
            f"Last manual refresh: {st.session_state.last_refreshed_at or 'never this session'}"
        )

    return {
        "query":              query,
        "max_results":        max_results,
        "selected_countries": set(selected_countries),
        "sic_filter":         set(sic_filter),
        "run":                run,
    }


def render_kpis(df: pd.DataFrame) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    def _n(col: str) -> int:
        return int(df[col].sum()) if (not df.empty and col in df.columns) else 0
    c1.metric("Companies",                len(df))
    c2.metric("PSC legal entities",       _n("psc_legal_entity_flag"))
    c3.metric("PSC nationality flags",    _n("psc_nationality_flag"))
    c4.metric("Director residency flags", _n("director_residency_flag"))
    c5.metric("Publishable",              _n("should_publish"))


_DISPLAY_COLS = [
    "company_name", "company_number", "sic_codes", "status",
    "psc_legal_entity_flag", "psc_legal_entity_names",
    "psc_nationality_flag",  "psc_matched_nationalities",
    "director_residency_flag", "director_matched_names", "director_matched_residencies",
    "priority_exception", "matched_priority_country",
    "should_publish", "publish_reason", "ch_url",
]


def _tab_df(df: pd.DataFrame) -> None:
    cols = [c for c in _DISPLAY_COLS if c in df.columns]
    st.dataframe(df[cols], use_container_width=True, height=480)


def render_results(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No results yet — enter a search term and press **Search**.")
        return

    n_pub  = int(df["should_publish"].sum())     if "should_publish"     in df.columns else 0
    n_pri  = int(df["priority_exception"].sum()) if "priority_exception" in df.columns else 0
    mask   = (
        ~df.get("psc_legal_entity_flag",    pd.Series(False, index=df.index)).astype(bool)
        & ~df.get("psc_nationality_flag",   pd.Series(False, index=df.index)).astype(bool)
        & ~df.get("director_residency_flag", pd.Series(False, index=df.index)).astype(bool)
    )
    n_none = int(mask.sum())

    t_all, t_pub, t_pri, t_none = st.tabs([
        f"All ({len(df)})",
        f"Publishable ({n_pub})",
        f"Priority exceptions ({n_pri})",
        f"Unflagged ({n_none})",
    ])

    with t_all:
        _tab_df(df)
    with t_pub:
        sub = df[df["should_publish"]] if "should_publish" in df.columns else df.iloc[0:0]
        _tab_df(sub) if not sub.empty else st.info("No publishable companies.")
    with t_pri:
        sub = df[df["priority_exception"]] if "priority_exception" in df.columns else df.iloc[0:0]
        _tab_df(sub) if not sub.empty else st.info("No priority-exception companies.")
    with t_none:
        sub = df[mask]
        _tab_df(sub) if not sub.empty else st.info("All companies have at least one flag.")

    st.download_button(
        "⬇ Download full results as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="ch_screening_results.csv",
        mime="text/csv",
    )


def render_rules() -> None:
    with st.expander("📋 Active screening rules"):
        st.markdown("""
| Rule | Behaviour |
|---|---|
| PSC tab contains a legal entity | `psc_legal_entity_flag = True` |
| PSC nationality matches a target country | `psc_nationality_flag = True` |
| Any director is resident in a target country | `director_residency_flag = True` |
| SIC 62012 or 72110, no priority-exception country | Always published regardless of flags |
| SIC 62012 or 72110 **and** Nigeria / Pakistan / Turkey detected | Published only if a flag is also set |
| No SIC match and no flags | Not published |
| Same company number in results twice | Processed once — duplicate skipped |
| Manual refresh not triggered | Cached enrichment reused for every lookup |
        """)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    _init_session()
    _get_key_rotator()  # validates keys immediately on load

    st.title("🏢 Companies House Screening Tool")
    st.caption(
        "Flags PSC legal entities · PSC nationality · director residency  |  "
        "SIC 62012 and 72110 always published unless Nigeria, Pakistan, or Turkey is detected.  |  "
        f"Running with {st.session_state.get('active_key_count', '?')} API key(s) in rotation."
    )

    controls = render_sidebar()
    st.session_state.active_target_countries = controls["selected_countries"]

    st.markdown(
        f"**Active filters** — "
        f"Query: `{controls['query'] or '—'}` · "
        f"SICs: `{', '.join(sorted(controls['sic_filter'])) or 'all'}` · "
        f"Target countries: `{', '.join(sorted(str(c) for c in controls['selected_countries'])) or 'none'}`"
    )
    st.divider()

    if controls["run"]:
        with st.spinner("Searching Companies House…"):
            try:
                raw = api_search(controls["query"], controls["max_results"])
            except requests.HTTPError as exc:
                st.error(f"Search request failed: {exc}")
                st.stop()

        if not raw:
            st.warning("No companies returned. Try a broader search term.")
        else:
            if controls["sic_filter"]:
                filtered = [r for r in raw if any(s in str(r) for s in controls["sic_filter"])]
                if not filtered:
                    filtered = raw
                    st.info("SIC filter matched no snippets — showing all returned companies.")
            else:
                filtered = raw

            st.session_state.results_df = enrich_all(filtered)

    render_kpis(st.session_state.results_df)
    render_results(st.session_state.results_df)
    render_rules()


if __name__ == "__main__":
    main()
