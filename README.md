# County Food & Health Explorer

**A Text2SQL agent for health-department staff: where in a state do limited food access, poverty and food-assistance need coincide with diabetes, obesity and food insecurity, county by county, and does the latest CDC data still show the same places?**

| | |
|---|---|
| Live | **https://food-health-explorer-948672489626.europe-west1.run.app** |
| Source | https://github.com/dj5301/food-health-explorer |
| Path | `text2sql` |
| Author | dj2763 |

## Try it in two minutes

1. Open the live link. If the service has been idle, the first page load starts a new instance and the page shows a wake-up notice for about 20 seconds.
2. Click the suggestion chips on the left, top to bottom, in one session. Each answer usually takes 5 to 30 seconds.
3. Open any tool card in the chat to see the call, its arguments and the raw result. The right panel shows the selected counties, the latest comparison and the CDC cross-check.

What you should see:

- **First chip (New York ranking):** 11 qualifying counties, led by Montgomery, Franklin, St. Lawrence, Orange and Chemung, with thresholds of 3.87% (low access) and 8.5% (diabetes).
- **Second chip (comparison):** the five counties against the other 57, with every 95% interval including zero, so the answer says the gaps could be chance.
- **Third chip (CDC check):** all five above the New York diabetes median of 9.0%; three of them above the obesity median of 32.9%.
- **Fourth chip (supercenters):** "31 counties have fewer than three supercenters", with an explanation of why "zero" cannot be counted.

