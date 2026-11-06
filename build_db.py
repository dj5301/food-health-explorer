"""Build the database and the context the agent retrieves. Run once, offline, before app.py.

    uv run --group build build_db.py

1. Load the USDA Food Environment Atlas (2025 release, 9 category sheets) and the County
   Health Rankings 2023 "Additional Measures" sheet into data/food_health.duckdb, cleaned:
   -9999 / -8888 / "N/A" become NULL, FIPS becomes a zero-padded 5-character key, state
   total rows leave the county tables, and every column gets a SQL-safe snake_case name.
2. Describe every column from the official data dictionaries shipped inside the workbooks
   (the Atlas "Variable List" sheet and the Rankings "Sources & Years" sheet): human name,
   units, data year, source, whether it is a state-level measure repeated for every county,
   and, for columns with few distinct values, every valid value.
3. Write a domain glossary: what a public-health analyst means by "limited access",
   "SNAP participation", "obesity" and so on, mapped to the columns that measure it and to
   the columns that look right but are not.
4. Write it all to data/mappings.json, the context app.py serves through read_mappings.

No model call: the dictionaries are authoritative, so a generated paraphrase would only
lose information.
"""

import json
import re
from pathlib import Path

import duckdb
from openpyxl import load_workbook

DATA = Path(__file__).parent / "data"
ATLAS_XLSX = DATA / "food_environment_atlas_2025.xlsx"
CHR_XLSX = DATA / "county_health_rankings_2023_additional_measures.xlsx"
DB_PATH = DATA / "food_health.duckdb"
MAPPINGS_PATH = DATA / "mappings.json"

MAX_ENUM_VALUES = 12  # above this a column is a measure, not a category
SENTINELS = {-9999, -8888}  # Atlas: not available / suppressed, county did not exist that year

# --- Food Environment Atlas: sheet -> table ---

ATLAS_TABLES = {
    "ACCESS": ("food_access", "Access and proximity to grocery stores: counts and shares of people living far from a store, overall and for low-income, SNAP, child, senior and race/ethnicity groups (2015, 2019)."),
    "STORES": ("food_stores", "Food store counts and stores per 1,000 residents: grocery, supercenter, convenience, specialty, SNAP-authorized and WIC-authorized stores (2016-2023)."),
    "RESTAURANTS": ("restaurants", "Fast-food and full-service restaurant counts and rates per 1,000 residents (2016, 2020)."),
    "ASSISTANCE": ("food_assistance", "Food assistance programs: SNAP, WIC, school meals, summer food, CACFP, FDPIR, food banks. Several SNAP/WIC participation shares are STATE-level policy or participation measures repeated for every county."),
    "INSECURITY": ("food_insecurity", "Household food insecurity and very low food security, three-year averages. Every column is STATE-level (same value for all counties in a state)."),
    "TAXES": ("food_taxes", "State sales-tax rates on soda, chips and general food, 2014. STATE-level."),
    "LOCAL": ("local_foods", "Local food systems: direct-to-consumer farm sales, farmers' markets, vegetable/orchard/berry acreage, CSAs, food hubs, farm-to-school (2012-2023)."),
    "HEALTH": ("food_health", "Health and physical activity: adult diabetes rate (county, 2015/2019), adult obesity rate (STATE-level, 2017/2022), high-school physical activity, recreation facilities."),
    "SOCIOECONOMIC": ("socioeconomic", "County demographics and economics: race/ethnicity shares, age shares, median household income, poverty rates, persistent-poverty, metro/nonmetro and population-loss flags."),
}

# Establishment counts from County Business Patterns are disclosure-suppressed: 2020 columns hide
# counts under 3, 2016 columns hide zeros. After cleaning those are NULL, and NULL must not be read as 0.
CBP_PREFIXES = ("GROC", "SUPERC", "CONVS", "SPECS", "FFR", "FSR", "RECFAC")
SUPPRESSION_NOTE = " NULL = suppressed (fewer than 3 establishments in 2020 columns, zero in 2016 columns), NOT zero: never COALESCE to 0; report NULL rows as 'suppressed / fewer than 3'."

