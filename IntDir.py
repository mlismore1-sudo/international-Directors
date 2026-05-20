"""
Companies House Screening Tool
================================
Run:     streamlit run companies_house_screening.py
Secrets: COMPANIES_HOUSE_API_KEY_1 / _2 / _3 in .streamlit/secrets.toml
Install: pip install streamlit requests pandas
"""

import json
import re
import time
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
# PERSISTENCE — results are saved to disk so they survive app restarts
# ─────────────────────────────────────────────────────────────────────────────
RESULTS_FILE = Path("ch_screening_results_saved.json")


def _save_results(df: pd.DataFrame) -> None:
    if df.empty:
        return
    RESULTS_FILE.write_text(df.to_json(orient="records", date_format="iso"), encoding="utf-8")


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
CACHE_TTL_SECONDS    = 43_200  # 12 hours

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

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
def _init_session() -> None:
    defaults: Dict[str, Any] = {
        "company_cache":     {},
        "refresh_token":     0,
        "last_refreshed_at": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

    # Load persisted results from disk on first run
    if "results_df" not in st.session_state:
        st.session_state.results_df = _load_saved_results()

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


_NORM_TARGETS:  Set[str] = _norm_set(TARGET_COUNTRIES)
_NORM_EXCLUDED: Set[str] = _norm_set(EXCLUDED_COUNTRIES)


def _is_target(value: Optional[str]) -> bool:
    return _normalise(value) in _NORM_TARGETS


def _is_excluded(value: Optional[str]) -> bool:
    return _normalise(value) in _NORM_EXCLUDED

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


def api_search_by_date(incorporated_from: date) -> List[Dict]:
    all_results: List[Dict] = []
    date_str = incorporated_from.strftime("%Y-%m-%d")

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
            total = data.get("total_results", 0)
            start += len(batch)
            if not batch or start >= total:
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
    legal_entities: List[str] = []
    target_nationals: List[str] = []
    excluded_found = False

    for psc in pscs:
        if _psc_is_legal_entity(psc):
            legal_entities.append(str(psc.get("name", "Unknown entity")))

        nat = psc.get("nationality")
        if _is_target(nat):
            name = str(psc.get("name", "Unknown"))
            target_nationals.append(f"{name} ({nat})")
        if _is_excluded(nat):
            excluded_found = True

    return {
        "owned_by_company":          bool(legal_entities),
        "owning_company_names":      "; ".join(legal_entities),
        "psc_from_target_country":   bool(target_nationals),
        "psc_target_details":        "; ".join(target_nationals),
        "psc_excluded":              excluded_found,
    }


def screen_officers(officers: List[Dict]) -> Dict:
    target_directors: List[str] = []
    excluded_found = False

    for o in officers:
        if str(o.get("officer_role", "")).lower() != "director":
            continue
        res = o.get("country_of_residence") or o.get("usual_residential_country")
        nat = o.get("nationality")
        # Check both country of residence and nationality for directors
        country_hit = res if _is_target(res) else (nat if _is_target(nat) else None)
        if country_hit:
            target_directors.append(f"{o.get('name', 'Unknown')} ({country_hit})")
        if _is_excluded(res) or _is_excluded(nat):
            excluded_found = True

    return {
        "director_from_target_country": bool(target_directors),
        "director_target_details":      "; ".join(target_directors),
        "director_excluded":            excluded_found,
    }


def decide_publish(psc: Dict, officers: Dict, sic_codes: Set[str]) -> Dict:
    always_publish = bool(sic_codes & ALWAYS_PUBLISH_SICS)
    excluded       = psc["psc_excluded"] or officers["director_excluded"]
    any_flag       = (
        psc["owned_by_company"]
        or psc["psc_from_target_country"]
        or officers["director_from_target_country"]
    )

    if excluded:
        return {"should_publish": False, "publish_reason": "Excluded country detected"}

    if always_publish:
        return {"should_publish": True, "publish_reason": "SIC 62012/72110 retained"}

    if any_flag:
        return {"should_publish": True, "publish_reason": "Screening criteria matched"}

    return {"should_publish": False, "publish_reason": "No criteria matched"}

# ─────────────────────────────────────────────────────────────────────────────
# BUILD OUTPUT ROW  — only the fields the user wants to see + metadata
# ─────────────────────────────────────────────────────────────────────────────
def build_row(cn: str, profile: Dict, psc: Dict, off: Dict, pub: Dict) -> Dict:
    sics = profile.get("sic_codes") or []
    return {
        # ── Core display columns ──────────────────────────────────────────
        "Company Name":              profile.get("company_name", ""),
        "SIC Codes":                 ", ".join(map(str, sics)),
        "Director Nationality":      "🌍 " + off["director_target_details"] if off["director_from_target_country"] else "—",
        "Owned by Another Company":  "👨‍👧 " + psc["owning_company_names"] if psc["owned_by_company"] else "No",
        "PSC from Target Country":   "🌍 " + psc["psc_target_details"] if psc["psc_from_target_country"] else "—",
        "Publishable":               "✅ Yes" if pub["should_publish"] else "❌ No",
        "Reason":                    pub["publish_reason"],
        # ── Reference / metadata ─────────────────────────────────────────
        "Company Number":            cn,
        "Incorporated":              profile.get("date_of_creation", ""),
        "CH Link":                   f"https://find-and-update.company-information.service.gov.uk/company/{cn}",
    }

# ─────────────────────────────────────────────────────────────────────────────
# CACHE-GUARDED ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────
def enrich_one(cn: str) -> Optional[Dict]:
    cache = st.session_state.company_cache
    token = st.session_state.refresh_token
    entry = cache.get(cn)

    if entry and entry.get("token") == token and (time.time() - entry["ts"]) < CACHE_TTL_SECONDS:
        return entry["data"]

    profile = api_profile(cn)

    # Filter using authoritative profile data — not unreliable search snippet fields
    status = str(profile.get("company_status", "")).lower()
    ctype  = str(profile.get("type", "")).lower()

    if status != "active":
        return None
    if ctype not in ACCEPTED_COMPANY_TYPES:
        return None

    officers = api_officers(cn)
    pscs     = api_pscs(cn)

    sics      = {str(c) for c in (profile.get("sic_codes") or [])}
    psc_flags = screen_pscs(pscs)
    off_flags = screen_officers(officers)
    pub_flags = decide_publish(psc_flags, off_flags, sics)
    row       = build_row(cn, profile, psc_flags, off_flags, pub_flags)

    cache[cn] = {"token": token, "ts": time.time(), "data": row}
    return row


def enrich_all(search_rows: List[Dict]) -> pd.DataFrame:
    # Deduplicate by company number only.
    # Do NOT pre-filter on raw search fields — the advanced-search endpoint
    # frequently omits company_type and company_status in its response items.
    # All filtering is done inside enrich_one using the full profile from /company/{cn}.
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

    total     = len(unique)
    progress  = st.progress(0)
    status_el = st.empty()
    rows: List[Dict] = []

    for idx, cn in enumerate(unique, 1):
        status_el.caption(f"Enriching {idx} / {total} — {cn}")
        try:
            row = enrich_one(cn)
            # Only keep rows that passed the profile-level filter and have a company name
            if row and row.get("Company Name", "").strip():
                rows.append(row)
        except requests.HTTPError:
            pass  # Skip quietly — avoids blank rows polluting the table
        progress.progress(idx / total)

    progress.empty()
    status_el.empty()
    return pd.DataFrame(rows) if rows else pd.DataFrame()

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
                              help="Forces all companies to be re-fetched from the API on the next run.")
        clear   = col2.button("🗑 Clear results", use_container_width=True,
                              help="Removes the saved results from disk and clears the screen.")

        if refresh:
            st.session_state.refresh_token    += 1
            st.session_state.last_refreshed_at = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
            st.success(f"Cache cleared. Token: {st.session_state.refresh_token}")

        if clear:
            st.session_state.results_df = pd.DataFrame()
            if RESULTS_FILE.exists():
                RESULTS_FILE.unlink()
            st.success("Saved results cleared.")

        st.divider()
        st.caption(
            f"🔑 Keys loaded: `{st.session_state.get('active_key_count', '—')}`  \n"
            f"Cache token: `{st.session_state.refresh_token}`  \n"
            f"Last refresh: {st.session_state.last_refreshed_at or 'never this session'}"
        )

        # Show when results were last saved
        if RESULTS_FILE.exists():
            mtime = datetime.fromtimestamp(RESULTS_FILE.stat().st_mtime)
            st.caption(f"💾 Results last saved: {mtime.strftime('%d %b %Y %H:%M')}")

    return {
        "incorporated_from": incorporated_from,
        "run":               run,
    }


