import os
import time
from datetime import date
from typing import List, Dict, Any, Optional

import requests
import streamlit as st
from dateutil.parser import parse as parse_date

# -----------------------------
# Configuration
# -----------------------------

# Allowed director countries of residence for the UI
ALLOWED_COUNTRIES = [
    "Finland",
    "Sweden",
    "Denmark",
    "Norway",
    "Germany",
    "Poland",
    "Spain",
    "France",
    "Italy",
    "Belgium",
    "Netherlands",
    "USA",
    "Hong Kong",
]

# Basic synonym mapping for officer.country_of_residence free-text values.
# Keys are the user-facing options; values are lists of acceptable variants.
COUNTRY_SYNONYMS = {
    "Finland": ["Finland"],
    "Sweden": ["Sweden"],
    "Denmark": ["Denmark"],
    "Norway": ["Norway"],
    "Germany": ["Germany"],
    "Poland": ["Poland"],
    "Spain": ["Spain"],
    "France": ["France"],
    "Italy": ["Italy"],
    "Belgium": ["Belgium"],
    "Netherlands": ["Netherlands", "Holland"],
    "USA": [
        "USA",
        "U.S.A.",
        "US",
        "U.S.",
        "United States",
        "United States of America",
        "America",
    ],
    "Hong Kong": ["Hong Kong", "HONG KONG", "HongKong"],
}

# Companies House API configuration
CH_API_BASE_URL = "https://api.company-information.service.gov.uk"

# Safety controls
DEFAULT_ITEMS_PER_PAGE = 50  # per advanced-search page
DEFAULT_MAX_COMPANIES = 100  # hard cap to avoid huge runs and rate-limit issues
REQUEST_SLEEP_SECONDS = 0.1  # small delay between requests as a courtesy

# Toggle for rotating across multiple API keys.
# IMPORTANT: Companies House explicitly warns against using multiple keys
# for the same live application to circumvent rate limiting. Use rotation
# only if you have a legitimate multi-key setup (e.g. separate test/prod apps).
ENABLE_KEY_ROTATION = False


# -----------------------------
# Helper functions
# -----------------------------


def load_api_keys() -> List[str]:
    """
    Load up to 3 Companies House API keys from Streamlit secrets or environment variables.

    Preferred (per-project secrets):
        .streamlit/secrets.toml in your working directory:
        [ch_api]
        keys = ["KEY1", "KEY2", "KEY3"]

    Also works with global secrets at ~/.streamlit/secrets.toml.[web:22]

    Fallback: environment variables:
        COMPANIES_HOUSE_API_KEY_1, COMPANIES_HOUSE_API_KEY_2, COMPANIES_HOUSE_API_KEY_3
    """
    keys: List[str] = []

    # Preferred: Streamlit secrets (project or global)
    try:
        if "ch_api" in st.secrets and "keys" in st.secrets["ch_api"]:
            raw_keys = st.secrets["ch_api"]["keys"]
            if isinstance(raw_keys, (list, tuple)):
                keys.extend(
                    [k.strip() for k in raw_keys if isinstance(k, str) and k.strip()]
                )
    except Exception:
        # st.secrets may not be available in some environments
        pass

    # Fallback: environment variables
    if not keys:
        for i in range(1, 4):
            env_key = os.getenv(f"COMPANIES_HOUSE_API_KEY_{i}")
            if env_key and env_key.strip():
                keys.append(env_key.strip())

    return keys


def get_next_api_key(api_keys: List[str]) -> str:
    """
    Select an API key for this search run.

    If rotation is disabled or there is only one key, always return the first.
    If rotation is enabled, rotate across keys per search run using session_state.
    """
    if not api_keys:
        raise RuntimeError("No Companies House API keys configured.")

    if not ENABLE_KEY_ROTATION or len(api_keys) == 1:
        return api_keys[0]

    if "api_key_index" not in st.session_state:
        st.session_state["api_key_index"] = 0

    idx = st.session_state["api_key_index"] % len(api_keys)
    st.session_state["api_key_index"] += 1
    return api_keys[idx]


def ch_get(
    path: str,
    api_key: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
) -> requests.Response:
    """
    Make a GET request to the Companies House API with Basic auth.

    The API key is sent as the username with a blank password.[web:35]
    """
    url = CH_API_BASE_URL + path
    resp = requests.get(url, auth=(api_key, ""), params=params, timeout=timeout)
    return resp


def normalise_country(text: Optional[str]) -> str:
    """Normalise a free-text country string for comparison."""
    if not text:
        return ""
    return text.strip().lower()


