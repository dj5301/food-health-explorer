"""The tools the harness can run, and the JSON that describes them to the model.

Context tools (read_mappings, get_enum_values) serve what build_db.py prepared offline.
run_sql is the Text2SQL retriever over a locked, read-only DuckDB file. rank_counties and
compare_groups are deterministic analyses (analysis.py) over rows the harness pulls itself.
fetch_cdc_places is the single external source.

Each tool receives the session's `state` dict so the model does not have to repeat what the
user already said: the state under study, the counties the last ranking selected, and the
counties the user asked to leave out. Pass these to set or replace them, omit to reuse.
"""

import json
import re
import threading
from pathlib import Path
from statistics import median

import duckdb

import analysis
from cdc_places import MEASURES as PLACES_MEASURES, VALUE_TYPES, PlacesError, fetch_places

DATA = Path(__file__).parent / "data"
DB_PATH = DATA / "food_health.duckdb"
MAPPINGS_PATH = DATA / "mappings.json"
MAX_ROWS = 200
SQL_TIMEOUT_S = 15
MAX_SEARCH_HITS = 40
MAX_PLACES_FIPS = 80

try:
    MAPPINGS = json.loads(MAPPINGS_PATH.read_text())
    # The security boundary is the connection, not the prompt: read-only, no file or network
    # access, and the model cannot SET its way out of either.
    db = duckdb.connect(str(DB_PATH), read_only=True, config={"enable_external_access": False, "lock_configuration": True})
except (FileNotFoundError, duckdb.Error) as e:
    raise SystemExit(f"No database or mappings found ({e}). Run `uv run --group build build_db.py` first.")

TABLES = {t["name"]: t for t in MAPPINGS["tables"]}
COLUMNS = {(t["name"], c["name"]): c for t in MAPPINGS["tables"] for c in t["columns"]}
STATES = {}  # 'ny' / 'new york' -> ('NY', 'New York')
for e in TABLES["counties"]["columns"][1]["enum_values"]:
    STATES[e["value"].lower()] = (e["value"], e["label"])
    STATES[e["label"].lower()] = (e["value"], e["label"])
NUMERIC_TYPES = {"BIGINT", "DOUBLE", "INTEGER", "HUGEINT", "DECIMAL", "FLOAT"}


def new_state() -> dict:
    return {
        "state_abbr": None,
        "state_name": None,
        "selected_fips": [],
        "selected_names": [],
        "excluded_fips": [],
        "last_sql": None,
        "last_ranking": None,
        "last_comparison": None,
        "last_places": None,
    }


def _err(kind: str, message: str, **extra) -> str:
    return json.dumps({"error": {"kind": kind, "message": message, **extra}})


def _query(sql: str, params: list | None = None) -> tuple[list[str], list[tuple]]:
    """Run a harness-written query (never model text) on its own cursor, with a timeout."""
    with db.cursor() as cur:
        timer = threading.Timer(SQL_TIMEOUT_S, cur.interrupt)
        timer.start()
        try:
            cur.execute(sql, params or [])
            rows = cur.fetchall()
        finally:
            timer.cancel()
        return [d[0] for d in cur.description or []], rows


# --- Resolving what the user / model named ---


def resolve_state(value, state: dict) -> tuple[str, str]:
    """'NY' / 'new york' / None (reuse the session's state) -> ('NY', 'New York')."""
    if value is None or str(value).strip() == "":
        if state["state_abbr"]:
            return state["state_abbr"], state["state_name"]
        raise ValueError("No state is stored in this session yet. Pass `state` (name or two-letter abbreviation).")
    key = str(value).strip().lower()
    if key not in STATES:
        raise ValueError(f"Unknown state '{value}'. Use a full name or USPS abbreviation; call get_enum_values('counties', 'state_abbr') for the list.")
    return STATES[key]


