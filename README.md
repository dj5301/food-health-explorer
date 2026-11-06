# County Food & Health Explorer

**A Text2SQL agent for health-department staff: where in a state do limited food access, poverty and food-assistance need coincide with diabetes, obesity and food insecurity, county by county, and does the latest CDC data still show the same places?**

Live: **https://food-health-explorer-1060383691779.europe-west1.run.app** · Source: https://github.com/dj5301/food-health-explorer · Path: `text2sql` · Author: dj2763

## Why this topic

<!-- TODO(dj2763): why you chose this question and this data (your SOSC P6777 HW4 datasets); who you imagined using it. -->

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

1. **State-level columns.** Atlas columns marked with `*` in the publisher's dictionary (adult obesity `pct_obese_adults22`, SNAP participation `pct_snap`, household food insecurity `foodinsec_21_23`, soda/snack taxes, policy flags) carry the **same value for every county in a state**. `mappings.json` flags them `is_state_level: true`, the glossary points to a county-level alternative (County Health Rankings or CDC PLACES), and `rank_counties`/`compare_groups` refuse them with an explanation.
2. **Suppressed store counts.** County Business Patterns counts (`groc`, `superc`, `convs`, `specs`, `ffr`, `fsr`, `recfac`) are blank when a county has fewer than three establishments (2020 columns) or none (2016 columns). They are loaded as `NULL`, documented as "NULL means suppressed, not zero", and `run_sql` rejects `COALESCE(superc20, 0)`-style queries that would turn suppression into zeros.

Sentinels `-9999` / `-8888` / `N/A` become `NULL`. FIPS codes are zero-padded text so `"01001"` joins correctly.

Build (only needed if the spreadsheets change):

```bash
uv run --group build build_db.py     # -> data/food_health.duckdb, data/mappings.json
```

## Tools

The model never computes a number. `tools.py` holds the JSON schemas and dispatcher, `analysis.py` the pure functions, `cdc_places.py` the external fetch. Every tool returns JSON; every error is `{"error": {"kind": ..., "message": ...}}` with a message the model can act on, never a traceback. Results carry code-written sentences (`reason`, `sentence`, `summary`, `warnings`) that the model restates rather than inventing causes.

### Offline context tools

**`read_mappings(table?, search?)`** — the Text2SQL "schema + glossary" layer. No arguments: tables, join key, data notes, glossary. `table`: every column with type, description, units, year, `is_state_level`, `is_enum`. `search`: keyword hits across 604 columns (max 40) plus matching glossary terms. Errors: `unknown_table`.

**`get_enum_values(table, column)`** — exact values of a categorical column (`state_abbr`, `metro23`, SNAP policy flags). Errors: `unknown_table`, `unknown_column`, `not_enum`.

### Data tools

