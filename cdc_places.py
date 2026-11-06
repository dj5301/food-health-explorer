"""Live county estimates from CDC PLACES (data.cdc.gov, Socrata API). The one external data source.

No API key is needed for modest use. Every failure mode is classified into a message the
model can act on instead of a traceback. Results are cached per process: a release does not
change within a session.
"""

import time

import requests

DATASET = "swc5-untb"  # PLACES: Local Data for Better Health, County Data (current release)
URL = f"https://data.cdc.gov/resource/{DATASET}.json"
TIMEOUT_S = 20
CACHE_TTL_S = 6 * 60 * 60
MAX_FIPS_PER_CALL = 80
VALUE_TYPES = {"AgeAdjPrv": "age-adjusted prevalence", "CrdPrv": "crude prevalence"}

SOURCE_NOTE = (
    "CDC PLACES county estimates (Behavioral Risk Factor Surveillance System, small-area model-based). "
    "Model-based estimates with confidence intervals, not direct counts; the latest release lags the survey year by about two years."
)

# The measures this agent exposes. PLACES has ~40; these are the ones that speak to food access and chronic disease.
MEASURES = {
    "OBESITY": "Obesity among adults (BMI >= 30)",
    "DIABETES": "Diagnosed diabetes among adults",
    "BPHIGH": "High blood pressure among adults",
    "CHD": "Coronary heart disease among adults",
    "LPA": "No leisure-time physical activity among adults",
    "FOODSTAMP": "Received food stamps (SNAP) in the past 12 months among adults",
    "FOODINSECU": "Food insecurity in the past 12 months among adults",
    "ACCESS2": "Current lack of health insurance among adults aged 18-64",
    "CHECKUP": "Routine doctor checkup within the past year among adults",
    "GHLTH": "Fair or poor self-rated health among adults",
}


class PlacesError(Exception):
    """Raised with a user-actionable message; `kind` classifies the failure."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


_cache: dict[tuple, tuple[float, dict]] = {}


def validate(fips: list, measures: list, value_type: str) -> tuple[list[str], list[str], str]:
    if not isinstance(fips, list) or not fips:
        raise PlacesError("invalid_argument", "fips must be a non-empty list of 5-digit county FIPS codes, e.g. [\"36001\", \"36005\"].")
    clean = []
    for f in fips:
        s = str(f).strip().zfill(5)
        if not (s.isdigit() and len(s) == 5):
            raise PlacesError("invalid_argument", f"'{f}' is not a 5-digit county FIPS code.")
        clean.append(s)
    if len(clean) > MAX_FIPS_PER_CALL:
        raise PlacesError("invalid_argument", f"At most {MAX_FIPS_PER_CALL} counties per call; got {len(clean)}. Rank first, then fetch the selected counties.")
    if not isinstance(measures, list) or not measures:
        raise PlacesError("invalid_argument", f"measures must be a non-empty list from: {', '.join(MEASURES)}.")
    bad = [m for m in measures if str(m).upper() not in MEASURES]
    if bad:
        raise PlacesError("unsupported_measure", f"Unsupported PLACES measure(s): {bad}. Supported: {', '.join(MEASURES)}.")
    if value_type not in VALUE_TYPES:
        raise PlacesError("invalid_argument", f"value_type must be one of {list(VALUE_TYPES)}.")
    return list(dict.fromkeys(clean)), [str(m).upper() for m in dict.fromkeys(measures)], value_type


def fetch_places(fips: list, measures: list, value_type: str = "AgeAdjPrv") -> dict:
    """Return {"year", "value_type", "measures": {id: description}, "counties": {fips: {measure: {...}}}, "missing": [...]}."""
    fips, measures, value_type = validate(fips, measures, value_type)
    key = (tuple(fips), tuple(measures), value_type)
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_TTL_S:
        return _cache[key][1]

    in_fips = ",".join(f"'{f}'" for f in fips)
    in_measures = ",".join(f"'{m}'" for m in measures)
    params = {
        "$select": "year,stateabbr,locationname,locationid,measureid,data_value,low_confidence_limit,high_confidence_limit,totalpopulation",
        "$where": f"locationid in({in_fips}) AND measureid in({in_measures}) AND datavaluetypeid='{value_type}'",
        "$limit": 5000,
    }
    try:
        resp = requests.get(URL, params=params, timeout=TIMEOUT_S, headers={"User-Agent": "food-health-explorer (course project)"})
    except requests.Timeout:
        raise PlacesError("service_unavailable", "CDC PLACES did not answer within 20 seconds. Try again or ask for fewer counties.")
    except requests.RequestException as e:
        raise PlacesError("service_unavailable", f"Could not reach CDC PLACES: {type(e).__name__}.")
    if resp.status_code == 429:
        raise PlacesError("rate_limited", "CDC PLACES is rate-limiting anonymous requests. Wait a minute and retry.")
    if resp.status_code >= 500:
        raise PlacesError("service_unavailable", f"CDC PLACES returned HTTP {resp.status_code}.")
    if resp.status_code != 200:
        raise PlacesError("service_unavailable", f"CDC PLACES returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        records = resp.json()
    except ValueError:
        raise PlacesError("service_unavailable", "CDC PLACES returned something that is not JSON.")

    counties: dict[str, dict] = {}
    years = set()
    for r in records:
        f = r.get("locationid")
        if not f:
            continue
        years.add(r.get("year"))
        entry = counties.setdefault(f, {"county_name": r.get("locationname"), "state_abbr": r.get("stateabbr"), "population": _num(r.get("totalpopulation")), "measures": {}})
        entry["measures"][r["measureid"]] = {
            "value_pct": _num(r.get("data_value")),
            "ci_low": _num(r.get("low_confidence_limit")),
            "ci_high": _num(r.get("high_confidence_limit")),
        }
    missing = [f for f in fips if f not in counties]
    result = {
        "source": SOURCE_NOTE,
        "dataset": f"https://data.cdc.gov/d/{DATASET}",
        "data_year": sorted(y for y in years if y)[-1] if years else None,
        "value_type": VALUE_TYPES[value_type],
        "measures": {m: MEASURES[m] for m in measures},
        "counties": counties,
        "missing_fips": missing,
        "n_requested": len(fips),
        "n_returned": len(counties),
    }
    _cache[key] = (now, result)
    return result


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