# Legend / classification codes the Atlas documents on its website but not in the workbook.
ATLAS_CODE_LABELS = {
    "METRO23": {0: "Nonmetro county", 1: "Metro county"},
    "PERPOV17_21": {0: "Not persistent poverty", 1: "Persistent-poverty county (20%+ poverty in each of the last four censuses/ACS periods)"},
    "POPLOSS15": {0: "No population loss", 1: "Population-loss county"},
    "SNAP_OAPP16": {0: "No", 1: "Yes"}, "SNAP_OAPP20": {0: "No", 1: "Yes"},
    "SNAP_CAP16": {0: "No", 1: "Yes"}, "SNAP_CAP20": {0: "No", 1: "Yes"},
    "SNAP_BBCE16": {0: "No", 1: "Yes"}, "SNAP_BBCE20": {0: "No", 1: "Yes"},
    "SNAP_REPORTSIMPLE16": {0: "No", 1: "Yes"}, "SNAP_REPORTSIMPLE20": {0: "No", 1: "Yes"},
    "FARM_TO_SCHOOL19": {0: "No farm-to-school activity reported", 1: "Farm-to-school activity reported"},
}

# --- County Health Rankings 2023, Additional Measures ---

# The sheet has 141 measure columns after FIPS/State/County. They come in the same order as the
# "Sources & Years" sheet, each measure spanning a known number of columns (counts, rates, race
# breakdowns). Listing the spans is how each column gets its official description.
CHR_MEASURE_SPANS = [
    ("Life Expectancy", 6), ("Premature Age-Adjusted Mortality", 7), ("Child Mortality", 7),
    ("Infant Mortality", 7), ("Frequent Physical Distress", 1), ("Frequent Mental Distress", 1),
    ("Diabetes Prevalence", 1), ("HIV Prevalence", 2), ("Food Insecurity", 2),
    ("Limited Access to Healthy Foods", 2), ("Drug Overdose Deaths", 7), ("Insufficient Sleep", 1),
    ("Uninsured Adults", 2), ("Uninsured Children", 2), ("Other Primary Care Providers", 2),
    ("High School Graduation", 2), ("Disconnected Youth", 1), ("Reading Scores", 5), ("Math Scores", 5),
    ("School Segregation", 1), ("School Funding Adequacy", 2), ("Gender Pay Gap", 3),
    ("Median Household Income", 6), ("Children Eligible for Free or Reduced Price Lunch", 1),
    ("Residential Segregation - Black/White", 1), ("Child Care Cost Burden", 1), ("Child Care Centers", 2),
    ("Homicides", 6), ("Suicides", 8), ("Firearm Fatalities", 7), ("Motor Vehicle Crash Deaths", 7),
    ("Juvenile Arrests", 4), ("Voter Turnout", 1), ("Census Participation", 1), ("Traffic Volume", 1),
    ("Homeownership", 2), ("Severe Housing Cost Burden", 2), ("Broadband Access", 2), ("Population", 1),
    ("% Below 18 Years of Age", 1), ("% 65 and Older", 1), ("% Non-Hispanic Black", 2),
    ("% American Indian or Alaska Native", 2), ("% Asian", 2), ("% Native Hawaiian or Other Pacific Islander", 2),
    ("% Hispanic", 2), ("% Non-Hispanic White", 2), ("% Not Proficient in English", 2), ("% Female", 1), ("% Rural", 2),
]