**`run_sql(query)`** — one read-only DuckDB `SELECT`/`WITH`, max 200 rows, 15 s timeout, connection opened with `read_only=True, enable_external_access=False, lock_configuration=True`. Guardrails: forbidden keywords (`attach`, `copy`, `install`, `create`, `insert`, …), raw sentinels (`-9999`), and the suppression trap above. Empty results include a hint about common filter mistakes (`county_name` has no "County" suffix; state codes are upper-case). Errors: `read_only`, `invalid_argument`, `sql_error` (DuckDB's message), `timeout`.

**`rank_counties(criteria, state_name?, exclude_counties?, require_counties?, top_n?)`** — the counties in a state that meet **every** criterion, ordered by how extreme they are.
- `criteria`: list of `{metric: "table.column", direction: "high"|"low", threshold: {type: "state_median"|"percentile"|"value", value?}}` (default threshold: state median).
- Computes, in code: the cutoff per criterion over all counties in the state, each county's within-state percentile, which criteria it meets, and a score (mean percentile). `exclude_counties` / `require_counties` are explicit parameters: excluded counties stay in the state benchmark and the result says so; required counties that fail a criterion are listed separately under `required_not_qualifying`.
- Returns `criteria[].cutoff/rule/state_median`, `ranking_rule`, `n_qualifying`, `selected[]` with values, percentiles and a `reason` sentence (`"Montgomery ranks 1st of 11 qualifying counties because it meets every criterion and its average percentile is 97: Low income & low access to store (%), 2019 18.4% (4.7x the median; higher than 100% of New York counties); % Adults with Diabetes (2020) 9.8% (vs median 8.5%; higher than 94% of New York counties)."`), `counties_missing_data`, `excluded_metrics` with the reason each was dropped, `warnings`, `caveats`.
- Side effect: stores the state, selected counties and exclusions in the session so follow-ups can omit them.
- Errors: `invalid_argument` (unknown state, bad direction/threshold, county names that match nothing), `no_usable_metric` (all metrics state-level or non-numeric).

**`compare_groups(metrics, group_a="selected", group_b="rest_of_state", state_name?)`** — medians, means, median difference and a 95% bootstrap interval (2,000 resamples, seed 4570, reproducible) per metric. Groups: `selected`, `rest_of_state`, `metro`, `nonmetro`, `all_state`, or `{"counties": [...]}`. Each metric comes with a sentence stating whether the interval includes zero ("the gap could be chance"). Minimum group size 3. Errors: `invalid_argument`, `no_usable_metric`.

**`fetch_cdc_places(measures?, fips?, counties?, state_name?, scope?, top_n?, value_type?, benchmark_state?)`** — the external tool. Fetches CDC PLACES estimates (`OBESITY`, `DIABETES`, `BPHIGH`, `CHD`, `LPA`, `FOODSTAMP`, `FOODINSECU`, `ACCESS2`, `CHECKUP`, `GHLTH`) for the session's selected counties (default), a given list (max 80), or every county in a state (`scope: "whole_state"`, ranked by the first measure in code). Also fetches the whole state to return the state median and `above_state_median` per county. Returns value, 95% CI, data year, `vs_state_median_points`, `summary` sentences, and a note that PLACES values are a cross-check and do not replace the Atlas/Rankings values the ranking used. Errors: `invalid_argument`, `unsupported_measure`, `rate_limited`, `service_unavailable`.

### Session state

`session_id -> {messages, state}` in process memory. `state` holds `state_abbr`, `selected_fips/names`, `excluded_fips`, `last_sql`, `last_ranking`, `last_comparison`, `last_places`. Tools receive it, so "those counties" and "same thing but for poverty" work without repeating the state. `GET /state` exposes it to the UI's side panel.

## Three sample queries

Each was run against the deployed code in a fresh session; expected numbers are from the tool results and were checked directly in DuckDB.

**1. Ranking** — "In New York State, which counties combine limited access to healthy food with adult diabetes above the state median? Show the top 5 and the thresholds you used."

Expected: `rank_counties` with two state-median criteria. Thresholds: low-income & low-access population above the NY county median of **3.87%** (`food_access.pct_laccess_lowi19`, Atlas 2019; County Health Rankings carries the same value as `pct_limited_access_to_healthy_foods`), adult diabetes above **8.5%** (`health_rankings.pct_adults_with_diabetes`, CHR 2023 / BRFSS 2020). **11 counties qualify**; top 5 by mean percentile: Montgomery (18.4%, 9.8%), Franklin (7.6%, 10.0%), St. Lawrence (7.3%, 9.2%), Orange (6.0%, 9.3%), Chemung (6.2%, 9.0%).

**2. Comparison with uncertainty** — "In Texas, which 5 counties have the highest share of households with no car and more than 1 mile from a store? Compare them to the rest of Texas on poverty rate and food insecurity."

Expected: `rank_counties` on `food_access.pct_laccess_hhnv19` → Kent 10.1%, Cottle 9.2%, Zavala 8.6%, Kenedy 7.2%, Terrell 6.3%; then `compare_groups` on `socioeconomic.povrate21` and `health_rankings.pct_food_insecure`: poverty median **17.4% vs 15.5%** (+1.9 pp, 95% interval [−3.8, 9.9]), food insecurity **14.1% vs 14.8%** (−0.7 pp, [−5.7, 8.0]); both intervals include zero, so the answer says the gaps could be chance.

**3. Aggregate with a metric the data lacks** — "What are the median adult diabetes rate and median child food insecurity rate across Mississippi counties, and how many Mississippi counties have child food insecurity above 25%?"

Expected: `run_sql` over `health_rankings` joined to `counties` (`state_abbr = 'MS'`): median adult diabetes **13.0%**, median food insecurity **17.9%**, **2 counties** above 25% (Holmes 27.6%, Humphreys 25.4%). The answer states that no county-level *child* food insecurity measure exists in these tables and that the figures are for the whole population.

### Follow-ups tested in the same session (the UI's suggestion chips)

After query 1: "How do those counties compare with the rest of the state on poverty rate, food insecurity and uninsured adults? Is the gap real?" → `compare_groups`, 5 vs 57 counties: poverty 15.0% vs 12.9% (+2.1 pp, [−0.7, 3.8]), food insecure 13.4% vs 11.0%, uninsured adults 6.6% vs 6.1%; every interval includes zero. "Does the latest CDC data still show those counties with high obesity and diabetes?" → `fetch_cdc_places` (2023 data): all five above the NY diabetes median of 9.0%; St. Lawrence 42.9%, Franklin 40.8% and Montgomery 38.9% above the obesity median of 32.9%, Orange (32.4%) and Chemung (30.8%) below it. "Redo the first ranking but leave out the five New York City boroughs." → same call with `exclude_counties`, and the result notes the boroughs still count toward the benchmark. "How many New York counties have zero supercenters, and what is their median poverty rate?" → `run_sql` with `superc20 IS NULL`: **31 counties have fewer than three supercenters** (the count is suppressed, not zero), median poverty 13.3%.

### The same model with no tools and no system prompt

`uv run compare.py --mode notools "<question>"` sends the question to `vertex_ai/gemini-2.5-flash` bare. Answers recorded on 2026-11-05:

1. NY ranking: a confident table with a "NY State median" diabetes rate of **10.5%** and limited-access median of **14.3%**, listing Bronx, Kings, Queens, Suffolk and Richmond. None of these numbers match the published tables (true medians 8.5% and 3.87%; the actual top counties are upstate).
2. Texas no-car counties: Maverick, Hidalgo, Cameron, Webb, Presidio with "~20.0%", "~15.0%" … and a comparison table of poverty and food insecurity values. The Atlas ranks Kent, Cottle, Zavala, Kenedy and Terrell; none of the five named counties are in the top 5.
3. Mississippi medians: "approximately 16.2%" diabetes, "22.1%" child food insecurity, "30 counties" above 25%, citing sources it did not query. The tables give 13.0%, 17.9% (all ages) and 2.

## Two questions a plain `run_sql` agent got wrong

`uv run compare.py --mode baseline "<question>"` runs the same model with only `run_sql`, a one-paragraph prompt listing the table names, and none of the mappings, glossary, guards or analysis tools. Both traces below are verbatim from `compare.py` (results truncated).

### A. "Which 5 New York counties have the highest adult obesity rate, and what is each county's rate?"

**Plain `run_sql`** — after discovering columns by `SELECT * … LIMIT 1`, it ran

```sql
SELECT counties.county_name, food_health.pct_obese_adults22
FROM counties JOIN food_health USING (fips)
WHERE counties.state_abbr = 'NY' ORDER BY food_health.pct_obese_adults22 DESC LIMIT 5
-- → Albany 30.1, Allegany 30.1, Bronx 30.1, Broome 30.1, Cattaraugus 30.1
```

and answered: *"All New York counties have the same reported adult obesity rate of 30.1%. The top 5 counties by adult obesity rate are: Albany 30.1%, Schoharie 30.1%, Bronx 30.1% …"* — `pct_obese_adults22` is a state-level column; 30.1% is New York State's rate repeated 62 times, and a "top 5" from it is meaningless.

**This agent** — `read_mappings` marks the column state-level and the glossary entry for *adult obesity* points to CDC PLACES, so it called

```
fetch_cdc_places({"scope": "whole_state", "state_name": "New York", "measures": ["OBESITY"], "top_n": 5})
→ ranking_note: "All 62 counties in New York were fetched and sorted by Obesity among adults (BMI >= 30) (highest first) …"
```

and answered: *"**St. Lawrence County has the highest adult obesity rate in New York at 42.9%, which is 10 percentage points above the state median.**"* with the table St. Lawrence 42.9, Franklin 40.8, Lewis 40.7, Niagara 39.4, Montgomery 38.9 (CDC PLACES 2025 release, 2023 data, state median 32.9%).

### B. "In Texas, which 5 counties have the highest share of households with no car and more than 1 mile from a store? Compare them to the rest of Texas on poverty rate and food insecurity."

**Plain `run_sql`** — found the right access column, then for food insecurity picked the Atlas table:

```sql
SELECT counties.county_name, food_access.pct_laccess_hhnv19, socioeconomic.povrate21, food_insecurity.foodinsec_21_23
FROM counties JOIN food_access USING (fips) JOIN socioeconomic USING (fips) JOIN food_insecurity USING (fips)
WHERE counties.state_abbr = 'TX' ORDER BY food_access.pct_laccess_hhnv19 DESC LIMIT 5
-- → Kent 10.06, 11.5, 16.9 · Cottle 9.23, 18.8, 16.9 · Zavala 8.62, 25.4, 16.9 · …
SELECT AVG(socioeconomic.povrate21), AVG(food_insecurity.foodinsec_21_23) FROM … WHERE state_abbr = 'TX' AND county_name NOT IN (…)
-- → 15.80, 16.90
```

and answered: *"The average food insecurity rate for these 5 counties is 16.90%. For the rest of Texas counties, the average food insecurity rate is also 16.90%. This suggests that, on average, food insecurity is similar between these two groups."* — `foodinsec_21_23` is Texas's statewide rate on every row, so the "comparison" compares a constant with itself. It also computed the 5-county poverty mean (17.46%) in its head.

**This agent** — rejected the state-level column via the mappings, used County Health Rankings' county measure, and let `compare_groups` do the arithmetic:

```
rank_counties({"criteria": [{"metric": "food_access.pct_laccess_hhnv19", "direction": "high"}], "state_name": "Texas", "top_n": 5})
compare_groups({"metrics": ["socioeconomic.povrate21", "health_rankings.pct_food_insecure"], "group_a": "selected", "group_b": "rest_of_state"})
→ "The median food insecurity rate in the 5 selected counties was 14.1% (n=5) vs 14.8% in the other 249 counties … The 95% bootstrap interval [-5.7, 8.0] includes zero, so the gap could be chance."
```

Answer: *"**The five Texas counties with the highest share of households with no car and low access to a store are Kent (10.1%), Cottle (9.2%), Zavala (8.6%), Kenedy (7.2%), and Terrell (6.3%).** These counties have a higher median poverty rate (17.4%) compared to the rest of Texas (15.5%) … the median food insecurity rate in these counties (14.1%) is slightly lower than the rest of Texas (14.8%) … could be due to chance."*

A third case worth knowing: asked how many New York counties had *zero* supercenters, the plain agent answered "no New York counties had zero supercenters in 2020" because `superc20 = 0` matches nothing; the suppressed counties are `NULL`. This agent reports "31 counties have fewer than three supercenters" and says why.

## What I observed while testing

<!-- TODO(dj2763): one concrete behaviour you saw during testing (e.g. the first obesity ranking listed Franklin out of order and missed Lewis until the ranking moved into fetch_cdc_places; or the agent inventing a 31-county list after its SQL failed, which led to the FAILED_TOOL_NUDGE guard). -->

## Interface and agent architecture

- **Backend** (`app.py`): FastAPI + LiteLLM, the course starter's bounded tool loop (`MAX_TOOL_ROUNDS = 10`, temperature 0.2, short backoff on Vertex 429). Model from `MODEL` (default `vertex_ai/gemini-2.5-flash`; any Ollama tool-calling model works). Single agent; every tool call carries `"agent": "analyst"`.
- **Harness guards** (code, one retry each): figures in the draft must appear in the user's message or a tool result (dollar, percent and percentage-point values, including ×100 rescaling) or the draft is discarded and the model told to call a tool; a data question answered with no successful data tool, or after every data tool errored, is sent back (the model once invented a 31-county list after a failed query); verbatim repeats and empty replies are retried; after a data tool runs the answer must open with one bold sentence; a funding/policy question must contain the one-sentence decline.
- **Frontend** (`index.html`, written for this project): left panel says what the tool explores, who it is for and how to use it, with four suggestion chips; the chat renders Markdown and shows each tool call as a collapsible card (name, arguments, SQL, result) coloured by kind (context / SQL / compute / external / error); the right panel reads `/state` and draws the selected counties against the state-median line per criterion, the latest comparison with its interval, the CDC PLACES cross-check, and the last SQL. On load it pings `/health` and shows a wake-up notice while the Cloud Run instance starts.

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
- Store counts under three establishments are suppressed; "zero stores" cannot be answered from these tables.
- Sessions live in process memory on a single Cloud Run instance; a restart clears them.
- The figure guard checks that numbers exist in tool output, not that the prose uses them correctly.

## Unfinished

<!-- TODO(dj2763): two things you did not finish or would do next. -->

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), Python ≥ 3.10.