def resolve_metric(ref: str) -> tuple[str, str, dict] | tuple[None, None, str]:
    """'health_rankings.pct_adults_with_diabetes' -> (table, column, meta). Bare column names work when unique.

    Returns (None, None, reason) instead of raising so callers can keep going with the rest.
    """
    ref = str(ref).strip().lower()
    if "." in ref:
        table, column = ref.split(".", 1)
        meta = COLUMNS.get((table, column))
        if meta is None:
            hint = _closest_columns(column)
            return None, None, f"'{ref}' is not a column in this database." + (f" Did you mean: {', '.join(hint)}?" if hint else " Call read_mappings(search=...) to find it.")
    else:
        hits = [(t, c) for (t, c) in COLUMNS if c == ref and t not in {"health_rankings_state"}]
        if not hits:
            hint = _closest_columns(ref)
            return None, None, f"'{ref}' is not a column in this database." + (f" Did you mean: {', '.join(hint)}?" if hint else "")
        if len(hits) > 1:
            return None, None, f"'{ref}' exists in several tables ({', '.join(t for t, _ in hits)}); write it as table.column."
        table, column = hits[0]
        meta = COLUMNS[(table, column)]
    if table == "health_rankings_state":
        return None, None, f"'{ref}' is a state-level benchmark table; county rankings need a county table."
    if column == "fips" or meta["type"] not in NUMERIC_TYPES or meta.get("enum_values"):
        return None, None, f"'{ref}' is not a numeric measure (type {meta['type']}{', categorical' if meta.get('enum_values') else ''}); it cannot be ranked or compared."
    if meta.get("is_state_level"):
        alt = _glossary_alternative(f"{table}.{column}")
        return None, None, f"'{ref}' is a STATE-level measure: every county in a state has the same value, so it cannot rank counties within a state." + (f" {alt}" if alt else "")
    return table, column, meta


def _closest_columns(name: str) -> list[str]:
    from difflib import get_close_matches

    by_column = {}
    for t, c in COLUMNS:
        if t != "health_rankings_state":
            by_column.setdefault(c, f"{t}.{c}")
    return [by_column[c] for c in get_close_matches(name, list(by_column), n=3, cutoff=0.7)]


def _glossary_alternative(ref: str) -> str | None:
    for g in MAPPINGS["glossary"]:
        if ref in g.get("not", {}):
            return f"Use instead: {'; '.join(g['use'])}." if g["use"] else None
    return None


def _metric_info(table: str, column: str, meta: dict) -> dict:
    units = (meta.get("units") or "").lower()
    label = meta["description"].split(". Measure")[0].split(" [STATE")[0].strip()
    year = meta.get("year")
    if year and not label.endswith(year):
        label = f"{label} ({year})"
    if not units and re.search(r"income|earnings|wage|spending", label, re.IGNORECASE):
        units = "dollars"
    return {
        "label": label,
        "units": "percent" if units.startswith("percent") else ("dollars" if "dollar" in units else units),
        "year": meta.get("year"),
        "source": meta.get("source"),
    }


def _county_rows(state_abbr: str, metrics: list[tuple[str, str]]) -> list[dict]:
    """Every county in the state with the requested metrics, keyed by 'table.column'."""
    tables = sorted({t for t, _ in metrics})
    select = ["c.fips", "c.county_name", "c.population_2021 AS population", "c.metro23 AS metro"]
    select += [f'"{t}"."{c}" AS "{t}.{c}"' for t, c in metrics]
    joins = " ".join(f'LEFT JOIN "{t}" USING (fips)' for t in tables)
    cols, rows = _query(f"SELECT {', '.join(select)} FROM counties c {joins} WHERE c.state_abbr = ? AND c.state_name IS NOT NULL ORDER BY c.county_name", [state_abbr])
    return [dict(zip(cols, r)) for r in rows]


def _resolve_metrics(refs: list) -> tuple[list[tuple[str, str]], dict, list[dict]]:
    """Split metric references into usable (table, column) pairs and excluded ones with reasons."""
    if not isinstance(refs, list) or not refs:
        raise ValueError("Give at least one metric as 'table.column', e.g. ['food_access.pct_laccess_lowi19'].")
    usable, info, excluded = [], {}, []
    for ref in refs:
        table, column, meta = resolve_metric(ref)
        if table is None:
            excluded.append({"metric": ref, "reason": meta})
            continue
        key = f"{table}.{column}"
        if key not in info:
            usable.append((table, column))
            info[key] = _metric_info(table, column, meta)
    return usable, info, excluded


# --- Context tools ---