def render_kpis(df: pd.DataFrame) -> None:
    if df.empty:
        return
    total      = len(df)
    publishable = int((df["Publishable"] == "✅ Yes").sum()) if "Publishable" in df.columns else 0
    owned_co   = int((df["Owned by Another Company"].str.startswith("Yes")).sum()) if "Owned by Another Company" in df.columns else 0
    psc_target = int((df["PSC from Target Country"] != "—").sum()) if "PSC from Target Country" in df.columns else 0
    dir_target = int((df["Director Nationality"] != "—").sum()) if "Director Nationality" in df.columns else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Companies screened", total)
    c2.metric("Publishable",        publishable)
    c3.metric("Owned by a company", owned_co)
    c4.metric("PSC target country", psc_target)
    c5.metric("Director target nationality", dir_target)


# Column order for the display table
_TABLE_COLS = [
    "Company Name",
    "SIC Codes",
    "Director Nationality",
    "Owned by Another Company",
    "PSC from Target Country",
    "Publishable",
]


def render_results(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No results yet — set an incorporation date and press **Run new search**.  \nPrevious results will appear here automatically when the app restarts.")
        return

    st.subheader(f"Results — {len(df)} companies")

    # Filter controls above the table
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        show_publishable = st.selectbox(
            "Publishable",
            ["All", "Publishable only", "Not publishable only"],
        )
    with fc2:
        show_owned = st.selectbox(
            "Owned by another company",
            ["All", "Owned by company only", "Not owned by company"],
        )
    with fc3:
        show_target = st.selectbox(
            "PSC / Director nationality",
            ["All", "Target nationality present", "No target nationality"],
        )

    view = df.copy()

    if show_publishable == "Publishable only":
        view = view[view["Publishable"] == "✅ Yes"]
    elif show_publishable == "Not publishable only":
        view = view[view["Publishable"] == "❌ No"]

    if show_owned == "Owned by company only":
        view = view[view["Owned by Another Company"].str.startswith("Yes")]
    elif show_owned == "Not owned by company":
        view = view[view["Owned by Another Company"] == "No"]

    if show_target == "Target nationality present":
        view = view[(view["PSC from Target Country"] != "—") | (view["Director Nationality"] != "—")]
    elif show_target == "No target nationality":
        view = view[(view["PSC from Target Country"] == "—") & (view["Director Nationality"] == "—")]

    cols = [c for c in _TABLE_COLS if c in view.columns]
    st.dataframe(view[cols], use_container_width=True, height=560)
    st.caption(f"Showing {len(view)} of {len(df)} companies after filters.")

    st.download_button(
        "⬇ Download as CSV",
        data=df[cols].to_csv(index=False).encode("utf-8"),
        file_name="ch_screening_results.csv",
        mime="text/csv",
    )


def render_rules() -> None:
    with st.expander("📋 Screening rules reference"):
        st.markdown(f"""
**Target SIC codes ({len(TARGET_SICS)}):** `{", ".join(sorted(TARGET_SICS))}`

**Target countries ({len(TARGET_COUNTRIES)}):** `{", ".join(sorted(TARGET_COUNTRIES))}`

**Excluded countries:** Nigeria, Pakistan, Turkey *(suppress publication even for always-publish SICs)*

| Rule | Result |
|---|---|
| PSC tab contains a legal entity (corporate kind, company-style name, or corporate control with no nationality) | "Owned by Another Company" = Yes |
| PSC nationality matches a target country | "PSC from Target Country" populated → published |
| Director nationality or country of residence matches a target country | "Director Nationality" populated → published |
| SIC 62012 or 72110, no excluded country | Always published |
| Excluded country found anywhere | Not published |
| Not Active, or not Private Ltd / LLP | Skipped before enrichment |
| Same company number appears twice | Enriched once only |
| App restarted without running a new search | Previous results reload automatically from disk |
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
        f"{st.session_state.get('active_key_count', '?')} API key(s) rotating"
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
            st.info(f"Found **{len(raw)}** raw results. Filtering and enriching…")
            new_df = enrich_all(raw)

            if not new_df.empty:
                # Merge new results with any existing saved results, deduplicating by company number
                existing = st.session_state.results_df
                if not existing.empty and "Company Number" in existing.columns:
                    combined = pd.concat([existing, new_df], ignore_index=True)
                    combined = combined.drop_duplicates(subset=["Company Number"], keep="last")
                else:
                    combined = new_df

                st.session_state.results_df = combined
                _save_results(combined)
                st.success(f"✅ {len(new_df)} companies enriched and saved. Results will persist across restarts.")

    render_kpis(st.session_state.results_df)
    render_results(st.session_state.results_df)
    render_rules()


if __name__ == "__main__":
    main()
