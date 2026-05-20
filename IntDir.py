import re
from typing import Any, Dict, List, Optional, Set

TARGET_COUNTRIES = {
    "nigeria",
    "pakistan",
    "turkey",
    "india",
    "china",
    "russia",
    "united arab emirates",
}

PRIORITY_EXCEPTION_COUNTRIES = {"nigeria", "pakistan", "turkey"}
ALWAYS_PUBLISH_SICS = {"62012", "72110"}

COUNTRY_ALIASES = {
    "turkiye": "turkey",
    "türkiye": "turkey",
    "uae": "united arab emirates",
    "u.a.e.": "united arab emirates",
    "england": "united kingdom",
    "scotland": "united kingdom",
    "wales": "united kingdom",
    "northern ireland": "united kingdom",
}


def normalize_country(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value).strip().lower())
    return COUNTRY_ALIASES.get(cleaned, cleaned)


NORMALIZED_TARGET_COUNTRIES: Set[str] = {normalize_country(c) for c in TARGET_COUNTRIES}
NORMALIZED_PRIORITY_COUNTRIES: Set[str] = {normalize_country(c) for c in PRIORITY_EXCEPTION_COUNTRIES}


def is_target_country(value: Optional[str]) -> bool:
    return normalize_country(value) in NORMALIZED_TARGET_COUNTRIES



def is_priority_country(value: Optional[str]) -> bool:
    return normalize_country(value) in NORMALIZED_PRIORITY_COUNTRIES



def psc_record_is_legal_entity(psc: Dict[str, Any]) -> bool:
    kind = str(psc.get("kind", "")).lower()
    name = str(psc.get("name", "")).strip()
    natures = [str(x).lower() for x in psc.get("natures_of_control", [])]

    legal_kind_markers = [
        "corporate-entity",
        "legal-person",
        "firm",
        "super-secure",
    ]
    if any(marker in kind for marker in legal_kind_markers):
        return True

    corporate_name_markers = [" ltd", " limited", " llp", " plc", " inc", " gmbh", " sarl", " bv"]
    if any(marker in f" {name.lower()}" for marker in corporate_name_markers):
        return True

    return any("ownership-of-shares" in nature or "voting-rights" in nature for nature in natures) and not psc.get("nationality")



def summarise_psc_flags(psc_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    legal_entity_flag = False
    target_nationality_flag = False
    matched_psc_nationalities: List[str] = []
    priority_match: Optional[str] = None

    for psc in psc_items:
        if psc_record_is_legal_entity(psc):
            legal_entity_flag = True

        nationality = psc.get("nationality")
        if is_target_country(nationality):
            target_nationality_flag = True
            matched_psc_nationalities.append(str(nationality))
            if is_priority_country(nationality) and priority_match is None:
                priority_match = str(nationality)

    return {
        "psc_legal_entity_flag": legal_entity_flag,
        "psc_target_nationality_flag": target_nationality_flag,
        "matched_psc_nationalities": matched_psc_nationalities,
        "psc_priority_country": priority_match,
    }



def summarise_director_flags(officers: List[Dict[str, Any]]) -> Dict[str, Any]:
    target_directors: List[str] = []
    priority_match: Optional[str] = None

    for officer in officers:
        role = str(officer.get("officer_role", "")).lower()
        if role != "director":
            continue

        residence = officer.get("country_of_residence") or officer.get("residential_country")
        if is_target_country(residence):
            target_directors.append(f"{officer.get('name', 'Unknown')} ({residence})")
            if is_priority_country(residence) and priority_match is None:
                priority_match = str(residence)

    return {
        "director_target_residency_flag": bool(target_directors),
        "matched_director_residencies": target_directors,
        "director_priority_country": priority_match,
    }



def should_publish_company(company: Dict[str, Any], psc_flags: Dict[str, Any], director_flags: Dict[str, Any]) -> Dict[str, Any]:
    sic_codes = {str(code) for code in company.get("sic_codes", []) if code}
    in_always_publish_sic = bool(sic_codes & ALWAYS_PUBLISH_SICS)

    priority_country = (
        psc_flags.get("psc_priority_country")
        or director_flags.get("director_priority_country")
    )

    flagged = (
        psc_flags["psc_legal_entity_flag"]
        or psc_flags["psc_target_nationality_flag"]
        or director_flags["director_target_residency_flag"]
    )

    if in_always_publish_sic and not priority_country:
        return {
            "should_publish": True,
            "publish_reason": "SIC 62012/72110 retained and no Nigeria/Pakistan/Turkey exception matched",
            "priority_exception_country_flag": False,
            "matched_priority_country": None,
        }

    if priority_country:
        return {
            "should_publish": flagged,
            "publish_reason": f"Priority exception country matched: {priority_country}",
            "priority_exception_country_flag": True,
            "matched_priority_country": priority_country,
        }

    return {
        "should_publish": flagged,
        "publish_reason": "Published because ownership/residency screening flagged the company" if flagged else "Not published because no screening rule matched",
        "priority_exception_country_flag": False,
        "matched_priority_country": None,
    }



def enrich_company_once(company_number: str, company_cache: Dict[str, Dict[str, Any]], refresh_token: int, fetch_company_profile, fetch_company_officers, fetch_company_psc) -> Dict[str, Any]:
    cache_key = f"{company_number}:{refresh_token}"
    if cache_key in company_cache:
        return company_cache[cache_key]

    profile = fetch_company_profile(company_number)
    officers = fetch_company_officers(company_number)
    psc_items = fetch_company_psc(company_number)

    psc_flags = summarise_psc_flags(psc_items)
    director_flags = summarise_director_flags(officers)
    publish_flags = should_publish_company(profile, psc_flags, director_flags)

    result = {
        **profile,
        **psc_flags,
        **director_flags,
        **publish_flags,
        "company_number": company_number,
    }
    company_cache[cache_key] = result
    return result



def process_companies(companies: List[Dict[str, Any]], company_cache: Dict[str, Dict[str, Any]], refresh_token: int, fetch_company_profile, fetch_company_officers, fetch_company_psc) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    output: List[Dict[str, Any]] = []

    for company in companies:
        company_number = str(company.get("company_number", "")).strip()
        if not company_number or company_number in seen:
            continue
        seen.add(company_number)

        enriched = enrich_company_once(
            company_number=company_number,
            company_cache=company_cache,
            refresh_token=refresh_token,
            fetch_company_profile=fetch_company_profile,
            fetch_company_officers=fetch_company_officers,
            fetch_company_psc=fetch_company_psc,
        )
        output.append(enriched)

    return output


# Streamlit usage pattern:
# if "company_cache" not in st.session_state:
#     st.session_state.company_cache = {}
# if "refresh_token" not in st.session_state:
#     st.session_state.refresh_token = 0
#
# if st.button("Manual refresh"):
#     st.session_state.refresh_token += 1
#
# results = process_companies(
#     companies=search_results,
#     company_cache=st.session_state.company_cache,
#     refresh_token=st.session_state.refresh_token,
#     fetch_company_profile=fetch_company_profile,
#     fetch_company_officers=fetch_company_officers,
#     fetch_company_psc=fetch_company_psc,
# )