def read_mappings(state: dict, table: str | None = None, search: str | None = None) -> str:
    if table:
        t = TABLES.get(str(table).lower())
        if t is None:
            return _err("unknown_table", f"No table '{table}'. Tables: {', '.join(TABLES)}.")
        return json.dumps({
            "table": t["name"], "description": t["description"], "row_count": t["row_count"], "source": t["source"],
            "columns": [{k: c[k] for k in ("name", "type", "description", "units", "year", "is_state_level") if k in c} | {"is_enum": "enum_values" in c} for c in t["columns"]],
        })
    if search:
        terms = [w for w in re.split(r"[^a-z0-9]+", str(search).lower()) if len(w) > 1]
        if not terms:
            return _err("invalid_argument", "search needs at least one word.")
        hits = []
        for (tname, cname), c in COLUMNS.items():
            hay = " ".join([cname, c.get("description", ""), c.get("subcategory", "")]).lower()
            score = sum(1 for w in terms if w in hay)
            if score:
                hits.append((score, {"table": tname, "column": cname, "type": c["type"], "description": c.get("description"), "units": c.get("units"), "year": c.get("year"), "is_state_level": c.get("is_state_level", False), "is_enum": "enum_values" in c}))
        hits.sort(key=lambda h: (-h[0], h[1]["is_state_level"], h[1]["table"], h[1]["column"]))
        glossary = [g for g in MAPPINGS["glossary"] if any(w in (g["term"] + " " + g["definition"]).lower() for w in terms)]
        return json.dumps({
            "search": search, "n_matching_columns": len(hits), "columns": [h for _, h in hits[:MAX_SEARCH_HITS]],
            "truncated": len(hits) > MAX_SEARCH_HITS, "glossary": glossary,
            "tip": "Prefer columns with is_state_level=false for county questions and the latest year suffix for 'current' values.",
        })
    return json.dumps({
        "database": "USDA Food Environment Atlas 2025 + County Health Rankings 2023, one row per US county, joined on fips.",
        "notes": MAPPINGS["notes"],
        "tables": [{"name": t["name"], "description": t["description"], "row_count": t["row_count"], "n_columns": len(t["columns"])} for t in MAPPINGS["tables"]],
        "glossary": [{"term": g["term"], "use": g["use"], "avoid": list(g["not"])} for g in MAPPINGS["glossary"]],
        "next": "Call read_mappings(table=...) for one table's columns or read_mappings(search='obesity') to find columns by keyword.",
    })


def get_enum_values(state: dict, table: str, column: str) -> str:
    c = COLUMNS.get((str(table).lower(), str(column).lower()))
    if c is None:
        return _err("unknown_column", f"No column '{table}.{column}'. Call read_mappings(table='{table}').")
    if "enum_values" not in c:
        return _err("not_enum", f"'{table}.{column}' is a numeric measure, not a category. Filter it with comparisons, not equality to a label.")
    return json.dumps({"table": table, "column": column, "values": c["enum_values"], "note": "Match values exactly; string comparison is case-sensitive."})


# --- Tool: run_sql ---

_FORBIDDEN = re.compile(r"\b(attach|copy|export|import|install|load|pragma|set|reset|create|insert|update|delete|drop|alter|call|checkpoint|vacuum)\b", re.IGNORECASE)
_SENTINEL = re.compile(r"-\s?(9999|8888)\b")
# COALESCE(superc20, 0) turns a suppressed store count into "zero stores". Catch it before it runs.
_SUPPRESSED_COUNT = re.compile(r"\b(?:coalesce|ifnull)\s*\(\s*(?:\w+\.)?(?:groc|superc|convs|specs|ffr|fsr|recfac)(?:pth)?\d\d\s*,\s*0(?:\.0*)?\s*\)", re.IGNORECASE)


