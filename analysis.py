"""Deterministic county analysis. No model, no database: rows in, numbers and sentences out.

Every public function takes plain lists of dicts (one per county) and returns a plain dict.
Invalid input raises ValueError with a message the model can act on; the tool layer
(tools.py) turns that into JSON. Sentences that explain a result ("why is this county
first?") are written here, by code, so the model only has to repeat them.
"""

import math
import random
import re
from difflib import get_close_matches
from statistics import median

DIRECTIONS = ("high", "low")
THRESHOLD_TYPES = ("state_median", "percentile", "value")
BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 4570  # fixed: the same question must give the same interval
MIN_GROUP_SIZE = 3


# --- Names ---


def normalize_name(name: str) -> str:
    """'St. Lawrence County' -> 'st lawrence'; 'Bronx county, NY' -> 'bronx'."""
    s = str(name).lower().strip()
    s = re.sub(r",.*$", "", s)
    s = re.sub(r"\b(county|parish|borough|census area|city and borough|municipality)\b", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_counties(names: list, rows: list[dict]) -> tuple[list[str], list[str]]:
    """Resolve user-written county names (or FIPS codes) to the fips in `rows`.

    Returns (matched_fips, unmatched_names). Forgives 'County' suffixes and small typos.
    """
    by_norm = {normalize_name(r["county_name"]): r["fips"] for r in rows}
    by_fips = {r["fips"]: r["fips"] for r in rows}
    matched, unmatched = [], []
    for raw in names or []:
        key = str(raw).strip()
        if key in by_fips:
            matched.append(key)
            continue
        norm = normalize_name(key)
        if norm in by_norm:
            matched.append(by_norm[norm])
            continue
        close = get_close_matches(norm, list(by_norm), n=1, cutoff=0.85)
        if close:
            matched.append(by_norm[close[0]])
        else:
            unmatched.append(str(raw))
    return list(dict.fromkeys(matched)), unmatched


# --- Small numeric helpers ---


def _present(values):
    return [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]


def percentile_of(value: float, population: list[float], direction: str) -> float:
    """Share of the population this value is at or beyond, in the chosen direction (0-100)."""
    if not population:
        return float("nan")
    if direction == "high":
        beyond = sum(1 for p in population if p <= value)
    else:
        beyond = sum(1 for p in population if p >= value)
    return 100.0 * beyond / len(population)


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def fmt(value, units: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if units == "percent":
        return f"{value:.1f}%"
    if units == "dollars":
        return f"${value:,.0f}"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# --- Tool 3: rank counties ---


def validate_criteria(criteria: list, metrics_info: dict) -> list[dict]:
    """Normalise criteria. metrics_info: metric -> {"label", "units"} for metrics that exist."""
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("criteria must be a non-empty list of {metric, direction, threshold}.")
    out = []
    for c in criteria:
        if not isinstance(c, dict) or "metric" not in c:
            raise ValueError("Each criterion needs at least a 'metric' (table.column).")
        direction = str(c.get("direction", "high")).lower()
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}, got '{direction}'.")
        threshold = c.get("threshold") or {"type": "state_median"}
        if isinstance(threshold, (int, float)):
            threshold = {"type": "value", "value": float(threshold)}
        ttype = str(threshold.get("type", "state_median"))
        if ttype not in THRESHOLD_TYPES:
            raise ValueError(f"threshold.type must be one of {THRESHOLD_TYPES}, got '{ttype}'.")
        if ttype in {"percentile", "value"} and threshold.get("value") is None:
            raise ValueError(f"threshold.type '{ttype}' needs a numeric threshold.value.")
        if ttype == "percentile" and not 0 < float(threshold["value"]) < 100:
            raise ValueError("threshold.value for 'percentile' must be between 0 and 100 (e.g. 75 = top quarter).")
        out.append({"metric": c["metric"], "direction": direction, "threshold": {"type": ttype, "value": threshold.get("value")}})
    return out


def rank_counties(
    rows: list[dict],
    criteria: list[dict],
    metrics_info: dict,
    state_label: str,
    exclude_fips: list[str] | None = None,
    require_fips: list[str] | None = None,
    top_n: int = 10,
) -> dict:
    """Find the counties that meet every criterion and order them by how extreme they are.

    rows: every county in the state, {"fips", "county_name", "population", <metric>: value}.
    metrics_info: metric -> {"label": str, "units": str} for the metrics present in rows.
    Thresholds are computed over ALL counties in the state (the benchmark), then the
    excluded counties are dropped from the candidates, never from the benchmark.
    """
    if not rows:
        raise ValueError(f"No counties found for {state_label}.")
    criteria = validate_criteria(criteria, metrics_info)
    exclude_fips, require_fips = set(exclude_fips or []), list(require_fips or [])
    if not 1 <= int(top_n) <= 100:
        raise ValueError("top_n must be between 1 and 100.")

    # Thresholds, over the whole state.
    resolved, warnings = [], []
    for c in criteria:
        m = c["metric"]
        values = sorted(_present([r.get(m) for r in rows]))
        if len(values) < MIN_GROUP_SIZE:
            raise ValueError(f"Metric '{m}' has values for only {len(values)} counties in {state_label}; cannot rank on it.")
        t = c["threshold"]
        if t["type"] == "state_median":
            cutoff, rule = median(values), f"{'above' if c['direction'] == 'high' else 'below'} the {state_label} county median"
        elif t["type"] == "percentile":
            q = float(t["value"]) / 100
            cutoff = quantile(values, q if c["direction"] == "high" else 1 - q)
            rule = f"in the {'top' if c['direction'] == 'high' else 'bottom'} {100 - float(t['value']):g}% of {state_label} counties"
        else:
            cutoff, rule = float(t["value"]), f"{'at or above' if c['direction'] == 'high' else 'at or below'} {fmt(float(t['value']), metrics_info[m]['units'])}"
        resolved.append({**c, "label": metrics_info[m]["label"], "units": metrics_info[m]["units"], "cutoff": round(cutoff, 4), "rule": rule, "state_median": round(median(values), 4), "n_with_data": len(values), "_values": values})

    def meets(value, c) -> bool | None:
        if value is None:
            return None
        if c["threshold"]["type"] == "state_median":
            return value > c["cutoff"] if c["direction"] == "high" else value < c["cutoff"]
        return value >= c["cutoff"] if c["direction"] == "high" else value <= c["cutoff"]

    # Score every county.
    scored, missing = [], []
    for r in rows:
        flags = {c["metric"]: meets(r.get(c["metric"]), c) for c in resolved}
        absent = [c["label"] for c in resolved if flags[c["metric"]] is None]
        if absent:
            missing.append({"fips": r["fips"], "county_name": r["county_name"], "missing": absent})
        pct = [percentile_of(r[c["metric"]], c["_values"], c["direction"]) for c in resolved if flags[c["metric"]] is not None]
        scored.append({
            "fips": r["fips"],
            "county_name": r["county_name"],
            "population": r.get("population"),
            "values": {c["metric"]: r.get(c["metric"]) for c in resolved},
            "percentiles": {c["metric"]: round(percentile_of(r[c["metric"]], c["_values"], c["direction"]), 1) for c in resolved if flags[c["metric"]] is not None},
            "meets": flags,
            "n_met": sum(1 for f in flags.values() if f),
            "score": round(sum(pct) / len(pct), 2) if pct and len(pct) == len(resolved) else None,
        })

    qualifying = [s for s in scored if all(s["meets"].values()) and s["fips"] not in exclude_fips]
    qualifying.sort(key=lambda s: (-s["score"], s["county_name"]))
    by_fips = {s["fips"]: s for s in scored}

    # Required counties are shown whether or not they qualify, and say so.
    required_not_qualifying = []
    for f in require_fips:
        s = by_fips.get(f)
        if s is None:
            continue
        if s not in qualifying:
            failed = [resolved_c["label"] for resolved_c in resolved if not s["meets"][resolved_c["metric"]]]
            why = f"{s['county_name']} was requested but does not meet: {', '.join(failed) or 'data missing for ' + ', '.join(m['missing'] for m in missing if m['fips'] == f)}."
            if f in exclude_fips:
                why += " It is also in exclude_counties; require wins."
            required_not_qualifying.append({**s, "why": why})

    selected = qualifying[: int(top_n)]
    for i, s in enumerate(selected, start=1):
        parts = []
        for c in resolved:
            v, u = s["values"][c["metric"]], c["units"]
            ratio = (v / c["state_median"]) if c["state_median"] else None
            how = f"{ratio:.1f}x the median" if ratio and ratio >= 1.5 else f"vs median {fmt(c['state_median'], u)}"
            parts.append(f"{c['label']} {fmt(v, u)} ({how}; {'higher' if c['direction'] == 'high' else 'lower'} than {s['percentiles'][c['metric']]:.0f}% of {state_label} counties)")
        s["rank"] = i
        s["reason"] = f"{s['county_name']} ranks {ordinal(i)} of {len(qualifying)} qualifying counties because it meets every criterion and its average percentile is {s['score']:.0f}: " + "; ".join(parts) + "."
    for s in required_not_qualifying:
        s["rank"] = None
        s["reason"] = s.pop("why")

    if exclude_fips:
        names = [by_fips[f]["county_name"] for f in exclude_fips if f in by_fips]
        warnings.append(f"Excluded from the candidates at the user's request (still part of the state benchmark): {', '.join(sorted(names))}.")
    if missing:
        warnings.append(f"{len(missing)} counties lack data for at least one metric and cannot qualify; see counties_missing_data.")
    if len(qualifying) > len(selected):
        warnings.append(f"{len(qualifying)} counties qualify; showing the top {len(selected)} by average percentile. Raise top_n to see more.")
    if not qualifying:
        warnings.append("No county meets every criterion. Loosen a threshold or drop a criterion.")

    for c in resolved:
        c.pop("_values")
    return {
        "state": state_label,
        "n_counties_in_state": len(rows),
        "criteria": resolved,
        "ranking_rule": "A county qualifies when it meets every criterion. Qualifying counties are ordered by the average of their within-state percentiles across the criteria (higher = more extreme); ties break alphabetically.",
        "n_qualifying": len(qualifying),
        "selected": selected,
        "required_not_qualifying": required_not_qualifying,
        "counties_missing_data": missing,
        "warnings": warnings,
        "caveats": [
            "Cross-sectional county data: it shows where measures co-occur, not that one causes another.",
            "Measures come from different years (see criteria[].label); treat small differences as noise.",
        ],
    }


# --- Tool 4: compare groups ---


def bootstrap_median_diff(a: list[float], b: list[float], reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    rng = random.Random(seed)
    diffs = []
    for _ in range(reps):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(median(sa) - median(sb))
    diffs.sort()
    return quantile(diffs, 0.025), quantile(diffs, 0.975)


def compare_groups(group_a: list[dict], group_b: list[dict], metrics: list[str], metrics_info: dict, label_a: str, label_b: str) -> dict:
    """Median (and mean) of each metric in two groups of counties, with a bootstrap CI on the median difference."""
    if not metrics:
        raise ValueError("metrics must be a non-empty list of table.column names.")
    if len(group_a) < MIN_GROUP_SIZE or len(group_b) < MIN_GROUP_SIZE:
        raise ValueError(f"Each group needs at least {MIN_GROUP_SIZE} counties; got {len(group_a)} ({label_a}) and {len(group_b)} ({label_b}).")
    overlap = {r["fips"] for r in group_a} & {r["fips"] for r in group_b}
    if overlap:
        raise ValueError(f"The two groups share {len(overlap)} counties; groups must be disjoint.")

    results, warnings = [], []
    for m in metrics:
        info = metrics_info[m]
        a, b = _present([r.get(m) for r in group_a]), _present([r.get(m) for r in group_b])
        dropped = (len(group_a) - len(a)) + (len(group_b) - len(b))
        if len(a) < MIN_GROUP_SIZE or len(b) < MIN_GROUP_SIZE:
            results.append({"metric": m, "label": info["label"], "units": info["units"], "error": f"Too few counties with data ({len(a)} vs {len(b)})."})
            continue
        med_a, med_b = median(a), median(b)
        lo, hi = bootstrap_median_diff(a, b)
        diff = med_a - med_b
        excludes_zero = lo > 0 or hi < 0
        u = info["units"]
        unit_word = " percentage points" if u == "percent" else ""
        sentence = (
            f"{info['label']}: median {fmt(med_a, u)} in {label_a} (n={len(a)}) vs {fmt(med_b, u)} in {label_b} (n={len(b)}), "
            f"a difference of {diff:+.2f}{unit_word}; the 95% bootstrap interval {lo:+.2f} to {hi:+.2f} "
            f"{'excludes zero, so the gap is unlikely to be chance' if excludes_zero else 'includes zero, so the gap could be chance'}."
        )
        if dropped:
            sentence += f" {dropped} counties without data were left out of this metric."
        results.append({
            "metric": m, "label": info["label"], "units": u, "year": info.get("year"),
            "group_a": {"label": label_a, "n": len(a), "median": round(med_a, 3), "mean": round(sum(a) / len(a), 3), "min": min(a), "max": max(a)},
            "group_b": {"label": label_b, "n": len(b), "median": round(med_b, 3), "mean": round(sum(b) / len(b), 3), "min": min(b), "max": max(b)},
            "median_difference": round(diff, 3),
            "bootstrap_95ci": [round(lo, 3), round(hi, 3)],
            "interval_excludes_zero": excludes_zero,
            "n_dropped_missing": dropped,
            "sentence": sentence,
        })
    if any("error" in r for r in results):
        warnings.append("Some metrics could not be compared; see results[].error.")
    return {
        "group_a": {"label": label_a, "n_counties": len(group_a)},
        "group_b": {"label": label_b, "n_counties": len(group_b)},
        "method": f"Medians compared; 95% interval from {BOOTSTRAP_REPS} seeded bootstrap resamples of the median difference (seed {BOOTSTRAP_SEED}, reproducible).",
        "results": results,
        "warnings": warnings,
        "caveats": [
            "This is a cross-sectional association between county groups, not a causal effect of food access on health.",
            "County medians hide within-county variation; a county is not a person.",
        ],
    }