def officer_matches_country(officer: Dict[str, Any], selected_country: str) -> bool:
    """
    Determine if an officer's country_of_residence matches the selected country
    using a small synonym map and case-insensitive comparison.
    """
    cor_raw = officer.get("country_of_residence")
    cor_norm = normalise_country(cor_raw)
    if not cor_norm:
        return False

    synonyms = COUNTRY_SYNONYMS.get(selected_country, [selected_country])
    synonyms_norm = [s.lower() for s in synonyms]
    return cor_norm in synonyms_norm


def parse_iso_date(text: str) -> Optional[date]:
    """Parse a YYYY-MM-DD string safely to a date object."""
    if not text:
        return None
    try:
        return parse_date(text).date()
    except Exception:
        return None


# -----------------------------
# API wrappers
# -----------------------------


@st.cache_data(show_spinner=False)
def search_companies_advanced(
    incorporated_from: str,
    incorporated_to: str,
    max_companies: int,
    items_per_page: int,
    api_key: str,
) -> List[Dict[str, Any]]:
    """
    Use the Companies House advanced search endpoint to fetch companies
    incorporated within a date range.

    Uses the incorporated_from / incorporated_to filters on
    /advanced-search/companies.[web:1]
    """
    companies: List[Dict[str, Any]] = []
    start_index = 0

    while len(companies) < max_companies:
        params = {
            "incorporated_from": incorporated_from,
            "incorporated_to": incorporated_to,
            "items_per_page": items_per_page,
            "start_index": start_index,
        }

        resp = ch_get("/advanced-search/companies", api_key=api_key, params=params)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Advanced search failed with status {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        items = data.get("items", []) or []
        if not items:
            break

        for item in items:
            companies.append(item)
            if len(companies) >= max_companies:
                break

        # Prepare for next page
        if len(items) < items_per_page:
            break

        start_index += len(items)
        time.sleep(REQUEST_SLEEP_SECONDS)

    return companies


@st.cache_data(show_spinner=False)
def fetch_company_officers(
    company_number: str,
    api_key: str,
    items_per_page: int = 100,
) -> List[Dict[str, Any]]:
    """
    Fetch the officers for a given company number using the
    /company/{company_number}/officers endpoint, which exposes
    country_of_residence for each officer.[web:11]
    """
    officers: List[Dict[str, Any]] = []
    start_index = 0

    while True:
        params = {
            "items_per_page": items_per_page,
            "start_index": start_index,
        }

        path = f"/company/{company_number}/officers"
        resp = ch_get(path, api_key=api_key, params=params)
        if resp.status_code != 200:
            # For many use cases, a 404 or similar just means no officers / restricted;
            # treat that as "no officers" rather than hard failing.
            break

        data = resp.json()
        items = data.get("items", []) or []
        if not items:
            break

        officers.extend(items)

        if len(items) < items_per_page:
            break

        start_index += len(items)
        time.sleep(REQUEST_SLEEP_SECONDS)

    return officers


def find_companies_with_director_country(
    companies: List[Dict[str, Any]],
    selected_country: str,
    api_key: str,
    max_companies_to_scan: int,
) -> List[Dict[str, Any]]:
    """
    For each company in the list, fetch officers and keep companies that have
    at least one officer with country_of_residence matching the selected country.
    """
    results: List[Dict[str, Any]] = []
    scanned = 0

    for company in companies:
        if scanned >= max_companies_to_scan:
            break

        company_number = company.get("company_number")
        if not company_number:
            continue

        officers = fetch_company_officers(company_number, api_key=api_key)
        matching_officers = [
            o for o in officers if officer_matches_country(o, selected_country)
        ]

        if matching_officers:
            incorporation_date = company.get("date_of_creation")
            inc_date_parsed = parse_iso_date(incorporation_date)
            inc_date_str = (
                inc_date_parsed.isoformat() if inc_date_parsed else incorporation_date
            )

            results.append(
                {
                    "company_number": company_number,
                    "company_name": company.get("company_name"),
                    "incorporation_date": inc_date_str,
                    "director_country_of_residence": selected_country,
                    "matching_officer_count": len(matching_officers),
                    "example_officer_name": matching_officers[0]
                    .get("name", "")
                    .title()
                    if matching_officers[0].get("name")
                    else "",
                }
            )

        scanned += 1
        time.sleep(REQUEST_SLEEP_SECONDS)

    return results


# -----------------------------
# Streamlit UI
# -----------------------------


def main() -> None:
    st.set_page_config(
        page_title="Companies House: directors by country",
        layout="wide",
    )

    st.title("Companies House: director country-of-residence filter")

    st.markdown(
        """
This app queries the UK Companies House Public Data API to find companies
incorporated within a date range that have at least one director whose
**country of residence** matches a selected country.

It uses the Companies House advanced search endpoint and the company officers
endpoint, which expose `incorporated_from` / `incorporated_to` filters and
`country_of_residence` respectively.[web:1][web:11]
        """
    )

    api_keys = load_api_keys()
    if not api_keys:
        st.error(
            "No Companies House API keys configured. "
            "Set them via Streamlit secrets (.streamlit/secrets.toml) or environment "
            "variables COMPANIES_HOUSE_API_KEY_1..3."
        )
        st.stop()

    with st.sidebar:
        st.header("Search settings")

        start_date = st.date_input(
            "Incorporated from",
            value=date(2024, 1, 1),
        )
        end_date = st.date_input(
            "Incorporated to",
            value=date.today(),
        )

        if end_date < start_date:
            st.error("End date must be on or after start date.")
            st.stop()

        selected_country = st.selectbox(
            "Director country of residence",
            ALLOWED_COUNTRIES,
            index=ALLOWED_COUNTRIES.index("Germany")
            if "Germany" in ALLOWED_COUNTRIES
            else 0,
        )

        max_companies = st.number_input(
            "Maximum companies to fetch (from advanced search)",
            min_value=10,
            max_value=500,
            value=DEFAULT_MAX_COMPANIES,
            step=10,
            help="Upper bound on companies fetched from advanced search to help "
            "stay within API rate limits.",
        )

        max_companies_to_scan = st.number_input(
            "Maximum companies to scan for officers",
            min_value=10,
            max_value=int(max_companies),
            value=min(DEFAULT_MAX_COMPANIES, int(max_companies)),
            step=10,
            help="Upper bound on how many companies we'll inspect for matching officers.",
        )

        items_per_page = st.slider(
            "Advanced search items per page",
            min_value=10,
            max_value=100,
            value=DEFAULT_ITEMS_PER_PAGE,
            step=10,
            help="Number of companies per page from the advanced search endpoint.",
        )

        advanced_options = st.expander("Advanced options", expanded=False)
        with advanced_options:
            st.write(
                "Companies House applies rate limits of 600 requests per 5 minutes per "
                "REST API key. This app adds small delays between requests and caps the "
                "number of companies to help stay within those limits.[web:3][web:12]"
            )

        run_search = st.button("Run search")

    st.markdown("---")

    if not run_search:
        st.info("Configure parameters in the sidebar and click **Run search**.")
        return

    # Each search run uses a selected (or rotated) API key.
    active_api_key = get_next_api_key(api_keys)

    incorporated_from_str = start_date.isoformat()
    incorporated_to_str = end_date.isoformat()

    with st.spinner("Fetching companies from Companies House (advanced search)..."):
        try:
            companies = search_companies_advanced(
                incorporated_from=incorporated_from_str,
                incorporated_to=incorporated_to_str,
                max_companies=int(max_companies),
                items_per_page=int(items_per_page),
                api_key=active_api_key,
            )
        except Exception as exc:
            st.error(f"Error during advanced search: {exc}")
            return

    st.success(f"Fetched {len(companies)} companies from advanced search.")

    if not companies:
        st.warning("No companies found in that date range. Try widening the dates.")
        return

    with st.spinner(
        f"Scanning up to {int(max_companies_to_scan)} companies for officers in {selected_country}..."
    ):
        try:
            results = find_companies_with_director_country(
                companies=companies,
                selected_country=selected_country,
                api_key=active_api_key,
                max_companies_to_scan=int(max_companies_to_scan),
            )
        except Exception as exc:
            st.error(f"Error while fetching officers: {exc}")
            return

    st.subheader("Matching companies")

    if not results:
        st.warning(
            f"No companies in the fetched set had officers with country_of_residence "
            f"matching {selected_country}."
        )
        return

    st.write(
        f"Found **{len(results)}** companies with at least one officer whose "
        f"country of residence matches **{selected_country}**."
    )

    st.dataframe(results, use_container_width=True)

    st.caption(
        "Data sourced from the Companies House Public Data API. "
        "Remember that `country_of_residence` is a free-text field and may not always "
        "standardise country names perfectly.[web:5][web:11]"
    )


if __name__ == "__main__":
    main()