# Workbook headers that repeat ("# Deaths" appears four times) or say nothing on their own.
CHR_RENAMES = {
    ("Premature Age-Adjusted Mortality", "# Deaths"): "num_premature_deaths",
    ("Child Mortality", "# Deaths"): "num_child_deaths",
    ("Infant Mortality", "# Deaths"): "num_infant_deaths",
    ("Suicides", "# Deaths"): "num_suicides",
    ("Suicides", "Crude Rate"): "suicide_rate_crude",
    ("High School Graduation", "Cohort Size"): "hs_graduation_cohort_size",
    ("Reading Scores", "Average Grade Performance"): "reading_score_grade_level",
    ("Math Scores", "Average Grade Performance"): "math_score_grade_level",
    ("School Segregation", "Segregation Index"): "school_segregation_index",
    ("Residential Segregation - Black/White", "Segregation Index"): "residential_segregation_black_white",
    ("Juvenile Arrests", "Denominator"): "juvenile_population",
    ("Child Care Cost Burden", "% Household Income Required for Child Care Expenses"): "pct_income_for_child_care",
}

# --- Glossary: what analysts say vs. what the columns measure ---

GLOSSARY = [
    {
        "term": "limited access to healthy food / low food access / food desert share",
        "definition": "Share of a county's population that is BOTH low-income AND lives far from a supermarket (more than 1 mile urban / 10 miles rural). This is the County Health Rankings 'Limited Access to Healthy Foods' definition.",
        "use": ["food_access.pct_laccess_lowi19", "health_rankings.pct_limited_access_to_healthy_foods"],
        "not": {"food_access.pct_laccess_pop19": "all residents far from a store regardless of income; much larger and mostly measures rurality"},
    },
    {
        "term": "SNAP participation rate",
        "definition": "Share of residents (or of eligible residents) receiving SNAP. The Atlas only publishes this at STATE level, so it cannot rank counties within a state.",
        "use": ["food_assistance.pc_snapben22 (SNAP benefits per capita, dollars, county-level proxy)", "food_access.pct_laccess_snap19 (SNAP households far from a store, county-level)", "health_rankings.pct_enrolled_in_free_or_reduced_lunch (child low-income proxy, county-level)"],
        "not": {"food_assistance.pct_snap22": "STATE-level: identical for every county in a state", "food_assistance.snap_part_rate19": "STATE-level"},
    },
    {
        "term": "adult obesity rate",
        "definition": "Share of adults with BMI >= 30. The Atlas value is STATE-level (same for every county). No county-level obesity exists in this database; the live CDC PLACES tool (fetch_cdc_places, measure OBESITY) is the county-level source.",
        "use": ["fetch_cdc_places measure OBESITY (county, model-based estimate)"],
        "not": {"food_health.pct_obese_adults22": "STATE-level; cannot distinguish counties", "food_health.pct_obese_adults17": "STATE-level"},
    },
    {
        "term": "adult diabetes rate",
        "definition": "Share of adults with diagnosed diabetes. Two county-level versions exist: Atlas 2019 (crude, CDC Diabetes Surveillance) and Rankings 2020 (age-adjusted, BRFSS). Say which one you used; do not average them.",
        "use": ["food_health.pct_diabetes_adults19", "health_rankings.pct_adults_with_diabetes"],
        "not": {"food_health.pct_diabetes_adults15": "older year; use only when the user asks about 2015 or change over time"},
    },
    {
        "term": "food insecurity",
        "definition": "Share of people lacking adequate access to food. The Atlas measure is STATE-level (3-year CPS average); the Rankings measure (Map the Meal Gap 2020) is county-level.",
        "use": ["health_rankings.pct_food_insecure"],
        "not": {"food_insecurity.foodinsec_21_23": "STATE-level", "food_insecurity.foodinsec_18_20": "STATE-level"},
    },
    {
        "term": "poverty rate / low income",
        "definition": "Share of residents below the federal poverty line, 2021 SAIPE.",
        "use": ["socioeconomic.povrate21", "socioeconomic.childpovrate21", "socioeconomic.perpov17_21 (persistent-poverty flag)"],
        "not": {},
    },
    {
        "term": "rural / nonmetro",
        "definition": "Metro status is a county classification (OMB 2023); % rural is a population share (Census 2010). Prefer metro23 for grouping counties, pct_rural for a continuous measure.",
        "use": ["socioeconomic.metro23", "health_rankings.pct_rural"],
        "not": {},
    },
    {
        "term": "uninsured",
        "definition": "Share of adults under 65 (or children under 19) without health insurance, SAHIE 2020.",
        "use": ["health_rankings.pct_uninsured_adults", "health_rankings.pct_uninsured_children"],
        "not": {},
    },
    {
        "term": "grocery access / store density",
        "definition": "Grocery stores per 1,000 residents (2020). Low density is a different concept from 'limited access', which is distance-based and income-conditioned.",
        "use": ["food_stores.grocpth20", "food_stores.superc20 (supercenters)", "food_stores.convspth20 (convenience stores)"],
        "not": {},
    },
    {
        "term": "counties with no supermarket / zero stores / store deserts",
        "definition": "Store and restaurant counts come from County Business Patterns, which suppresses small counts: 2020 columns are NULL when a county has fewer than 3 establishments, 2016 columns when it has none. The database therefore cannot say that a county has ZERO grocery stores or supercenters; it can say the count is suppressed (fewer than 3). Count NULLs as 'suppressed', never as 0.",
        "use": ["food_stores.groc20 IS NULL  (fewer than 3 grocery stores)", "food_stores.superc20 IS NULL  (fewer than 3 supercenters)", "food_stores.grocpth20 (stores per 1,000 residents, also NULL when suppressed)"],
        "not": {"COALESCE(food_stores.superc20, 0) = 0": "turns 'suppressed' into 'zero' and overstates store deserts"},
    },
    {
        "term": "latest year",
        "definition": "Atlas columns end in a 2-digit year (e.g. _19 = 2019, _22 = 2022). When a variable has two years, the larger suffix is the latest. PCH_/CH_ columns are the change between the two years, not a level.",
        "use": [],
        "not": {},
    },
]