```bash
uv sync
gcloud auth application-default login          # Vertex AI (course default)
VERTEXAI_PROJECT=<your-gcp-project> uv run uvicorn app:app --port 8000
# or, without GCP:
MODEL="ollama_chat/gpt-oss:20b" uv run uvicorn app:app --port 8000
```

Environment: `MODEL`, `VERTEX_LOCATION` (default `global`), `VERTEXAI_PROJECT`, `PORT`.

`compare.py` reproduces the baseline traces above: `uv run compare.py "<question>"` runs the bare model, the `run_sql`-only agent and this agent in sequence.

## Deployment

GCP Cloud Run (`europe-west1`), Google Cloud buildpacks from this repository; `Procfile` → `uvicorn app:app --host 0.0.0.0 --port $PORT`. Deployed with `--max-instances 1` (in-memory sessions), 1 GiB memory, startup CPU boost. Gemini is called through the service account (`roles/aiplatform.user`); `VERTEXAI_PROJECT` is a service environment variable; no credentials are committed. Continuous deployment from `main` via Developer Connect, per the course guide.

```bash
gcloud run deploy food-health-explorer --source . --region europe-west1 --allow-unauthenticated \
  --max-instances 1 --memory 1Gi --cpu-boost --set-env-vars VERTEXAI_PROJECT=<project-id>
```