def run_sql(state: dict, query: str) -> str:
    q = str(query or "").strip().rstrip(";").strip()
    if not q:
        return _err("invalid_argument", "query is empty.")
    if ";" in q:
        return _err("invalid_argument", "One statement per call.")
    if not re.match(r"^(select|with)\b", q, re.IGNORECASE):
        return _err("read_only", "Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(q):
        return _err("read_only", "The database is read-only: no DDL, DML, PRAGMA, SET, ATTACH, COPY or extension commands.")
    if _SENTINEL.search(q):
        return _err("invalid_argument", "Missing values are NULL, not -9999 / -8888. Use IS NULL / IS NOT NULL.")
    if _SUPPRESSED_COUNT.search(q):
        return _err("invalid_argument", "Rejected: store/restaurant/recreation counts are NULL when SUPPRESSED (fewer than 3 establishments in 2020 data, zero in 2016 data), not when zero. Do not COALESCE them to 0. Count `col IS NULL` rows and report them as 'suppressed / fewer than 3'; the data cannot identify exactly-zero counties.")

    with db.cursor() as cur:
        timer = threading.Timer(SQL_TIMEOUT_S, cur.interrupt)
        timer.start()
        try:
            cur.execute(q)
            rows = cur.fetchmany(MAX_ROWS + 1)
            columns = [d[0] for d in cur.description or []]
        except duckdb.InterruptException:
            return _err("timeout", f"Query exceeded {SQL_TIMEOUT_S}s. Add filters or aggregate.")
        except duckdb.Error as e:
            # The model cannot see an exception. Return something it can reason about.
            msg = str(e).split("\n")[0]
            hint = ""
            if "Referenced column" in msg or "not found" in msg:
                hint = " Call read_mappings(search=...) to find the right column; Atlas names are lower-case codes like pct_laccess_lowi19."
            return _err("sql_error", msg + hint)
        finally:
            timer.cancel()
    state["last_sql"] = q
    out = {"columns": columns, "rows": [list(r) for r in rows[:MAX_ROWS]], "row_count": min(len(rows), MAX_ROWS), "truncated": len(rows) > MAX_ROWS}
    if len(rows) == 0:
        out["note"] = "No rows. Check filters: county_name has no 'County' suffix, state_abbr is upper-case, enum values are exact."
    return json.dumps(out, default=str)


# --- Tool: rank_counties ---


def rank_counties(state: dict, criteria: list, state_name: str | None = None, exclude_counties: list | None = None, require_counties: list | None = None, top_n: int = 10) -> str:
    try:
        abbr, full = resolve_state(state_name, state)
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("criteria must be a non-empty list of {metric, direction, threshold}.")
        usable, info, excluded_metrics = _resolve_metrics([c.get("metric") if isinstance(c, dict) else c for c in criteria])
        if not usable:
            return _err("no_usable_metric", "None of the requested metrics can rank counties.", excluded_metrics=excluded_metrics)
        kept = [c for c in criteria if isinstance(c, dict) and resolve_metric(c.get("metric"))[0]]
        for c in kept:
            t, col, _ = resolve_metric(c["metric"])
            c["metric"] = f"{t}.{col}"
        rows = _county_rows(abbr, usable)
        exclude_fips, bad_exclude = analysis.match_counties(exclude_counties or [], rows)
        require_fips, bad_require = analysis.match_counties(require_counties or [], rows)
        result = analysis.rank_counties(rows, kept, info, full, exclude_fips=exclude_fips, require_fips=require_fips, top_n=int(top_n))
    except ValueError as e:
        return _err("invalid_argument", str(e))

    for name in bad_exclude:
        result["warnings"].append(f"Could not match '{name}' to a county in {full}; it was NOT excluded.")
    for name in bad_require:
        result["warnings"].append(f"Could not match '{name}' to a county in {full}; it was NOT force-included.")
    result["excluded_metrics"] = excluded_metrics
    if excluded_metrics:
        result["warnings"].append("Some requested metrics were left out of the criteria; see excluded_metrics for why and what to use instead.")

    # Remember the facts the user established, for follow-ups.
    state["state_abbr"], state["state_name"] = abbr, full
    state["selected_fips"] = [s["fips"] for s in result["selected"]]
    state["selected_names"] = [s["county_name"] for s in result["selected"]]
    state["excluded_fips"] = exclude_fips
    state["last_ranking"] = {k: v for k, v in result.items() if k != "counties_missing_data"} | {"n_missing": len(result["counties_missing_data"])}
    result["session_note"] = f"Stored for follow-ups: state {abbr}, {len(state['selected_fips'])} selected counties, {len(exclude_fips)} exclusions. Later calls may omit state/counties to reuse them."
    return json.dumps(result, default=str)


# --- Tool: compare_groups ---

GROUP_KEYWORDS = ("selected", "rest_of_state", "metro", "nonmetro", "all_state")


def _group(spec, rows: list[dict], state: dict, other: set[str] | None = None) -> tuple[list[dict], str, list[str]]:
    """Resolve a group spec to (rows, label, unmatched_names)."""
    by_fips = {r["fips"]: r for r in rows}
    if spec is None:
        spec = "selected"
    if isinstance(spec, str):
        key = spec.strip().lower()
        if key == "selected":
            if not state["selected_fips"]:
                raise ValueError("No counties are selected yet: run rank_counties first or pass {\"counties\": [...]} explicitly.")
            picked = [by_fips[f] for f in state["selected_fips"] if f in by_fips]
            return picked, f"the {len(picked)} selected counties", []
        if key == "rest_of_state":
            if other is None:
                raise ValueError("'rest_of_state' is only valid for group_b.")
            picked = [r for r in rows if r["fips"] not in other]
            return picked, f"the other {len(picked)} counties in the state", []
        if key in {"metro", "nonmetro"}:
            want = 1 if key == "metro" else 0
            picked = [r for r in rows if r.get("metro") == want]
            return picked, f"{key} counties", []
        if key == "all_state":
            return list(rows), f"all {len(rows)} counties in the state", []
        raise ValueError(f"Unknown group '{spec}'. Use one of {GROUP_KEYWORDS} or {{\"counties\": [names or fips]}}.")
    if isinstance(spec, dict) and "counties" in spec:
        fips, bad = analysis.match_counties(spec["counties"], rows)
        picked = [by_fips[f] for f in fips]
        return picked, spec.get("label") or f"the {len(picked)} named counties", bad
    raise ValueError("group must be a keyword string or {\"counties\": [...]} .")


def compare_groups(state: dict, metrics: list, group_a=None, group_b="rest_of_state", state_name: str | None = None) -> str:
    try:
        abbr, full = resolve_state(state_name, state)
        usable, info, excluded_metrics = _resolve_metrics(metrics)
        if not usable:
            return _err("no_usable_metric", "None of the requested metrics can be compared across counties.", excluded_metrics=excluded_metrics)
        rows = _county_rows(abbr, usable)
        a_rows, label_a, bad_a = _group(group_a, rows, state)
        b_rows, label_b, bad_b = _group(group_b, rows, state, other={r["fips"] for r in a_rows})
        result = analysis.compare_groups(a_rows, b_rows, [f"{t}.{c}" for t, c in usable], info, label_a, label_b)
    except ValueError as e:
        return _err("invalid_argument", str(e))
    result["state"] = full
    result["excluded_metrics"] = excluded_metrics
    for name in bad_a + bad_b:
        result["warnings"].append(f"Could not match '{name}' to a county in {full}; it was left out of its group.")
    state["state_abbr"], state["state_name"] = abbr, full
    state["last_comparison"] = result
    return json.dumps(result, default=str)


# --- Tool: fetch_cdc_places (external) ---


def fetch_cdc_places(state: dict, measures: list | None = None, fips: list | None = None, counties: list | None = None, state_name: str | None = None, value_type: str = "AgeAdjPrv", benchmark_state: bool = True, scope: str = "auto", top_n: int = 10) -> str:
    measures = measures or ["OBESITY", "DIABETES"]
    whole_state = False
    try:
        abbr, full = (resolve_state(state_name, state) if (state_name or state["state_abbr"]) else (None, None))
        if scope not in {"auto", "selected", "whole_state"}:
            raise ValueError("scope must be 'auto', 'selected' or 'whole_state'.")
        if fips:
            targets = [str(f).zfill(5) for f in fips]
        elif counties:
            if not abbr:
                raise ValueError("Pass `state` so county names can be resolved.")
            all_rows = _county_rows(abbr, [])
            targets, bad = analysis.match_counties(counties, all_rows)
            if bad:
                raise ValueError(f"Could not match {bad} to counties in {full}.")
        elif scope != "whole_state" and state["selected_fips"]:
            targets = list(state["selected_fips"])
        elif abbr:
            whole_state = True  # every county in the state, ranked by the first measure
            targets = [r["fips"] for r in _county_rows(abbr, [])]
        else:
            raise ValueError("No counties given and no state known. Pass `state` (for every county in it), `fips`/`counties`, or run rank_counties first.")
        if not 1 <= int(top_n) <= 100:
            raise ValueError("top_n must be between 1 and 100.")
        if whole_state:
            data = None
            for i in range(0, len(targets), MAX_PLACES_FIPS):
                chunk = fetch_places(targets[i : i + MAX_PLACES_FIPS], measures, value_type)
                if data is None:
                    data = {**chunk, "counties": dict(chunk["counties"]), "missing_fips": list(chunk["missing_fips"])}
                else:
                    data["counties"].update(chunk["counties"])
                    data["missing_fips"] += chunk["missing_fips"]
        else:
            data = fetch_places(targets, measures, value_type)
    except (ValueError, PlacesError) as e:
        kind = getattr(e, "kind", "invalid_argument")
        return _err(kind, str(e))

    # Name the counties from our own table and, when the state is known, benchmark them against the whole state.
    names = {}
    if targets:
        cols, rows = _query(f"SELECT fips, county_name, state_abbr FROM counties WHERE fips IN ({','.join('?' * len(targets))})", targets)
        names = {r[0]: (r[1], r[2]) for r in rows}
    benchmark, bench_note = {}, None
    if benchmark_state and abbr:
        try:
            state_fips = [r["fips"] for r in _county_rows(abbr, [])]
            pooled: dict[str, list[float]] = {m: [] for m in data["measures"]}
            chunks = [data] if whole_state else [fetch_places(state_fips[i : i + MAX_PLACES_FIPS], list(data["measures"]), value_type) for i in range(0, len(state_fips), MAX_PLACES_FIPS)]
            for chunk in chunks:
                for entry in chunk["counties"].values():
                    for m, v in entry["measures"].items():
                        if v["value_pct"] is not None:
                            pooled[m].append(v["value_pct"])
            for m, vals in pooled.items():
                if vals:
                    benchmark[m] = {"state_median_pct": round(median(vals), 2), "state_min_pct": min(vals), "state_max_pct": max(vals), "n_counties": len(vals)}
            bench_note = f"Benchmark = all {len(state_fips)} counties in {full} from the same PLACES release."
        except PlacesError as e:
            bench_note = f"State benchmark unavailable ({e.kind}: {e})."

    out_counties = []
    for f in targets:
        entry = data["counties"].get(f)
        if entry is None:
            continue
        measures_out = {}
        for m, v in entry["measures"].items():
            item = dict(v)
            if m in benchmark and v["value_pct"] is not None:
                item["vs_state_median_points"] = round(v["value_pct"] - benchmark[m]["state_median_pct"], 2)
                item["above_state_median"] = v["value_pct"] > benchmark[m]["state_median_pct"]
            measures_out[m] = item
        out_counties.append({"fips": f, "county_name": names.get(f, (entry["county_name"], entry["state_abbr"]))[0], "state_abbr": names.get(f, (None, entry["state_abbr"]))[1], "population": entry["population"], "measures": measures_out})

    sentences, ranking_note = [], None
    first = list(data["measures"])[0]
    if whole_state:
        # The code ranks, so the model only has to copy the order.
        out_counties.sort(key=lambda c: (-(c["measures"].get(first, {}).get("value_pct") or -1), c["county_name"]))
        for i, c in enumerate(out_counties, start=1):
            c["rank"] = i
        shown = out_counties[: int(top_n)]
        ranking_note = f"All {len(out_counties)} counties in {full} were fetched and sorted by {PLACES_MEASURES[first]} (highest first); showing the top {len(shown)}. Ranks are 1 = highest."
        sentences.append(f"Top {len(shown)} {full} counties by {PLACES_MEASURES[first]} ({data['data_year']} data): " + "; ".join(f"{c['rank']}. {c['county_name']} {c['measures'][first]['value_pct']}%" for c in shown) + ".")
        out_counties = shown
    for m in data["measures"]:
        if m in benchmark and not whole_state:
            above = [c["county_name"] for c in out_counties if c["measures"].get(m, {}).get("above_state_median")]
            sentences.append(f"{PLACES_MEASURES[m]}: {len(above)} of {len(out_counties)} requested counties are above the {full} county median of {benchmark[m]['state_median_pct']}% ({data['data_year']} data)" + (f": {', '.join(above)}." if above else "."))
    result = {
        "scope": "whole_state" if whole_state else "requested_counties",
        "ranking_note": ranking_note,
        "source": data["source"],
        "dataset": data["dataset"],
        "data_year": data["data_year"],
        "value_type": data["value_type"],
        "measures": data["measures"],
        "counties": out_counties,
        "missing_fips": data["missing_fips"],
        "state_benchmark": benchmark,
        "benchmark_note": bench_note,
        "summary": sentences,
        "note": "These are CDC model-based survey estimates for a later year than the Atlas/Rankings tables. They are a cross-check and do NOT replace the local values used in rankings; the session's selected counties are unchanged.",
    }
    state["last_places"] = {k: result[k] for k in ("data_year", "measures", "counties", "state_benchmark")}
    if abbr and not state["state_abbr"]:
        state["state_abbr"], state["state_name"] = abbr, full
    return json.dumps(result, default=str)


# --- What the model sees ---

_STATE_PARAM = {"type": "string", "description": "State name or USPS abbreviation, e.g. 'New York' or 'NY'. Omit to reuse the state stored in this session."}
_METRIC_DESC = "Metric as 'table.column' from read_mappings, e.g. 'food_access.pct_laccess_lowi19' or 'health_rankings.pct_adults_with_diabetes'. State-level columns are rejected with an explanation."

TOOLS = [
    {"type": "function", "function": {
        "name": "read_mappings",
        "description": "Describe the database. With no arguments: the tables, the join key, data notes and a glossary of public-health terms mapped to the right columns. With `table`: every column of that table (name, type, description, units, year, whether it is a STATE-level measure, whether it is an enum). With `search`: columns and glossary entries matching keywords, across all tables. Call this before writing SQL or naming metrics; never guess column names.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string", "description": "A table name, e.g. 'health_rankings'."},
            "search": {"type": "string", "description": "Keywords, e.g. 'obesity', 'snap', 'grocery access'."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_enum_values",
        "description": "Exact valid values of a categorical column (state abbreviations and names, metro flag, SNAP policy flags). Call before filtering on one; equality is case-sensitive.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string", "description": "Table name, e.g. 'counties'."},
            "column": {"type": "string", "description": "Column name, e.g. 'state_abbr'."},
        }, "required": ["table", "column"]},
    }},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": f"Run ONE read-only DuckDB SELECT over the county database and return up to {MAX_ROWS} rows. Tables share the key `fips`; join with USING (fips) and filter counties by counties.state_abbr. Use for lookups, lists, aggregates and anything rank_counties/compare_groups do not cover. Missing values are NULL. Errors come back as {{\"error\": ...}}; read the message and fix the query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "A single DuckDB SELECT or WITH statement. No semicolons, no writes."},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "rank_counties",
        "description": "Find the counties in a state that meet EVERY criterion (e.g. limited food access above the state median AND diabetes above the state median) and order them by how extreme they are. Thresholds are computed by code over all counties in the state. Returns, per county, the values, within-state percentiles and a ready-made `reason` sentence; plus the thresholds used, counties with missing data, metrics that had to be left out and why, and warnings. Use `exclude_counties` / `require_counties` whenever the user names counties to leave out or to keep in; do not drop or add them any other way. Stores the selection in the session for follow-ups.",
        "parameters": {"type": "object", "properties": {
            "criteria": {"type": "array", "description": "One entry per condition.", "items": {"type": "object", "properties": {
                "metric": {"type": "string", "description": _METRIC_DESC},
                "direction": {"type": "string", "enum": list(analysis.DIRECTIONS), "description": "'high' = the county's value must be high (default); 'low' = must be low."},
                "threshold": {"type": "object", "description": "How high/low. Default {type: 'state_median'}. {type: 'percentile', value: 75} = top quarter (or bottom quarter for direction 'low'). {type: 'value', value: 10} = at or above/below 10 in the metric's units.", "properties": {
                    "type": {"type": "string", "enum": list(analysis.THRESHOLD_TYPES)},
                    "value": {"type": "number"},
                }},
            }, "required": ["metric"]}},
            "state_name": _STATE_PARAM,
            "exclude_counties": {"type": "array", "items": {"type": "string"}, "description": "Counties the user wants left out of the candidates (names or fips), e.g. ['Bronx', 'Kings', 'New York', 'Queens', 'Richmond'] for 'ignore NYC'. They still count toward the state benchmark; the result says so."},
            "require_counties": {"type": "array", "items": {"type": "string"}, "description": "Counties the user wants to see regardless (names or fips). They appear in the result with their values; if they do not qualify, the result explains which criterion they miss."},
            "top_n": {"type": "integer", "description": "How many qualifying counties to return (1-100, default 10)."},
        }, "required": ["criteria"]},
    }},
    {"type": "function", "function": {
        "name": "compare_groups",
        "description": "Compare two groups of counties in a state on one or more metrics: medians, means, median difference and a seeded 95% bootstrap interval, with a sentence per metric that says whether the gap is likely chance. Default groups: the counties selected by the last rank_counties vs the rest of the state. Also supports metro vs nonmetro and explicit county lists. Use when the user asks 'how do these counties compare', 'is the gap real', 'metro vs rural'.",
        "parameters": {"type": "object", "properties": {
            "metrics": {"type": "array", "items": {"type": "string"}, "description": "Metrics to compare, each as 'table.column'."},
            "group_a": {"description": "'selected' (default), 'metro', 'nonmetro', 'all_state', or {\"counties\": [names or fips], \"label\": \"...\"}.", "anyOf": [{"type": "string", "enum": list(GROUP_KEYWORDS)}, {"type": "object", "properties": {"counties": {"type": "array", "items": {"type": "string"}}, "label": {"type": "string"}}, "required": ["counties"]}]},
            "group_b": {"description": "'rest_of_state' (default), 'metro', 'nonmetro', 'all_state', or {\"counties\": [...]}. Must not overlap group_a.", "anyOf": [{"type": "string", "enum": list(GROUP_KEYWORDS)}, {"type": "object", "properties": {"counties": {"type": "array", "items": {"type": "string"}}, "label": {"type": "string"}}, "required": ["counties"]}]},
            "state_name": _STATE_PARAM,
        }, "required": ["metrics"]},
    }},
    {"type": "function", "function": {
        "name": "fetch_cdc_places",
        "description": "EXTERNAL DATA. Fetch the latest CDC PLACES county estimates (adult obesity, diagnosed diabetes, high blood pressure, SNAP receipt, food insecurity, no insurance, ...) for a set of counties and benchmark them against every county in the state from the same release. This is the only county-level source of adult OBESITY and SNAP receipt in this agent (the Atlas has them only at state level). Defaults to the counties selected by the last rank_counties; with scope='whole_state' (or no selection) it fetches EVERY county in the state and returns them ranked by the first measure, so use it directly for 'which counties have the highest obesity rate'. Returns values with confidence intervals, the data year, the state median per measure and which counties sit above it. Results are a cross-check; they do not replace the Atlas/Rankings values.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["auto", "selected", "whole_state"], "description": "auto (default): the session's selected counties if any, otherwise every county in the state. whole_state: every county in the state, ranked by the first measure. selected: only the selected counties."},
            "top_n": {"type": "integer", "description": "In whole_state scope, how many ranked counties to return (default 10, max 100)."},
            "measures": {"type": "array", "items": {"type": "string", "enum": list(PLACES_MEASURES)}, "description": "PLACES measure ids. Default ['OBESITY', 'DIABETES']. " + "; ".join(f"{k} = {v}" for k, v in PLACES_MEASURES.items())},
            "fips": {"type": "array", "items": {"type": "string"}, "description": f"County FIPS codes (max {MAX_PLACES_FIPS}). Omit to use `counties` or the session's selected counties."},
            "counties": {"type": "array", "items": {"type": "string"}, "description": "County names in the session's (or given) state, as an alternative to fips."},
            "state_name": _STATE_PARAM,
            "value_type": {"type": "string", "enum": list(VALUE_TYPES), "description": "AgeAdjPrv (age-adjusted, default; comparable across counties) or CrdPrv (crude)."},
            "benchmark_state": {"type": "boolean", "description": "Also fetch every county in the state to compute the state median (default true)."},
        }, "required": []},
    }},
]

TOOL_MAP = {
    "read_mappings": read_mappings,
    "get_enum_values": get_enum_values,
    "run_sql": run_sql,
    "rank_counties": rank_counties,
    "compare_groups": compare_groups,
    "fetch_cdc_places": fetch_cdc_places,
}


def run_tool(name: str, args: dict, state: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return _err("unknown_tool", f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}")
    try:
        return TOOL_MAP[name](state, **args)
    except TypeError as e:
        return _err("invalid_argument", f"Bad arguments for {name}: {e}")
    except Exception as e:  # last line of defense: the model must see something it can reason about
        return _err("internal", f"{name} failed: {type(e).__name__}: {str(e)[:200]}. Check the inputs and try again.")
