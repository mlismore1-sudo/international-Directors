"""
Companies House Screening Tool
================================
Run:     streamlit run companies_house_screening.py
Secrets: COMPANIES_HOUSE_API_KEY_1 / _2 / _3 in .streamlit/secrets.toml
Install: pip install streamlit requests pandas
"""

import re
import time
from datetime import date, datetime
from itertools import cycle
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
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
COMPANIES_HOUSE_BASE = "https://api.company-information.service.gov.uk"
REQUEST_TIMEOUT      = 20
CACHE_TTL_SECONDS    = 43_200  # 12 hours

# Target SIC codes — all are candidates if nationality/ownership criteria met
TARGET_SICS: Set[str] = {
    "62012", "62020", "63120", "47910", "46190", "46499",
    "70229", "73110", "74909", "68209", "64209", "68100",
    "32990", "10890", "86900", "93130", "96040", "82990",
    "72110",
}

# Always publish these two SIC codes unless an excluded country is matched
ALWAYS_PUBLISH_SICS: Set[str] = {"62012", "72110"}

# Target countries — PSC nationality or director residency triggers a flag
TARGET_COUNTRIES: Set[str] = {
    "china", "france", "germany", "belgium", "netherlands",
    "spain", "portugal", "lithuania", "poland", "norway",
    "finland", "denmark", "sweden", "united states",
    "india", "singapore", "hong kong",
}

# Excluded countries — suppress publication even for always-publish SICs
EXCLUDED_COUNTRIES: Set[str] = {"nigeria", "pakistan", "turkey"}

# Accepted company types
ACCEPTED_COMPANY_TYPES: Set[str] = {
    "ltd",                       # Private Limited Company
    "llp",                       # Limited Liability Partnership
    "private-limited-company",
    "limited-liability-partnership",
}

_COUNTRY_ALIASES: Dict[str, str] = {
    "turkiye":           "turkey",
    "türkiye":           "turkey",
    "uae":               "united arab emirates",
    "u.a.e.":            "united arab emirates",
    "england":           "united kingdom",
    "scotland":          "united kingdom",
    "wales":             "united kingdom",
    "northern ireland":  "united kingdom",
    "usa":               "united states",
    "u.s.a.":            "united states",
    "united states of america": "united states",
    "us":                "united states",
    "hk":                "hong kong",
    "prc":               "china",
    "peoples republic of china": "china",
    "people's republic of china": "china",
}

_LEGAL_KIND_MARKERS: List[str] = [
    "corporate-entity", "legal-person", "firm", "super-secure",
]

_CORPORATE_NAME_MARKERS: List[str] = [
    " ltd", " limited", " llp", " plc", " inc",
    " gmbh", " sarl", " bv", " ag", " oy", " spa",
    " srl", " as ", " ab ", " oy ", " nv ",
]