The [three sample queries](#three-sample-queries) below list the exact expected numbers.

## Contents

- [Why this topic](#why-this-topic)
- [Who it is for and what it answers](#who-it-is-for-and-what-it-answers)
- [Data: where it comes from and how it is loaded](#data-where-it-comes-from-and-how-it-is-loaded)
- [Tools](#tools)
- [Three sample queries](#three-sample-queries)
- [Two questions a plain `run_sql` agent got wrong](#two-questions-a-plain-run_sql-agent-got-wrong)
- [What I observed while testing](#what-i-observed-while-testing)
- [Interface and agent architecture](#interface-and-agent-architecture)
- [Scope and limitations](#scope-and-limitations)
- [Next stage plan](#next-stage-plan)
- [Setup](#setup)
- [Deployment](#deployment)

## Why this topic

I chose this topic because of my own background and interest in food safety, a longstanding public concern. A previous experience with spoiled coconut water I bought online made that interest more personal and motivated me to explore food-related health questions through data. This project focuses on food access and food insecurity rather than product safety: the USDA Food Environment Atlas and County Health Rankings datasets let me examine how county food environments overlap with poverty and chronic disease. I imagined health-department analysts using it to explore these patterns without needing to know SQL or the datasets' column names.

## Who it is for and what it answers

State and county health-department analysts who decide where nutrition and chronic-disease programs (SNAP-Ed, produce prescriptions, farmers'-market incentives, diabetes prevention) should go first. They know their counties but not the column names of federal datasets, and they need every number to be traceable to a published table.

The agent answers questions like:

- "Which New York counties combine limited access to healthy food with adult diabetes above the state median?"
- "How do those counties compare with the rest of the state on poverty and food insecurity? Is the gap real or noise?"
- "Does the latest CDC data still show those counties with high obesity?"
- "How many counties have no supercenter, and what is their poverty rate?"

It does **not** recommend policy. Asked "should we fund X in county Y", it says in one sentence that the data cannot show what a program would change, then runs the comparison it can do in the same turn.

## Data: where it comes from and how it is loaded

Two spreadsheets are committed in `data/` (12 MB) together with the compiled database (10 MB) and mappings; they are compiled offline into a read-only DuckDB file plus a mappings document. No LLM is used in the build step; the column descriptions come from the publishers' own data dictionaries.

| Source | File | What it contributes |
|---|---|---|
| USDA ERS **Food Environment Atlas**, 2025 release (data years 2012–2023) | `data/food_environment_atlas_2025.xlsx` | Nine county tables: `food_access` (low-income & low-access population, no-car households far from a store), `food_stores` (grocery / supercenter / convenience / specialty counts and per-1,000), `restaurants`, `food_assistance` (SNAP benefits per capita, WIC, school meals), `food_insecurity`, `food_taxes`, `local_foods`, `food_health` (adult diabetes 2015/2019, recreation facilities), `socioeconomic` (poverty 2021, median income, race/ethnicity, metro flag). |
| **County Health Rankings 2023**, Additional Measures sheet | `data/county_health_rankings_2023_additional_measures.xlsx` | `health_rankings` (3,142 counties × 141 measures: % adults with diabetes, % food insecure, % limited access to healthy foods, % uninsured adults, life expectancy, median household income, segregation indices, …) and `health_rankings_state` (51 state rows). |
| **CDC PLACES** county estimates, 2025 release (2023 BRFSS data) | live, `https://data.cdc.gov/resource/swc5-untb.json` | The external source: age-adjusted adult obesity, diagnosed diabetes, high blood pressure, SNAP receipt, food insecurity, no insurance, etc. Fetched per request, cached 6 hours. |

`counties` is the dimension table (3,156 rows: `fips`, `state_abbr`, `state_name`, `county_name`, `metro23`, `population_2021`); every measure table has one row per county keyed by `fips`.

Two properties of the Atlas decide whether a Text2SQL answer is right or wrong, and both are encoded in the build:

1. **State-level columns.** Atlas columns marked with `*` in the publisher's dictionary (adult obesity `pct_obese_adults22`, SNAP participation `pct_snap`, household food insecurity `foodinsec_21_23`, soda/snack taxes, policy flags) carry the **same value for every county in a state**. `mappings.json` flags them `is_state_level: true`, the glossary points to a county-level alternative (County Health Rankings or CDC PLACES), `rank_counties`/`compare_groups` refuse them with an explanation, and `run_sql` attaches a warning to any result that reads one.
2. **Suppressed store counts.** County Business Patterns counts (`groc`, `superc`, `convs`, `specs`, `ffr`, `fsr`, `recfac`) are blank when a county has fewer than three establishments (2020 columns) or none (2016 columns). They are loaded as `NULL`, documented as "NULL means suppressed, not zero", and `run_sql` rejects `COALESCE(superc20, 0)`-style queries that would turn suppression into zeros.

Sentinels `-9999` / `-8888` / `N/A` become `NULL`. FIPS codes are zero-padded text so `"01001"` joins correctly.

Build (only needed if the spreadsheets change):

```bash
uv run --group build build_db.py     # -> data/food_health.duckdb, data/mappings.json
```

## Tools

Numbers are computed by code, not by the model. The prompt forbids mental arithmetic, and the harness checks every figure in a draft against the tool results (a check with one retry, described under [Harness guards](#harness-guards), not a guarantee).

- `tools.py` holds the JSON schemas and dispatcher, `analysis.py` the pure functions, `cdc_places.py` the external fetch.
- Every tool returns JSON. Every error is `{"error": {"kind": ..., "message": ...}}` with a message the model can act on, never a traceback.
- Results carry code-written sentences (`reason`, `sentence`, `summary`, `warnings`) that the model restates rather than inventing causes.

### Offline context tools

**`read_mappings(table?, search?)`** — the Text2SQL "schema + glossary" layer.

- No arguments: tables, join key, data notes, glossary.
- `table`: every column with type, description, units, year, `is_state_level`, `is_enum`.
- `search`: keyword hits across 604 columns (max 40) plus matching glossary terms.
- Errors: `unknown_table`.

**`get_enum_values(table, column)`** — exact values of a categorical column (`state_abbr`, `metro23`, SNAP policy flags).

- Errors: `unknown_table`, `unknown_column`, `not_enum`.

### Data tools

**`run_sql(query)`** — one read-only DuckDB `SELECT`/`WITH`.

- Limits: max 200 rows, 15 s timeout; the connection is opened with `read_only=True, enable_external_access=False, lock_configuration=True`.
- Guardrails: forbidden keywords (`attach`, `copy`, `install`, `create`, `insert`, …), raw sentinels (`-9999`), and the suppression trap above.
- A query that reads a state-level column still runs, but the result carries a `warnings` entry naming the column, saying the value is the state's rate on every county row, and pointing to the county-level alternative.
- Empty results include a hint about common filter mistakes (`county_name` has no "County" suffix; state codes are upper-case).
- Errors: `read_only`, `invalid_argument`, `sql_error` (DuckDB's message), `timeout`.

**`rank_counties(criteria, state_name?, exclude_counties?, require_counties?, top_n?)`** — the counties in a state that meet **every** criterion, ordered by how extreme they are.

- **Criteria.** A list of `{metric: "table.column", direction: "high"|"low", threshold: {type: "state_median"|"percentile"|"value"|"none", value?}}`. The default threshold is the state median.
  - `{type: "none"}` applies no cutoff and ranks every county with a value. A plain "highest N" question needs it, since the default would drop every county at or below the median before ranking (asking for New York's top 40 on diabetes returned 26). When the default leaves fewer counties than `top_n`, the result warns and names `none`.
  - Two shorthands are accepted because they have one meaning: `threshold: "median"`, and a bare `"table.column"` string as a whole criterion.
  - Operators (`"operator": ">"`), expression strings (`"col > median"`) and unknown fields are rejected, not guessed.
- **Computed in code:** the cutoff per criterion over all counties in the state, each county's within-state percentile, which criteria it meets, and a score (mean percentile).
- **Exclusions.** `exclude_counties` / `require_counties` are explicit parameters. Excluded counties stay in the state benchmark and the result says so; required counties that fail a criterion are listed separately under `required_not_qualifying`.
- **Returns** `criteria[].cutoff/rule/state_median`, `ranking_rule`, `n_qualifying`, `counties_missing_data`, `excluded_metrics` with the reason each was dropped, `warnings`, `caveats`, and `selected[]` with values, percentiles and a `reason` sentence, for example:

  > "Montgomery ranks 1st of 11 qualifying counties because it meets every criterion and its average percentile is 97: Low income & low access to store (%), 2019 18.4% (4.7x the median; higher than 100% of New York counties); % Adults with Diabetes (2020) 9.8% (vs median 8.5%; higher than 94% of New York counties)."
- **Side effect:** stores the state, selected counties and exclusions in the session so follow-ups can omit them.
- **Errors:** `invalid_argument` (unknown state, malformed criteria, bad direction/threshold; criteria errors quote the expected shape so the model can fix the call in one round), `no_usable_metric` (all metrics state-level or non-numeric). County names in `exclude_counties` / `require_counties` that match nothing are reported in `warnings`, not as an error.

**`profile_counties(counties, metrics, state_name?)`** — the lookup for one or a few named counties ("how does Kings County do on food access", a borough or city that maps to a county, the side-by-side after a declined funding question).

- **Returns,** per county and metric: the published value, the state county median, the county's rank and percentile among counties with data, and a code-written `sentence`:

  > "Low income & low access to store (%), 2019: Kings is less than 0.1%, below the New York county median of 3.9%; it ranks 60th highest of 62 counties with data (at or above 5% of them). This is the value as published in the source table."
- **Why the sentence exists:** the model, given the raw 0.0016 from `run_sql`, called the value "not credible". It is the Atlas's published figure (about 41 residents); see [What I observed while testing](#what-i-observed-while-testing).
- Does not change the session's selected counties.
- **Errors:** `invalid_argument` (unknown state, no county matched), `no_usable_metric`.

**`compare_groups(metrics, group_a="selected", group_b="rest_of_state", state_name?)`** — medians, means, median difference and a 95% bootstrap interval per metric.

- **Method:** 2,000 resamples, seed 4570, reproducible. The interval resamples counties only; it does not carry the sampling or model error inside each county's published estimate, and the result's `interpretation` field says so.
- **Groups:** `selected`, `rest_of_state`, `metro`, `nonmetro`, `all_state`, or `{"counties": [...]}`. Minimum group size 3.
- **Wording:** each metric comes with a sentence stating whether the interval includes zero ("the gap could be chance"). An interval that includes zero is reported as "cannot be told apart from chance", never as "no gap", and differences are in percentage points.
- **Errors:** `invalid_argument`, `no_usable_metric`.

**`fetch_cdc_places(measures?, fips?, counties?, state_name?, scope?, top_n?, exclude_counties?, value_type?, benchmark_state?)`** — the external tool.

- **Measures:** `OBESITY`, `DIABETES`, `BPHIGH`, `CHD`, `LPA`, `FOODSTAMP`, `FOODINSECU`, `ACCESS2`, `CHECKUP`, `GHLTH`.
- **Which counties:** the session's selected counties (default), a given list (max 80), or every county in a state (`scope: "whole_state"`, ranked by the first measure in code). A selection made in one state is not reused when `state_name` names another: the call falls back to the whole state, or errors if `scope: "selected"` was asked for.
- **Benchmark:** it also fetches the whole state to return the state median and `above_state_median` per county. `exclude_counties` drops named counties from the returned list or ranking while keeping them in the state median, and the result says so.
- **Returns** value, 95% CI, data year, `vs_state_median_points`, `summary` sentences, and a note that PLACES values are a cross-check and do not replace the Atlas/Rankings values the ranking used.
- **Errors:** `invalid_argument`, `unsupported_measure`, `rate_limited`, `service_unavailable`.

### Session state

`session_id -> {messages, state}` in process memory. `state` holds `state_abbr`, `state_name`, `selected_state`, `selected_fips/names`, `excluded_fips`, `last_sql`, `last_ranking`, `last_comparison`, `last_places`. Tools receive it, so "those counties" and "same thing but for poverty" work without repeating the state. `GET /state` exposes it to the UI's side panel.

## Three sample queries

For each query: the answer I expect and the table it comes from, then this agent's answer next to the same model's answer with no tools and no system prompt.

- **Expected** numbers come from the tool results and were checked directly in DuckDB (`data/food_health.duckdb`, built from the two spreadsheets in `data/`).
- **This agent** was recorded on 2026-11-18 with the deployed code (`vertex_ai/gemini-3.8-flash`, low thinking effort), each query in a fresh session.
- **Same model, no tools** is `uv run compare.py --mode notools "<question>"`: the same model and settings, the question sent bare. Recorded the same day.

### 1. Ranking

> "In New York State, which counties combine limited access to healthy food with adult diabetes above the state median? Show the top 5 and the thresholds you used."

**Expected.** `rank_counties` with two state-median criteria:

- low-income & low-access population above the NY county median of **3.87%**: table `food_access`, column `pct_laccess_lowi19` (USDA Food Environment Atlas, 2019; County Health Rankings carries the same value as `health_rankings.pct_limited_access_to_healthy_foods`)
- adult diabetes above the NY county median of **8.5%**: table `health_rankings`, column `pct_adults_with_diabetes` (County Health Rankings 2023, BRFSS 2020)
- **11 counties qualify**; top 5 by mean percentile: Montgomery (18.4%, 9.8%), Franklin (7.6%, 10.0%), St. Lawrence (7.3%, 9.2%), Orange (6.0%, 9.3%), Chemung (6.2%, 9.0%)

| | Low-access threshold | Diabetes threshold | Top 5 counties |
|---|---|---|---|
| **Expected** | 3.87% (state median) | 8.5% (state median) | Montgomery, Franklin, St. Lawrence, Orange, Chemung |
| **This agent** | 3.87% | 8.5% | Montgomery, Franklin, St. Lawrence, Orange, Chemung (and says 11 qualify) |
| **Same model, no tools** | "> 6.0%", plus food insecurity "> 11.0%" | "> 8.8%" | Bronx, Sullivan, Cattaraugus, Chautauqua, Niagara |

- **This agent** (`read_mappings`, `rank_counties`): *"Montgomery, Franklin, St. Lawrence, Orange, and Chemung show the highest co-occurrence of limited food access and adult diabetes above New York state medians, led by Montgomery where 18.4% of residents face low income and low food access—4.7 times the median."*
- **No tools:** invented thresholds and redefined "limited access" as food insecurity. Of its five counties only Chautauqua meets both criteria in the tables, and it ranks 7th of the 11 that do.

### 2. Comparison with uncertainty

> "In Texas, which 5 counties have the highest share of households with no car and more than 1 mile from a store? Compare them to the rest of Texas on poverty rate and food insecurity."

**Expected.**

- `rank_counties` on table `food_access`, column `pct_laccess_hhnv19` (Atlas, 2019; one criterion, `threshold: {type: "none"}`, `top_n: 5`) → Kent 10.1%, Cottle 9.2%, Zavala 8.6%, Kenedy 7.2%, Terrell 6.3%
- `compare_groups` on `socioeconomic.povrate21` (Atlas, 2021) and `health_rankings.pct_food_insecure` (County Health Rankings 2023, Map the Meal Gap 2020):
  - poverty median **17.4% vs 15.5%** (+1.9 pp, 95% interval [-3.8, 9.9])
  - food insecurity **14.1% vs 14.8%** (-0.7 pp, [-5.7, 8.0])
- both intervals include zero, so the answer should say the gaps could be chance
- the trace may show one more call, `profile_counties` on the five counties, which the model uses to fill the per-county table; the numbers do not change

| | Top 5 counties | Poverty: top 5 vs rest | Food insecurity: top 5 vs rest | Verdict |
|---|---|---|---|---|
| **Expected** | Kent, Cottle, Zavala, Kenedy, Terrell | 17.4% vs 15.5% | 14.1% vs 14.8% | Both gaps could be chance |
| **This agent** | Kent, Cottle, Zavala, Kenedy, Terrell | 17.4% vs 15.5%, +1.90 pp [-3.80, +9.90] | 14.1% vs 14.8%, -0.70 pp [-5.70, +8.00] | "cannot be distinguished from the rest of the state" |
| **Same model, no tools** | Presidio, Zavala, Brooks, Hudspeth, Willacy | "~26.5%" vs "13.8% – 14.2%" | "~21.8%" vs "15.5% – 16.4%" | "roughly 1.5 to nearly 2.5 times higher" |

- **This agent** (`read_mappings` ×2, `rank_counties`, `compare_groups`, `profile_counties`): *"Kent, Cottle, Zavala, Kenedy, and Terrell have the highest shares of households without a car and living far from a store in Texas, but as a group their poverty and food insecurity medians cannot be distinguished from the rest of the state."*
- **No tools:** only Zavala is in both lists. It set aside Kenedy and Terrell, two of the actual top five, as counties whose "margins of error fluctuate wildly", and reported a large gap where the published data shows none that can be told apart from chance.

### 3. Aggregate with a metric the data lacks

> "What are the median adult diabetes rate and median child food insecurity rate across Mississippi counties, and how many Mississippi counties have child food insecurity above 25%?"

**Expected.** `run_sql` over table `health_rankings` joined to `counties` (`state_abbr = 'MS'`):

- median adult diabetes **13.0%** (`pct_adults_with_diabetes`, County Health Rankings 2023, age-adjusted, 2020)
- no county-level *child* food insecurity measure exists in these tables; the nearest is all-ages food insecurity (`pct_food_insecure`), median **17.9%**
- **2 counties** above 25% on that measure: Holmes 27.6%, Humphreys 25.4%
- the model often also reports the Atlas 2019 crude diabetes median (`food_health.pct_diabetes_adults19`), **10.6%**, labelled as a different version of the measure and not averaged

| | Median adult diabetes | Median child food insecurity | Counties above 25% |
|---|---|---|---|
| **Expected** | 13.0% | Not in the data (all ages: 17.9%) | 2 (all ages) |
| **This agent** | 13.0% (and 10.6% crude, 2019, labelled separately) | Says no county-level child measure exists; all ages 17.9% | 2: Holmes 27.6%, Humphreys 25.4% |
| **Same model, no tools** | "~15.4%" | "~24.5%" | "38 of 82 counties" |

- **This agent** (`read_mappings`, `run_sql` ×4): *"The database does not have a county-level child food insecurity measure; using the county-level all-ages measure, the county median food insecurity rate across Mississippi is 17.9%, the county median adult diabetes rate is 13.0% (County Health Rankings 2020), and exactly 2 counties have food insecurity above 25%."*
- **No tools:** answered the child question as if the figures existed, citing sources it did not query; all three numbers are off.

### Follow-ups tested in the same session (the UI's suggestion chips)

All after query 1:

- **"How do those counties compare with the rest of the state on poverty rate, food insecurity and uninsured adults? Is the gap real?"** → `compare_groups`, 5 vs 57 counties: poverty 15.0% vs 12.9% (+2.1 pp, [-0.7, 3.8]), food insecure 13.4% vs 11.0%, uninsured adults 6.6% vs 6.1%. Every interval includes zero.
- **"Does the latest CDC data still show those counties with high obesity and diabetes?"** → `fetch_cdc_places` (2023 data): all five above the NY diabetes median of 9.0%. St. Lawrence 42.9%, Franklin 40.8% and Montgomery 38.9% are above the obesity median of 32.9%; Orange (32.4%) and Chemung (30.8%) are below it.
- **"Redo the first ranking but leave out the five New York City boroughs."** → the same call with `exclude_counties`, and the result notes the boroughs still count toward the benchmark.
- **"How many New York counties had zero supercenters in 2020, and what is their median poverty rate?"** → `run_sql` with `superc20 IS NULL`: **31 counties have fewer than three supercenters** (the count is suppressed, not zero), median poverty 13.3%.

## Two questions a plain `run_sql` agent got wrong

`uv run compare.py --mode baseline "<question>"` runs the same model with only `run_sql`, a one-paragraph prompt listing the table names, and none of the mappings, glossary, guards or analysis tools (its `run_sql` also runs without the state-level warning). It has the same budget as this agent, 10 tool rounds. The traces are verbatim from `compare.py` (results truncated); open each one to see it.

I recorded the plain agent with two models:

- **Gemini 2.5 Flash, 2026-11-05** (the deployed model at the time): it answered both questions, and both answers are wrong.
- **Gemini 3.8 Flash, 2026-11-18** (deployed now, default thinking level): three runs per question, and all six ended with `(tool-call limit reached)` and no answer. It noticed the state-level columns but spent its ten rounds looking for a replacement it could not find (in one run of question B it was still looking up columns when the rounds ran out). I did not test whether a larger round budget would let it finish.

This agent's traces below are from Gemini 3.8 Flash at the default thinking level, before the switch to low; the tools it calls and the numbers it reports are the same at both levels.

### A. "Which 5 New York counties have the highest adult obesity rate, and what is each county's rate?"

**Plain `run_sql`, Gemini 2.5 Flash: wrong.** It answered *"All New York counties have the same reported adult obesity rate of 30.1%. The top 5 counties by adult obesity rate are: Albany 30.1%, Schoharie 30.1%, Bronx 30.1% …"* `pct_obese_adults22` is a state-level column: 30.1% is New York State's rate repeated 62 times, and a "top 5" from it is meaningless.

<details>
<summary>Plain <code>run_sql</code> trace, Gemini 2.5 Flash</summary>

After discovering columns by `SELECT * … LIMIT 1`, it ran:

```sql
SELECT counties.county_name, food_health.pct_obese_adults22
FROM counties JOIN food_health USING (fips)
WHERE counties.state_abbr = 'NY' ORDER BY food_health.pct_obese_adults22 DESC LIMIT 5
-- → Albany 30.1, Allegany 30.1, Bronx 30.1, Broome 30.1, Cattaraugus 30.1
```

</details>

**Plain `run_sql`, Gemini 3.8 Flash: no answer.** It worked out by query that `pct_obese_adults22` holds one value per state, took five rounds to do so, then confirmed that no table has a county-level obesity column. The plain agent has no way to reach one.

<details>
<summary>Plain <code>run_sql</code> trace, Gemini 3.8 Flash</summary>

Calls 1–3 searched `information_schema.columns` for obesity columns and found only the Atlas's two. Then:

```sql
-- call 4
SELECT c.county_name, fh.pct_obese_adults22, fh.pct_obese_adults17
FROM counties c JOIN food_health fh USING (fips)
WHERE c.state_abbr = 'NY' ORDER BY fh.pct_obese_adults22 DESC NULLS LAST LIMIT 10
-- → Albany 30.1, 25.7 · Allegany 30.1, 25.7 · Bronx 30.1, 25.7 · Broome 30.1, 25.7 · Cattaraugus 30.1, 25.7 · …

-- call 5
SELECT DISTINCT pct_obese_adults22 FROM food_health fh JOIN counties c USING (fips) WHERE c.state_abbr = 'NY'
-- → 30.1

-- call 9
SELECT state_abbr, COUNT(DISTINCT pct_obese_adults22), COUNT(DISTINCT pct_obese_adults17)
FROM counties JOIN food_health USING (fips) GROUP BY state_abbr
HAVING COUNT(DISTINCT pct_obese_adults22) > 1 OR COUNT(DISTINCT pct_obese_adults17) > 1
-- → no rows
```

Calls 6–8 and 10 were more schema lookups (one `DESCRIBE`, rejected as not a `SELECT`; the column list of `health_rankings`). Answer: `(tool-call limit reached)`.

</details>

**This agent: right, in two calls.** `read_mappings` marks the column state-level and the glossary entry for *adult obesity* points to CDC PLACES, so it went straight to the external source:

> **The Food Environment Atlas cannot rank counties by adult obesity because its obesity measures are state-level, but CDC PLACES model-based estimates identify St. Lawrence County as having New York's highest adult obesity rate at 42.9%, followed by Franklin (40.8%), Lewis (40.7%), Niagara (39.4%), and Montgomery (38.9%).**

followed by a table giving each county's 95% confidence interval and its difference from the state county median of 32.9% (CDC PLACES 2025 release, 2023 data).

<details>
<summary>This agent's trace</summary>

```
read_mappings({"search": "obesity"})
→ "Adult obesity rate, 2017 [STATE-level: same value for every county in the state]" … "is_state_level": true
fetch_cdc_places({"scope": "whole_state", "state_name": "NY", "measures": ["OBESITY"], "top_n": 5})
→ ranking_note: "All 62 counties in New York were fetched and sorted by Obesity among adults (BMI >= 30) (highest first); showing the top 5. Ranks are 1 = highest."
```

</details>

### B. "In Texas, which 5 counties have the highest share of households with no car and more than 1 mile from a store? Compare them to the rest of Texas on poverty rate and food insecurity."

**Plain `run_sql`, Gemini 2.5 Flash: wrong.** It answered *"The average food insecurity rate for these 5 counties is 16.90%. For the rest of Texas counties, the average food insecurity rate is also 16.90%. This suggests that, on average, food insecurity is similar between these two groups."* `foodinsec_21_23` is Texas's statewide rate on every row, so the "comparison" compares a constant with itself. It also computed the 5-county poverty mean (17.46%) in its head.

<details>
<summary>Plain <code>run_sql</code> trace, Gemini 2.5 Flash</summary>

It found the right access column, then for food insecurity picked the Atlas table:

```sql
SELECT counties.county_name, food_access.pct_laccess_hhnv19, socioeconomic.povrate21, food_insecurity.foodinsec_21_23
FROM counties JOIN food_access USING (fips) JOIN socioeconomic USING (fips) JOIN food_insecurity USING (fips)
WHERE counties.state_abbr = 'TX' ORDER BY food_access.pct_laccess_hhnv19 DESC LIMIT 5
-- → Kent 10.06, 11.5, 16.9 · Cottle 9.23, 18.8, 16.9 · Zavala 8.62, 25.4, 16.9 · …
SELECT AVG(socioeconomic.povrate21), AVG(food_insecurity.foodinsec_21_23) FROM … WHERE state_abbr = 'TX' AND county_name NOT IN (…)
-- → 15.80, 16.90
```

</details>

**Plain `run_sql`, Gemini 3.8 Flash: half the work, no answer.** By call 7 it had the right five counties and both food-insecurity columns side by side. Call 8 established that `foodinsec_21_23` is Texas's statewide rate on every row. It then ran out of rounds before computing the comparison with the rest of the state, which is the second half of the question.

<details>
<summary>Plain <code>run_sql</code> trace, Gemini 3.8 Flash</summary>

Calls 1–6 discovered columns (`DESCRIBE`, rejected; `SELECT * … LIMIT 1` on three tables; two `information_schema` searches). Then:

```sql
-- call 7
SELECT c.county_name, c.fips, fa.pct_laccess_hhnv19, fa.pct_laccess_hhnv15, se.povrate21, fi.foodinsec_21_23, hr.pct_food_insecure
FROM counties c JOIN food_access fa USING (fips)
LEFT JOIN socioeconomic se USING (fips) LEFT JOIN food_insecurity fi USING (fips) LEFT JOIN health_rankings hr USING (fips)
WHERE c.state_abbr = 'TX' ORDER BY fa.pct_laccess_hhnv19 DESC NULLS LAST LIMIT 10
-- → Kent 10.06, 4.28, 11.5, 16.9, 14.1 · Cottle 9.23, 11.74, 18.8, 16.9, 19.5 · …

-- call 8
SELECT DISTINCT foodinsec_21_23, foodinsec_18_20 FROM counties JOIN food_insecurity USING (fips) WHERE state_abbr = 'TX'
-- → 16.9, 13.3

-- call 10
SELECT * FROM health_rankings_state WHERE state_abbr = 'TX'
-- → Binder Error: Referenced column "state_abbr" not found in FROM clause!
```

Answer: `(tool-call limit reached)`.

</details>

**This agent: right, in nine calls.** The mappings told it which food-insecurity column is county-level, and the tools did the arithmetic:

> **Kent, Cottle, Zavala, Kenedy, and Terrell have the highest shares of households with no car and low store access in Texas, led by Kent at 10.1% (4.7x the state median of 2.1%), but their differences in poverty and food insecurity compared to the rest of the state cannot be distinguished from chance.**

followed by a per-county table and the comparison: poverty 17.4% vs 15.5% (+1.9 pp, [-3.8, 9.9]), food insecurity 14.1% vs 14.8% (-0.7 pp, [-5.7, 8.0]), both marked "Interval excludes zero? No".

<details>
<summary>This agent's trace</summary>

Six `read_mappings` calls searched for the car, poverty and food-insecurity columns, then read the `food_access` table and the overview. Then:

```
rank_counties({"top_n": 5, "state_name": "Texas", "criteria": [{"direction": "high", "threshold": {"type": "none"}, "metric": "food_access.pct_laccess_hhnv19"}]})
compare_groups({"metrics": ["socioeconomic.povrate21", "health_rankings.pct_food_insecure"]})
→ "% Food Insecure (2020): median 14.1% in the 5 selected counties (n=5) vs 14.8% in the other 249 counties in the state (n=249), a difference of -0.70 percentage points; the 95% bootstrap interval -5.70 to +8.00 includes zero, so the gap could be chance."
profile_counties({"metrics": ["food_access.pct_laccess_hhnv19", "socioeconomic.povrate21", "health_rankings.pct_food_insecure"], "state_name": "Texas", "counties": ["Kent", "Cottle", "Zavala", "Kenedy", "Terrell"]})
```

</details>

### A third case: "zero" supercenters

Asked how many New York counties had *zero* supercenters in 2020 and their median poverty rate:

- **Gemini 2.5 Flash, plain `run_sql`:** answered "no New York counties had zero supercenters in 2020", because `superc20 = 0` matches nothing; the suppressed counties are `NULL`.
- **Gemini 3.8 Flash, plain `run_sql`:** ran `WHERE … fs.superc20 = 0`, got no rows, then counted the column over all counties (3,144 rows: 675 with a value, 0 zeros, 2,469 `NULL`) and hit the limit there. It found that the blanks are `NULL`, but nothing in the database says that `NULL` means "fewer than three, suppressed".
- **This agent:** "31 counties have fewer than three supercenters", with the reason.

## What I observed while testing

In one test with Haiku, I asked, "Which census tracts in Brooklyn have the worst food access?" It correctly explained that the data only goes down to the county level, then used `run_sql` to look up Kings County. But it labelled the low-income and low-access population share of **0.0016%** as "not credible" and said it could not assess food access there. This is the Atlas's published value, corresponding to about 41 residents, and County Health Rankings carries the same number. The model had retrieved the right value but then dismissed it.

I first added a wording check that sent drafts containing "not credible" back for revision. In the next attempt, Haiku avoided those words but still claimed the value was on a different scale from the state figures and should not be treated as a percentage. The check changed the wording without correcting the interpretation.

I then added `profile_counties`, which computes the comparison and writes the explanation in code: "Kings is less than 0.1%, below the New York county median of 3.9%; it ranks 60th highest of 62 counties." In a subsequent test, Haiku restated that explanation without dismissing the value. For this case, giving the model a sentence grounded in the calculation worked better than asking it not to comment or blocking particular words. This follows the same approach I use for ranking reasons and group comparisons elsewhere in the project.

## Interface and agent architecture

### Backend

`app.py`: FastAPI + LiteLLM, built on the course starter's bounded tool loop.

- **Loop:** at most 10 tool rounds per turn (`MAX_TOOL_ROUNDS`), temperature 0.2, short backoff on Vertex 429. The temperature is not sent to the Claude API, where `claude-haiku-5-5` accepts only its default.
- **Thinking effort:** Gemini runs with `reasoning_effort="low"`. At the model's default level, most of a turn's time went into thinking: the final answer to sample query 2 was about 2,300 output tokens (about 1,400 of them thinking) and the turn took 31 s. At `low` it was about 770 tokens and 13–19 s, with the same tool calls and the same figures on all three sample queries, the four follow-ups and the "can't answer" questions. When Vertex is slow, time grows with output tokens, so this keeps turns inside the deadline. The sample-query answers and the no-tools answers in this README were recorded at `low`; the plain `run_sql` comparison was recorded at the default level, as that section says.
- **Model,** chosen with `MODEL` through LiteLLM:
  - Vertex AI `vertex_ai/gemini-3.8-flash`: the default and what is deployed. A GCP project created after the 2.5 models were closed to new projects gets 404 for `gemini-2.5-flash`.
  - Claude API `anthropic/claude-haiku-5-5`.
  - Local Ollama `ollama_chat/gpt-oss:20b`, the only local model supported.
- **Single agent:** every tool call carries `"agent": "analyst"`.

### Harness guards

Code, not the prompt, checks each draft answer. Each kind of problem gets one retry.

- **Unbacked figures.** Figures in the draft must appear in the user's message or a tool result (dollar, percent and percentage-point values, including ×100 rescaling; "95% interval" / "95% CI" is read as the method's name, not a figure). Otherwise the draft is discarded and the model told to call a tool. If the second draft still has unbacked figures, it is returned with an "Unverified figures" note listing them, so they are never passed off silently.
- **No data behind a data answer.** A data question answered with no successful data tool, or after every data tool errored, is sent back (the model once invented a 31-county list after a failed query).
- **Repeats and empty replies** are retried.
- **Added commentary.** A draft that adds what no tool produced (a verdict that a value is "not credible", a hedged "likely", an unprompted "I can also...") is sent back, matched on wording that appears in no tool result.
- **Opening sentence.** After a data tool runs, the answer must open with one bold sentence.
- **Policy questions.** A funding/policy question must contain the one-sentence decline.
- **Plain text.** Inline LaTeX the model sometimes writes despite the prompt (`BMI $\ge$ 30`) is turned into plain characters (`BMI ≥ 30`), since the interface renders Markdown only.
- **Turn deadline.** Each turn has 240 s and each model call at most 90 s, inside Cloud Run's 300 s request limit. When time runs out, the reply is built in code from the sentences the tools had already written, says it was cut short, and asks the user to try again. Close to the deadline, a draft with a problem goes out with its warning note instead of being sent back. This was added after Vertex calls that usually take a few seconds took 50–75 s for several minutes on 2026-11-18 and the Texas sample question hit the 300 s limit twice with no answer.

### Frontend

`index.html`, written for this project:

- **Left panel:** what the tool explores, who it is for and how to use it, with four suggestion chips.
- **Chat:** renders Markdown and shows each tool call as a collapsible card (name, arguments, SQL, result), coloured by kind (context / SQL / compute / external / error).
- **Right panel:** reads `/state` and draws the selected counties against the state-median line per criterion, the latest comparison with its interval, the CDC PLACES cross-check, and the last SQL.
- **Cold start:** on load it pings `/health` and shows a wake-up notice while the Cloud Run instance starts.

### API

`/chat` keeps the starter contract:

```json
{
  "response": "Markdown answer",
  "session_id": "…",
  "tool_calls": [{"name": "rank_counties", "args": {…}, "result": "{…json…}", "agent": "analyst"}]
}
```

Other endpoints: `GET /state?session_id=`, `POST /clear?session_id=`, `GET /health`.

## Scope and limitations

- Cross-sectional county data from different years (Atlas 2019–2022, CHR 2023 using 2019–2020 inputs, PLACES 2023): the agent reports co-occurrence, not causes, and says so in every answer.
- Adult obesity and SNAP participation exist in the Atlas only at state level; county values come from CDC PLACES, a model-based estimate for a later year, labelled as such and never substituted into rankings.
- Store counts under three establishments are suppressed; "zero stores" cannot be answered from these tables, and the answer says that before giving the suppressed count.
- Connecticut: the Atlas and County Health Rankings publish values for the 8 historical counties, CDC PLACES for the 9 planning regions that replaced them in 2022. Rankings and comparisons use the 8 counties (the 9 regions are empty rows and are left out of counts and medians), and the CDC cross-check cannot be matched to them; both tools return a warning that says this.
- The bootstrap interval reflects variation between counties, not the uncertainty of each county's own estimate, so it understates the full uncertainty.
- Sessions live in process memory on a single Cloud Run instance; a restart clears them.
- The figure guard checks that numbers exist in tool output, not that the prose uses them correctly, and it retries once: a figure that survives the retry is flagged in the answer, not removed.
- Response time depends on the model service. Answers usually take 5–30 s, plus about 20 s when an idle instance starts. When Vertex is slow, a turn can reach the 240 s deadline and return the cut-short reply described under [Harness guards](#harness-guards).

## Next stage plan

- **Data updates and versioning.** The local Atlas and County Health Rankings data still need to be updated manually. I would next check for newer releases and add a versioned update process that preserves the existing snapshots and records the release and data years used in each answer. Before loading an update, I would check for changes in column names, metric definitions and county FIPS codes so that a newer file does not silently change what a comparison means.
- **More systematic reliability testing.** Most of my testing so far has involved a small number of questions and follow-ups. I would turn these into a fixed set of regression cases covering alternative wording, session follow-ups, unavailable metrics and very small published values such as Kings County's food-access share. I would rerun them after prompt, tool or model changes, checking both the answers and the tool calls to see whether earlier fixes still hold.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), Python ≥ 3.10.

```bash
uv sync

# Claude API
ANTHROPIC_API_KEY=<your-key> MODEL="anthropic/claude-haiku-5-5" uv run uvicorn app:app --port 8000

# Local, no API key: Ollama
ollama pull gpt-oss:20b
MODEL="ollama_chat/gpt-oss:20b" uv run uvicorn app:app --port 8000

# Vertex AI (course default)
gcloud auth application-default login
VERTEXAI_PROJECT=<your-gcp-project> uv run uvicorn app:app --port 8000
```

Models:

| Setup | `MODEL` | Needs |
|---|---|---|
| Claude API | `anthropic/claude-haiku-5-5` | `ANTHROPIC_API_KEY` |
| Local (Ollama) | `ollama_chat/gpt-oss:20b` | Ollama running, `gpt-oss:20b` pulled (13 GB) |
| Vertex AI (default, deployed) | `vertex_ai/gemini-3.8-flash` | `VERTEXAI_PROJECT`, `roles/aiplatform.user` |

In my local tests on November 11, 2026, `gpt-oss:20b` answered the New York and Texas sample questions with results that matched the expected answers. For Mississippi, it correctly noted that the database has no county-level child food-insecurity measure, but stopped without reporting the available all-ages figures. I also tried `qwen2.5:7b`, `llama3.1:8b` and `llama3.2:3b`; `gemma2:9b` did not support tool calling in my setup. These observations come from one test run per model, so they are not enough to establish a general reliability ranking. The local setup I document and support here uses `gpt-oss:20b`.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `MODEL` | `vertex_ai/gemini-3.8-flash` | LiteLLM model name |
| `VERTEXAI_PROJECT` | none | GCP project for Vertex AI |
| `VERTEX_LOCATION` | `global` | Vertex AI location |
| `ANTHROPIC_API_KEY` | none | Claude API key |
| `TURN_DEADLINE_S` | `240` | Seconds a chat turn may run before the cut-short reply |
| `MODEL_CALL_TIMEOUT_S` | `90` | Seconds one model call may take |
| `MODEL_REASONING_EFFORT` | `low` | Gemini thinking level on Vertex; empty for the model's default |
| `PORT` | `8000` | Port when running `app.py` directly |

`compare.py` reproduces the comparison traces above: `uv run compare.py "<question>"` runs the bare model, the `run_sql`-only agent and this agent in sequence.

## Deployment

- **Where:** GCP Cloud Run, project `ieor4570-food-health`, region `europe-west1`, built with Google Cloud buildpacks from this repository. `Procfile` → `uvicorn app:app --host 0.0.0.0 --port $PORT`.
- **Service settings:** `--max-instances 1` (sessions are in memory), 1 GiB memory, startup CPU boost, 300 s request timeout.
- **Credentials:** Gemini is called through the default compute service account (`roles/aiplatform.user`; `roles/run.builder` so source deploys can build). `VERTEXAI_PROJECT` and `MODEL` are service environment variables. No credentials are committed, and `.gcloudignore` keeps `.env` and `.venv` out of the upload.

- **Continuous deployment:** every push to `main` triggers a Cloud Build build (buildpacks) that deploys a new revision of the service. The deploy step changes only the image, so the environment variables and service settings above carry over.

A manual deploy from a local checkout, for example to another project:

```bash
gcloud run deploy food-health-explorer --source . --project <project-id> --region europe-west1 --allow-unauthenticated \
  --max-instances 1 --memory 1Gi --cpu-boost --timeout 300 \
  --set-env-vars VERTEXAI_PROJECT=<project-id>,MODEL=vertex_ai/gemini-3.8-flash
```