# --- Helpers ---


def snake(name: str) -> str:
    s = name.strip().replace("%", "pct ").replace("#", "num ")
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    return s


def clean(value):
    """Workbook cell -> SQL value. Sentinels and N/A become NULL."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if v in {"", "N/A", "NA", "NaN"}:
            return None
        try:
            value = float(v)
        except ValueError:
            return v
    if isinstance(value, (int, float)) and value in SENTINELS:
        return None
    return value


def fips_key(value) -> str | None:
    if value is None or value == "":
        return None
    try:
        return f"{int(float(value)):05d}"
    except (TypeError, ValueError):
        return None


def rows_of(ws, header_row: int):
    """Yield (header, rows) from a worksheet, header taken from `header_row` (1-based)."""
    it = ws.iter_rows(min_row=header_row, values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it)]
    while header and header[-1] == "":
        header.pop()  # read-only mode reports trailing empty columns
    rows = [list(r[: len(header)]) for r in it if any(c is not None for c in r)]
    return header, rows


def column_type(values) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return "DOUBLE"
    if all(isinstance(v, bool) for v in present):
        return "BOOLEAN"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "BIGINT" if all(float(v).is_integer() for v in present) else "DOUBLE"
    return "VARCHAR"


def create_table(db, name: str, columns: list[tuple[str, str]], rows: list[list]):
    cols = ", ".join(f'"{c}" {t}' for c, t in columns)
    db.execute(f'CREATE TABLE "{name}" ({cols})')
    if rows:
        placeholders = ", ".join("?" for _ in columns)
        db.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)


def describe_enum(db, table: str, column: str, dtype: str, labels: dict | None):
    if dtype not in {"BIGINT", "VARCHAR", "BOOLEAN"}:
        return None
    n = db.execute(f'SELECT count(DISTINCT "{column}") FROM "{table}"').fetchone()[0]
    if n == 0 or n > MAX_ENUM_VALUES:
        return None
    values = [r[0] for r in db.execute(f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL ORDER BY 1').fetchall()]
    if labels:
        return [{"value": v, "label": labels.get(int(v) if isinstance(v, (int, float)) else v, str(v))} for v in values]
    return [{"value": v, "label": str(v)} for v in values]


# --- 1. Food Environment Atlas ---


def build_atlas(db) -> tuple[list[dict], dict]:
    wb = load_workbook(ATLAS_XLSX, read_only=True, data_only=True)

    # Variable List: code -> (name, units, category, subcategory)
    header, rows = rows_of(wb[" Variable List"], 2)
    dictionary = {}
    for r in rows:
        rec = dict(zip(header, r))
        code = str(rec.get("Variable Code") or "").strip()
        if code:
            dictionary[code] = rec

    tables = []
    counties: dict[str, dict] = {}  # fips -> {state_abbr, county_name}
    for sheet, (table, table_desc) in ATLAS_TABLES.items():
        header, rows = rows_of(wb[sheet], 2)
        assert header[:3] == ["FIPS", "State", "County"], (sheet, header[:3])
        codes = header[3:]
        records = []
        for r in rows:
            fips = fips_key(r[0])
            if fips is None:
                continue  # Alaska census areas without a FIPS in the LOCAL sheet
            state_abbr, county = str(r[1]).strip(), str(r[2]).strip()
            counties.setdefault(fips, {"state_abbr": state_abbr, "county_name": county})
            records.append([fips] + [clean(v) for v in r[3:]])

        columns = [("fips", "VARCHAR")]
        for i, code in enumerate(codes, start=1):
            columns.append((code.lower(), column_type([rec[i] for rec in records])))
        create_table(db, table, columns, records)

        col_meta = [{"name": "fips", "type": "VARCHAR", "description": "5-digit county FIPS code, zero-padded. Join key for every table.", "is_state_level": False}]
        for code, (name, dtype) in zip(codes, columns[1:]):
            d = dictionary.get(code, {})
            label = str(d.get("Variable Name") or code)
            state_level = label.rstrip().endswith("*")
            label = label.rstrip("* ").strip()
            year = re.search(r"(20\d\d(?:\s*-\s*\d{2,4})?)\s*$", label)
            suppressed = code.startswith(CBP_PREFIXES) and not code.startswith("PCH_")
            meta = {
                "name": name,
                "type": dtype,
                "description": label + (" [STATE-level: same value for every county in the state]" if state_level else "") + (SUPPRESSION_NOTE if suppressed else ""),
                "null_means": "suppressed, fewer than 3 (2020) or zero (2016)" if suppressed else "not available",
                "units": str(d.get("Units") or ""),
                "year": year.group(1).replace(" ", "") if year else None,
                "subcategory": str(d.get("Subcategory Name") or ""),
                "is_state_level": state_level,
                "source": "USDA ERS Food Environment Atlas, 2025 release",
            }
            enum = describe_enum(db, table, name, dtype, ATLAS_CODE_LABELS.get(code))
            if enum is not None:
                meta["enum_values"] = enum
            col_meta.append(meta)
        n = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        tables.append({"name": table, "description": table_desc, "row_count": n, "source": "USDA ERS Food Environment Atlas, 2025 release", "columns": col_meta})
        print(f"  {table}: {n} rows, {len(columns) - 1} measures")
    wb.close()
    return tables, counties


# --- 2. County Health Rankings ---


def build_chr(db, counties: dict) -> list[dict]:
    wb = load_workbook(CHR_XLSX, read_only=True, data_only=True)

    # Sources & Years: measure -> (description, source, years)
    header, rows = rows_of(wb["Addtl Measure Sources & Years"], 1)
    sources = {}
    for r in rows:
        rec = dict(zip(header, r))
        measure = str(rec.get("Measure") or "").strip().rstrip("*+").strip()
        if measure and rec.get("Description"):
            sources[measure] = rec

    header, rows = rows_of(wb["Additional Measure Data"], 1)
    assert header[:3] == ["FIPS", "State", "County"], header[:3]
    measure_cols = header[3:]
    assert sum(n for _, n in CHR_MEASURE_SPANS) == len(measure_cols), (sum(n for _, n in CHR_MEASURE_SPANS), len(measure_cols))

    # Assign each column a measure and a unique snake_case name.
    assigned, names, seen = [], [], set()
    i = 0
    for measure, span in CHR_MEASURE_SPANS:
        for raw in measure_cols[i : i + span]:
            base = re.sub(r"\.\d+$", "", raw)  # openpyxl does not add .1 suffixes, but be safe
            name = CHR_RENAMES.get((measure, base)) or snake(base)
            if name in seen:
                name = f"{name}_{snake(measure)}"
            seen.add(name)
            assigned.append((measure, base))
            names.append(name)
        i += span

    county_rows, state_rows = [], []
    state_names = {}
    for r in rows:
        fips = fips_key(r[0])
        if fips is None:
            continue
        state_name, county = str(r[1]).strip(), (str(r[2]).strip() if r[2] is not None else None)
        values = [clean(v) for v in r[3:]]
        if county is None or fips.endswith("000"):
            state_rows.append([fips, state_name] + values)
            state_names[fips[:2]] = state_name
        else:
            county_rows.append([fips] + values)

    types = [column_type([rec[k + 1] for rec in county_rows]) for k in range(len(names))]
    create_table(db, "health_rankings", [("fips", "VARCHAR")] + list(zip(names, types)), county_rows)
    create_table(db, "health_rankings_state", [("fips", "VARCHAR"), ("state_name", "VARCHAR")] + list(zip(names, types)), state_rows)

    col_meta = [{"name": "fips", "type": "VARCHAR", "description": "5-digit county FIPS code, zero-padded. Join key for every table.", "is_state_level": False}]
    for (measure, raw), name, dtype in zip(assigned, names, types):
        src = sources.get(measure, {})
        race = re.search(r"\((AIAN|Asian|Black|Hispanic|White)\)", raw)
        desc = f"{raw}. Measure '{measure}': {src.get('Description', '')}".strip()
        if race:
            desc += f" This column is the {race.group(1)} subgroup."
        col_meta.append({
            "name": name,
            "type": dtype,
            "description": desc,
            "units": "percent" if raw.startswith("%") else ("count" if raw.startswith("#") else ""),
            "year": str(src.get("Year(s)") or ""),
            "subcategory": measure,
            "is_state_level": False,
            "source": f"County Health Rankings 2023 — {src.get('Source', '')}".strip(" —"),
        })
    n = db.execute("SELECT count(*) FROM health_rankings").fetchone()[0]
    print(f"  health_rankings: {n} county rows, {len(names)} measures; health_rankings_state: {len(state_rows)} rows")
    tables = [
        {"name": "health_rankings", "description": "County Health Rankings 2023 'Additional Measures': county-level health outcomes (life expectancy, mortality, diabetes, HIV), food insecurity and limited access (Map the Meal Gap / USDA), insurance, education, income, free/reduced lunch, safety, housing, broadband and demographics. Counties only.", "row_count": n, "source": "County Health Rankings & Roadmaps 2023, Additional Measure Data", "columns": col_meta},
        {"name": "health_rankings_state", "description": "Same columns as health_rankings but one row per STATE plus the United States (fips 00000). Use for state benchmarks; never mix with county rows.", "row_count": len(state_rows), "source": "County Health Rankings & Roadmaps 2023, Additional Measure Data", "columns": [{"name": "fips", "type": "VARCHAR", "description": "State FIPS padded to 5 digits, e.g. 36000 = New York; 00000 = United States.", "is_state_level": True}, {"name": "state_name", "type": "VARCHAR", "description": "Full state name.", "is_state_level": True}] + col_meta[1:]},
    ]
    wb.close()
    return tables, state_names


# --- 3. The counties dimension ---


def build_counties(db, counties: dict, state_names: dict) -> dict:
    rows = []
    for fips, c in sorted(counties.items()):
        rows.append([fips, c["state_abbr"], state_names.get(fips[:2]), c["county_name"]])
    create_table(db, "counties", [("fips", "VARCHAR"), ("state_abbr", "VARCHAR"), ("state_name", "VARCHAR"), ("county_name", "VARCHAR")], rows)
    db.execute("""
        ALTER TABLE counties ADD COLUMN metro23 BIGINT;
        UPDATE counties SET metro23 = s.metro23 FROM socioeconomic s WHERE s.fips = counties.fips;
        ALTER TABLE counties ADD COLUMN population_2021 BIGINT;
        UPDATE counties SET population_2021 = h.population FROM health_rankings h WHERE h.fips = counties.fips;
    """)
    n = db.execute("SELECT count(*) FROM counties").fetchone()[0]
    states = db.execute("SELECT state_abbr, state_name FROM counties WHERE state_name IS NOT NULL GROUP BY 1, 2 ORDER BY 1").fetchall()
    print(f"  counties: {n} rows, {len(states)} states")
    return {
        "name": "counties",
        "description": "One row per county: FIPS, state abbreviation and name, county name, metro flag and 2021 population. Start here to resolve a state or county name to its fips, then join the measure tables on fips.",
        "row_count": n,
        "source": "Derived from the two workbooks",
        "columns": [
            {"name": "fips", "type": "VARCHAR", "description": "5-digit county FIPS code, zero-padded. Join key for every table.", "is_state_level": False},
            {"name": "state_abbr", "type": "VARCHAR", "description": "Two-letter state abbreviation, e.g. 'NY'.", "is_state_level": False, "enum_values": [{"value": a, "label": n or a} for a, n in states]},
            {"name": "state_name", "type": "VARCHAR", "description": "Full state name, e.g. 'New York'. NULL for Connecticut planning regions that exist only in the Atlas.", "is_state_level": False, "enum_values": [{"value": n, "label": a} for a, n in states]},
            {"name": "county_name", "type": "VARCHAR", "description": "County name WITHOUT the word 'County' (e.g. 'Albany', 'Bronx', 'St. Lawrence'). Match with ILIKE or compare lower-case.", "is_state_level": False},
            {"name": "metro23", "type": "BIGINT", "description": "Metro/nonmetro county, 2023 OMB definition.", "is_state_level": False, "enum_values": [{"value": 0, "label": "Nonmetro county"}, {"value": 1, "label": "Metro county"}]},
            {"name": "population_2021", "type": "BIGINT", "description": "Resident population, 2021 Census estimate (from County Health Rankings).", "is_state_level": False},
        ],
    }


# --- Main ---

if __name__ == "__main__":
    DB_PATH.unlink(missing_ok=True)
    db = duckdb.connect(str(DB_PATH))
    print("Food Environment Atlas")
    atlas_tables, counties = build_atlas(db)
    print("County Health Rankings")
    chr_tables, state_names = build_chr(db, counties)
    counties_table = build_counties(db, counties, state_names)
    db.close()

    mappings = {
        "database": DB_PATH.name,
        "join_key": "fips",
        "notes": [
            "Every table has one row per county keyed by fips (VARCHAR, 5 digits). Join tables with USING (fips).",
            "Missing, suppressed and not-applicable values are NULL. There are no -9999 / -8888 sentinels left.",
            "Store, restaurant and recreation-facility COUNTS (groc*, superc*, convs*, specs*, ffr*, fsr*, recfac*) are NULL when suppressed (fewer than 3 in 2020, zero in 2016). NULL is not zero; the data cannot identify counties with exactly zero stores.",
            "Columns flagged is_state_level are the same for every county in a state: they cannot rank counties within a state.",
            "Atlas column names end in a 2-digit data year; PCH_/CH_ columns are changes between two years.",
            "State totals live in health_rankings_state, never in the county tables.",
        ],
        "tables": [counties_table] + atlas_tables + chr_tables,
        "glossary": GLOSSARY,
    }
    MAPPINGS_PATH.write_text(json.dumps(mappings, indent=1, default=str))
    total_cols = sum(len(t["columns"]) for t in mappings["tables"])
    print(f"Built {DB_PATH.name} and {MAPPINGS_PATH.name}: {len(mappings['tables'])} tables, {total_cols} columns, {len(GLOSSARY)} glossary terms")