# ─────────────────────────────────────────────────────────────────────────────
# API KEY ROTATION  (reads from Streamlit secrets)
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
                "Add the following to your Streamlit secrets:\n"
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

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
def _init_session() -> None:
    defaults: Dict[str, Any] = {
        "company_cache":     {},
        "refresh_token":     0,
        "results_df":        pd.DataFrame(),
        "last_refreshed_at": None,
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


_NORM_TARGET_COUNTRIES:   Set[str] = _norm_set(TARGET_COUNTRIES)
_NORM_EXCLUDED_COUNTRIES: Set[str] = _norm_set(EXCLUDED_COUNTRIES)


def _is_target(value: Optional[str]) -> bool:
    return _normalise(value) in _NORM_TARGET_COUNTRIES


def _is_excluded(value: Optional[str]) -> bool:
    return _normalise(value) in _NORM_EXCLUDED_COUNTRIES

# ─────────────────────────────────────────────────────────────────────────────
# COMPANIES HOUSE API HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.auth = (api_key, "")
    s.headers.update({"Accept": "application/json"})
    return s


def _get(url: str, params: Optional[Dict] = None) -> Dict:
    api_key = _next_key()
    resp    = _make_session(api_key).get(url, params=params, timeout=REQUEST_TIMEOUT)
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
        time.sleep(0.05)
    return out


def api_search_by_incorporation_date(incorporated_from: date) -> List[Dict]:
    """
    Search Companies House advanced search filtering by incorporation date,
    active status, company type, and SIC codes.
    Returns all matching companies (no artificial cap).
    """
    all_results: List[Dict] = []
    date_str    = incorporated_from.strftime("%Y-%m-%d")

    for sic in TARGET_SICS:
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
            all_results.extend(batch)

            total_declared = data.get("total_results", 0)
            start += len(batch)
            if not batch or start >= total_declared:
                break
            time.sleep(0.05)

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
# COMPANY TYPE FILTER
# ─────────────────────────────────────────────────────────────────────────────
def _is_accepted_type(company_type: Optional[str]) -> bool:
    if not company_type:
        return False
    ct = company_type.strip().lower()
    return ct in ACCEPTED_COMPANY_TYPES

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
    excluded_flag  = False
    legal_names:   List[str] = []
    nationalities: List[str] = []
    excluded_nat:  List[str] = []

    for psc in pscs:
        if _psc_is_legal_entity(psc):
            legal_flag = True
            legal_names.append(str(psc.get("name", "Unknown PSC")))

        nat = psc.get("nationality")
        if _is_target(nat):
            nat_flag = True
            nationalities.append(str(nat))
        if _is_excluded(nat):
            excluded_flag = True
            excluded_nat.append(str(nat))

    return {
        "psc_legal_entity_flag":     legal_flag,
        "psc_legal_entity_names":    "; ".join(legal_names),
        "psc_nationality_flag":      nat_flag,
        "psc_matched_nationalities": "; ".join(nationalities),
        "psc_excluded_flag":         excluded_flag,
        "psc_excluded_nationalities": "; ".join(excluded_nat),
    }


def screen_officers(officers: List[Dict]) -> Dict:
    names:         List[str] = []
    residencies:   List[str] = []
    excluded_dirs: List[str] = []
    excluded_res:  List[str] = []

    for o in officers:
        if str(o.get("officer_role", "")).lower() != "director":
            continue
        res = o.get("country_of_residence") or o.get("usual_residential_country")
        if _is_target(res):
            names.append(str(o.get("name", "Unknown")))
            residencies.append(str(res))
        if _is_excluded(res):
            excluded_dirs.append(str(o.get("name", "Unknown")))
            excluded_res.append(str(res))

    return {
        "director_residency_flag":      bool(names),
        "director_matched_names":       "; ".join(names),
        "director_matched_residencies": "; ".join(residencies),
        "director_excluded_flag":       bool(excluded_dirs),
        "director_excluded_names":      "; ".join(excluded_dirs),
        "director_excluded_residencies": "; ".join(excluded_res),
    }


def decide_publish(profile: Dict, psc: Dict, officers: Dict) -> Dict:
    sics           = {str(c) for c in (profile.get("sic_codes") or [])}
    always_publish = bool(sics & ALWAYS_PUBLISH_SICS)
    excluded       = psc["psc_excluded_flag"] or officers["director_excluded_flag"]
    any_flag       = (
        psc["psc_legal_entity_flag"]
        or psc["psc_nationality_flag"]
        or officers["director_residency_flag"]
    )

    # Excluded country always wins — suppresses even always-publish SICs
    if excluded:
        excluded_countries = "; ".join(filter(None, [
            psc.get("psc_excluded_nationalities", ""),
            officers.get("director_excluded_residencies", ""),
        ]))
        return {
            "should_publish":  False,
            "publish_reason":  f"Suppressed — excluded country detected: {excluded_countries}",
            "flag_type":       "excluded",
        }

    if always_publish:
        return {
            "should_publish": True,
            "publish_reason": "SIC 62012/72110 retained — no excluded country",
            "flag_type":      "sic-retained",
        }

    if any_flag:
        reasons = []
        if psc["psc_legal_entity_flag"]:
            reasons.append("PSC legal entity")
        if psc["psc_nationality_flag"]:
            reasons.append(f"PSC nationality: {psc['psc_matched_nationalities']}")
        if officers["director_residency_flag"]:
            reasons.append(f"Director residency: {officers['director_matched_residencies']}")
        return {
            "should_publish": True,
            "publish_reason": "; ".join(reasons),
            "flag_type":      "screening-match",
        }

    return {
        "should_publish": False,
        "publish_reason": "No screening rule matched",
        "flag_type":      "no-match",
    }


def build_row(cn: str, profile: Dict, psc: Dict, off: Dict, pub: Dict) -> Dict:
    sics = profile.get("sic_codes") or []
    addr = profile.get("registered_office_address") or {}
    return {
        "company_number":               cn,
        "company_name":                 profile.get("company_name", ""),
        "company_type":                 profile.get("type", ""),
        "status":                       profile.get("company_status", ""),
        "incorporated":                 profile.get("date_of_creation", ""),
        "sic_codes":                    ", ".join(map(str, sics)),
        "address":                      ", ".join(str(v) for v in addr.values() if v),
        "psc_legal_entity_flag":        psc["psc_legal_entity_flag"],
        "psc_legal_entity_names":       psc["psc_legal_entity_names"],
        "psc_nationality_flag":         psc["psc_nationality_flag"],
        "psc_matched_nationalities":    psc["psc_matched_nationalities"],
        "psc_excluded_flag":            psc["psc_excluded_flag"],
        "psc_excluded_nationalities":   psc["psc_excluded_nationalities"],
        "director_residency_flag":      off["director_residency_flag"],
        "director_matched_names":       off["director_matched_names"],
        "director_matched_residencies": off["director_matched_residencies"],
        "director_excluded_flag":       off["director_excluded_flag"],
        "director_excluded_names":      off["director_excluded_names"],
        "should_publish":               pub["should_publish"],
        "publish_reason":               pub["publish_reason"],
        "flag_type":                    pub["flag_type"],
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
    # Deduplicate by company number before any API enrichment
    seen:   Set[str]  = set()
    unique: List[str] = []
    for r in search_rows:
        cn = str(r.get("company_number", "")).strip()
        if cn and cn not in seen:
            # Pre-filter: must be active and accepted company type
            status = str(r.get("company_status", "")).lower()
            ctype  = str(r.get("company_type", r.get("type", ""))).lower()
            if status != "active":
                continue
            if not _is_accepted_type(ctype):
                continue
            seen.add(cn)
            unique.append(cn)

    if not unique:
        st.warning("No valid active Ltd/LLP companies found after filtering.")
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
                "company_number": cn, "company_name": "", "company_type": "",
                "status": "", "incorporated": "", "sic_codes": "", "address": "",
                "psc_legal_entity_flag": False, "psc_legal_entity_names": "",
                "psc_nationality_flag": False, "psc_matched_nationalities": "",
                "psc_excluded_flag": False, "psc_excluded_nationalities": "",
                "director_residency_flag": False, "director_matched_names": "",
                "director_matched_residencies": "", "director_excluded_flag": False,
                "director_excluded_names": "", "should_publish": False,
                "publish_reason": f"API error: {exc}", "flag_type": "error",
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
        st.header("📅 Incorporation date")
        incorporated_from = st.date_input(
            "Show companies incorporated from",
            value=date(date.today().year, 1, 1),
            max_value=date.today(),
            help="Only companies incorporated on or after this date will be searched and screened.",
        )

        st.divider()
        st.header("ℹ️ Active filters")
        st.caption(
            f"**SIC codes:** {len(TARGET_SICS)} target codes  \n"
            f"**Target countries:** {len(TARGET_COUNTRIES)}  \n"
            f"**Excluded countries:** Nigeria, Pakistan, Turkey  \n"
            f"**Company types:** Private Ltd, LLP  \n"
            f"**Status:** Active only"
        )

        st.divider()
        col1, col2 = st.columns(2)
        run     = col1.button("🔍 Run", use_container_width=True, type="primary")
        refresh = col2.button("🔄 Refresh", use_container_width=True,
                              help="Clears cache — companies will be re-fetched from the API on next run.")

        if refresh:
            st.session_state.refresh_token    += 1
            st.session_state.last_refreshed_at = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
            st.success(f"Cache cleared. Token: {st.session_state.refresh_token}")

        st.divider()
        st.caption(
            f"🔑 Keys loaded: `{st.session_state.get('active_key_count', '—')}`  \n"
            f"Cache token: `{st.session_state.refresh_token}`  \n"
            f"Last refresh: {st.session_state.last_refreshed_at or 'never this session'}"
        )

    return {
        "incorporated_from": incorporated_from,
        "run":               run,
    }


def render_kpis(df: pd.DataFrame) -> None:
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    def _n(col: str) -> int:
        return int(df[col].sum()) if (not df.empty and col in df.columns) else 0
    c1.metric("Companies screened",       len(df))
    c2.metric("PSC legal entities",       _n("psc_legal_entity_flag"))
    c3.metric("PSC nationality flags",    _n("psc_nationality_flag"))
    c4.metric("Director residency flags", _n("director_residency_flag"))
    c5.metric("Excluded country hits",    _n("psc_excluded_flag") + _n("director_excluded_flag"))
    c6.metric("Publishable",              _n("should_publish"))


_DISPLAY_COLS = [
    "company_name", "company_number", "company_type", "incorporated", "sic_codes",
    "psc_legal_entity_flag", "psc_legal_entity_names",
    "psc_nationality_flag",  "psc_matched_nationalities",
    "psc_excluded_flag",     "psc_excluded_nationalities",
    "director_residency_flag", "director_matched_names", "director_matched_residencies",
    "director_excluded_flag", "director_excluded_names",
    "should_publish", "publish_reason", "flag_type", "address", "ch_url",
]


def _show_df(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No companies in this category.")
        return
    cols = [c for c in _DISPLAY_COLS if c in df.columns]
    st.dataframe(df[cols], use_container_width=True, height=500)


def render_results(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No results yet — set an incorporation date and press **Run**.")
        return

    n_pub      = int(df["should_publish"].sum())      if "should_publish"      in df.columns else 0
    n_excluded = int(df["psc_excluded_flag"].sum()) + int(df["director_excluded_flag"].sum()) \
                 if "psc_excluded_flag" in df.columns else 0
    n_legal    = int(df["psc_legal_entity_flag"].sum()) if "psc_legal_entity_flag" in df.columns else 0
    mask_none  = (
        ~df.get("psc_legal_entity_flag",    pd.Series(False, index=df.index)).astype(bool)
        & ~df.get("psc_nationality_flag",   pd.Series(False, index=df.index)).astype(bool)
        & ~df.get("director_residency_flag", pd.Series(False, index=df.index)).astype(bool)
    )

    t_pub, t_legal, t_geo, t_excl, t_all, t_none = st.tabs([
        f"✅ Publishable ({n_pub})",
        f"🏢 PSC legal entity ({n_legal})",
        f"🌍 Geography flags ({int(df.get('psc_nationality_flag', pd.Series(False)).sum() + df.get('director_residency_flag', pd.Series(False)).sum())})",
        f"🚫 Excluded ({n_excluded})",
        f"📋 All ({len(df)})",
        f"⬜ Unflagged ({int(mask_none.sum())})",
    ])

    with t_pub:
        _show_df(df[df["should_publish"]] if "should_publish" in df.columns else df.iloc[0:0])

    with t_legal:
        _show_df(df[df["psc_legal_entity_flag"]] if "psc_legal_entity_flag" in df.columns else df.iloc[0:0])

    with t_geo:
        if "psc_nationality_flag" in df.columns and "director_residency_flag" in df.columns:
            geo_mask = df["psc_nationality_flag"] | df["director_residency_flag"]
            _show_df(df[geo_mask])
        else:
            st.info("No geography data.")

    with t_excl:
        if "psc_excluded_flag" in df.columns:
            excl_mask = df["psc_excluded_flag"] | df["director_excluded_flag"]
            _show_df(df[excl_mask])
        else:
            st.info("No excluded companies.")

    with t_all:
        _show_df(df)

    with t_none:
        _show_df(df[mask_none])

    st.download_button(
        "⬇ Download full results as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="ch_screening_results.csv",
        mime="text/csv",
    )


def render_rules() -> None:
    with st.expander("📋 Screening rules reference"):
        st.markdown(f"""
**Target SIC codes ({len(TARGET_SICS)}):**
`{", ".join(sorted(TARGET_SICS))}`

**Target countries ({len(TARGET_COUNTRIES)}):**
`{", ".join(sorted(TARGET_COUNTRIES))}`

**Excluded countries:** Nigeria, Pakistan, Turkey

| Rule | Outcome |
|---|---|
| PSC tab contains a legal entity (corporate kind, Ltd/LLP name, or corporate control with no nationality) | `psc_legal_entity_flag = True` → published |
| PSC nationality matches a target country | `psc_nationality_flag = True` → published |
| Any director resident in a target country | `director_residency_flag = True` → published |
| SIC 62012 or 72110 and no excluded country | Always published regardless of other flags |
| Any excluded country (Nigeria / Pakistan / Turkey) detected anywhere | **Suppressed** — not published |
| Company is not Active | Skipped before enrichment |
| Company is not Private Ltd or LLP | Skipped before enrichment |
| Same company number appears twice in search results | Processed once — duplicate skipped |
| Company already in cache and refresh not triggered | Cached result reused — no API call made |
        """)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    _init_session()
    _get_key_rotator()  # validates keys on load — stops cleanly if missing

    st.title("🏢 Companies House Screening Tool")
    st.caption(
        f"Active · Private Ltd & LLP only · {len(TARGET_SICS)} target SIC codes · "
        f"{len(TARGET_COUNTRIES)} target countries · "
        f"Nigeria / Pakistan / Turkey suppressed · "
        f"{st.session_state.get('active_key_count', '?')} API key(s) rotating"
    )

    controls = render_sidebar()

    st.markdown(
        f"**Incorporation from:** `{controls['incorporated_from'].strftime('%d %b %Y')}` · "
        f"Cache token: `{st.session_state.refresh_token}` · "
        f"Last refresh: `{st.session_state.last_refreshed_at or 'never'}`"
    )
    st.divider()

    if controls["run"]:
        with st.spinner(f"Searching Companies House for companies incorporated from {controls['incorporated_from']}…"):
            try:
                raw = api_search_by_incorporation_date(controls["incorporated_from"])
            except requests.HTTPError as exc:
                st.error(f"Search failed: {exc}")
                st.stop()

        if not raw:
            st.warning("No companies returned. Try an earlier incorporation date.")
        else:
            st.info(f"Found **{len(raw)}** raw results. Deduplicating, filtering, and enriching…")
            st.session_state.results_df = enrich_all(raw)

    render_kpis(st.session_state.results_df)
    render_results(st.session_state.results_df)
    render_rules()


if __name__ == "__main__":
    main()
