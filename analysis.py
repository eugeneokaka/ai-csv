"""Pure, factual dataset analysis used to ground the LLM.

No file I/O and no environment access — every function takes a pandas
DataFrame and returns plain Python structures. `worker.build_prompt()` renders
these facts into the prompt so the model interprets real numbers instead of
inventing them. The same functions are re-exported by `helper.py` for use in
AI-generated code.

Design rules:
- Only pandas is imported (keeps requirements.txt unchanged).
- Statistics are computed, never guessed.
- Inferred values (e.g. date frequency) are explicitly labelled.
- Functions degrade to an empty result rather than raising.
"""

from __future__ import annotations

import pandas as pd

SAMPLE_CAP = 50_000
CORR_THRESHOLD = 0.5
CORR_MAX_PAIRS = 5
CORR_MAX_COLS = 30
OUTLIER_K = 1.5
DATE_PARSE_MIN_RATIO = 0.8
STRENGTH_WEAK = 0.3
STRENGTH_STRONG = 0.7

MAX_COLUMN_LINES = 40
MAX_TREND_LINES = 20
MAX_OUTLIER_LINES = 20


# --- small utilities ---

def _numeric_cols(df) -> list:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def _round(value, ndigits: int = 4):
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _strength(abs_r: float) -> str:
    if abs_r < STRENGTH_WEAK:
        return "weak"
    if abs_r < STRENGTH_STRONG:
        return "moderate"
    return "strong"


def _fmt(value) -> str:
    return "n/a" if value is None else str(value)


# --- 1. dataset ---

def get_dataset_info(df) -> dict:
    """Rows, columns, column names and dtypes."""
    return {
        "rows": int(len(df)),
        "columns": int(df.shape[1]),
        "column_names": [str(c) for c in df.columns],
        "column_types": {str(c): str(dtype) for c, dtype in df.dtypes.items()},
    }


# --- 2. per-column info ---

def get_column_info(df) -> dict:
    """Per column: dtype, missing count, unique count, and numeric extent."""
    info = {}
    for col in df.columns:
        series = df[col]
        entry = {
            "dtype": str(series.dtype),
            "missing": int(series.isna().sum()),
            "unique": int(series.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(series):
            entry.update({
                "min": _round(series.min()),
                "max": _round(series.max()),
                "mean": _round(series.mean()),
                "median": _round(series.median()),
            })
        info[str(col)] = entry
    return info


# --- 3. dates ---

def _detect_date_columns(df) -> dict:
    """Return {column: parsed Series} for object columns that look like dates."""
    detected = {}
    for col in df.columns:
        series = df[col]
        if not (series.dtype == object or pd.api.types.is_string_dtype(series)):
            continue
        non_null = series.dropna()
        if len(non_null) == 0:
            continue
        # Skip plain 4-digit year columns (e.g. "2020"), which parse as Jan 1.
        sample = non_null.astype(str).head(20)
        if sample.str.fullmatch(r"\d{4}").all():
            continue
        try:
            parsed = pd.to_datetime(non_null, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            parsed = pd.to_datetime(non_null, errors="coerce")
        if parsed.notna().mean() >= DATE_PARSE_MIN_RATIO:
            detected[str(col)] = parsed
    return detected


def _infer_frequency(gap) -> str | None:
    """Infer a coarse granularity from the median gap between observations."""
    if gap is None or pd.isna(gap):
        return None
    seconds = gap.total_seconds()
    day = 86400.0
    if seconds < 3600:
        return "hourly or finer"
    if seconds < day:
        return "sub-daily"
    days = seconds / day
    if days <= 1.5:
        return "daily"
    if days <= 10:
        return "weekly"
    if days <= 45:
        return "monthly"
    if days <= 130:
        return "quarterly"
    if days <= 550:
        return "yearly"
    return "multi-year"


def get_date_info(df) -> dict:
    """Detected date columns with range, observation count and inferred frequency."""
    result = {}
    for col, parsed in _detect_date_columns(df).items():
        parsed = parsed.dropna()
        if parsed.empty:
            continue
        ordered = parsed.sort_values()
        n = int(len(ordered))
        freq = None
        if n >= 3:
            freq = _infer_frequency(ordered.diff().dropna().median())
        result[col] = {
            "start": ordered.iloc[0].isoformat(),
            "end": ordered.iloc[-1].isoformat(),
            "observations": n,
            "inferred_frequency": freq or "unknown",
        }
    return result


# --- 4. missing values ---

def get_missing_values(df) -> dict:
    total = len(df)
    out = {}
    for col in df.columns:
        count = int(df[col].isna().sum())
        out[str(col)] = {
            "count": count,
            "pct": round(count / total * 100, 2) if total else 0.0,
        }
    return out


# --- 5. duplicates ---

def get_duplicate_info(df) -> dict:
    total = len(df)
    dup = int(df.duplicated().sum())
    return {
        "duplicate_rows": dup,
        "duplicate_pct": round(dup / total * 100, 2) if total else 0.0,
    }


# --- 6. summary statistics ---

def get_summary_statistics(df) -> dict:
    cols = _numeric_cols(df)
    if not cols:
        return {}
    desc = df[cols].describe().to_dict()
    return {
        str(col): {stat: _round(val) for stat, val in stats.items()}
        for col, stats in desc.items()
    }


# --- 7. correlations ---

def calculate_correlations(
    df,
    threshold: float = CORR_THRESHOLD,
    max_pairs: int = CORR_MAX_PAIRS,
    max_cols: int = CORR_MAX_COLS,
) -> list:
    """Pearson correlations for numeric pairs with |r| >= threshold (top pairs)."""
    cols = _numeric_cols(df)[:max_cols]
    if len(cols) < 2:
        return []
    corr = df[cols].corr(method="pearson")
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            r = corr.iloc[i, j]
            if pd.isna(r):
                continue
            abs_r = abs(float(r))
            if abs_r < threshold:
                continue
            pairs.append({
                "a": str(cols[i]),
                "b": str(cols[j]),
                "r": _round(r),
                "abs_r": _round(abs_r),
                "direction": "positive" if r > 0 else "negative",
                "strength": _strength(abs_r),
            })
    pairs.sort(key=lambda p: p["abs_r"] or 0, reverse=True)
    return pairs[:max_pairs]


# --- 8. trends over time ---

def _time_axis(df, date_col: str | None = None):
    """Return (column_name, integer time axis) for a detected date column."""
    detected = _detect_date_columns(df)
    if not detected:
        return None
    if date_col is not None and str(date_col) in detected:
        name = str(date_col)
    else:
        name = next(iter(detected))
    axis = detected[name].dropna().astype("int64")
    return name, axis


def analyze_trends(df, date_col: str | None = None) -> dict:
    """Direction and correlation with time for each numeric column."""
    axis_info = _time_axis(df, date_col)
    if not axis_info:
        return {}
    name, axis = axis_info
    series = {}
    for col in _numeric_cols(df):
        pair = pd.DataFrame({"t": axis, "v": df[col]}).dropna()
        if len(pair) < 3:
            continue
        r = pair["t"].corr(pair["v"])
        if pd.isna(r):
            continue
        r = float(r)
        series[col] = {
            "corr_with_time": _round(r),
            "direction": "increasing" if r > 0 else ("decreasing" if r < 0 else "flat"),
            "strength": _strength(abs(r)),
        }
    if not series:
        return {}
    return {"date_column": name, "series": series}


# --- 9. time confounding ---

def detect_time_confounding(
    correlations: list,
    trends: dict,
    threshold: float = CORR_THRESHOLD,
) -> list:
    """Flag strong correlations where both variables also track time."""
    series = (trends or {}).get("series", {})
    flags = []
    for pair in correlations or []:
        a_time = series.get(pair["a"], {}).get("corr_with_time")
        b_time = series.get(pair["b"], {}).get("corr_with_time")
        if a_time is None or b_time is None:
            continue
        if abs(a_time) >= threshold and abs(b_time) >= threshold:
            flags.append({
                "a": pair["a"],
                "b": pair["b"],
                "r": pair["r"],
                "a_corr_with_time": a_time,
                "b_corr_with_time": b_time,
                "note": (
                    "both variables trend over time — the correlation may be "
                    "confounded by time, not causal"
                ),
            })
    return flags


# --- 10. outliers ---

def detect_outliers(df, k: float = OUTLIER_K) -> dict:
    """IQR-based outlier counts and bounds per numeric column (no row dumps)."""
    out = {}
    for col in _numeric_cols(df):
        s = df[col].dropna()
        if len(s) < 4:
            continue
        q1 = s.quantile(0.25)
        q3 = s.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0:
            continue
        lower = q1 - k * iqr
        upper = q3 + k * iqr
        count = int(((s < lower) | (s > upper)).sum())
        out[col] = {
            "count": count,
            "pct": round(count / len(s) * 100, 2),
            "lower_bound": _round(lower),
            "upper_bound": _round(upper),
        }
    return out


# --- 11. derived metric candidates ---

_METRIC_RULES = [
    ("return_rate", ("return", "returned", "refund"),
     ("order", "orders", "sale", "sales", "units", "purchase", "purchases")),
    ("conversion_rate", ("click", "clicks", "conversion", "conversions", "lead", "leads"),
     ("impression", "impressions", "visit", "visits", "view", "views")),
    ("click_through_rate", ("click", "clicks"), ("impression", "impressions")),
    ("profit_margin", ("profit", "profit_amount"),
     ("revenue", "sales_amount", "sales", "turnover")),
    ("cost_ratio", ("cost", "costs", "expense", "expenses"),
     ("revenue", "sales", "turnover")),
    ("average_order_value", ("revenue", "sales_amount", "sales", "turnover"),
     ("order", "orders", "units", "transactions")),
    ("error_rate", ("error", "errors", "failure", "failures", "defect", "defects"),
     ("total", "count", "requests", "attempts")),
]


def suggest_derived_metrics(df) -> list:
    """Name-based candidate ratios/metrics — always verify against the data."""
    lower = {str(c).lower(): str(c) for c in df.columns}
    suggestions = []
    for name, num_tokens, den_tokens in _METRIC_RULES:
        num = next((lower[c] for c in lower if any(t in c for t in num_tokens)), None)
        den = next(
            (lower[c] for c in lower
             if any(t in c for t in den_tokens) and lower[c] != num),
            None,
        )
        if num and den:
            suggestions.append({
                "name": name,
                "formula": f"{num} / {den}",
                "columns": [num, den],
                "note": "candidate only — verify these columns are appropriate",
            })
    seen = set()
    deduped = []
    for s in suggestions:
        if s["name"] in seen:
            continue
        seen.add(s["name"])
        deduped.append(s)
    return deduped[:5]


# --- orchestration ---

def build_facts(df) -> dict:
    """Compute every fact, each independently guarded so one failure degrades only."""
    sample_note = None
    total = len(df)
    if total > SAMPLE_CAP:
        df = df.sample(SAMPLE_CAP, random_state=0)
        sample_note = f"based on {SAMPLE_CAP} of {total} rows (sampled)"

    facts = {"sampled": sample_note}

    def safe(key, fn):
        try:
            facts[key] = fn()
        except Exception:
            facts[key] = None

    safe("dataset", lambda: get_dataset_info(df))
    safe("columns", lambda: get_column_info(df))
    safe("dates", lambda: get_date_info(df))
    safe("missing", lambda: get_missing_values(df))
    safe("duplicates", lambda: get_duplicate_info(df))
    safe("summary", lambda: get_summary_statistics(df))
    safe("correlations", lambda: calculate_correlations(df))
    safe("trends", lambda: analyze_trends(df))
    safe("derived_metrics", lambda: suggest_derived_metrics(df))
    safe("outliers", lambda: detect_outliers(df))
    safe("time_confounding", lambda: detect_time_confounding(
        facts.get("correlations") or [], facts.get("trends") or {}
    ))
    return facts


def format_facts(facts: dict) -> str:
    """Render `build_facts()` output as a compact, LLM-facing text block."""
    if not facts:
        return ""

    lines = []

    ds = facts.get("dataset")
    if ds:
        lines.append(f"Dataset: {ds['rows']} rows x {ds['columns']} columns")
        lines.append(f"Columns: {', '.join(ds['column_names'])}")
    if facts.get("sampled"):
        lines.append(f"NOTE: {facts['sampled']} — do NOT report these as full-dataset figures.")

    columns = facts.get("columns")
    if columns:
        lines.append("")
        lines.append("Column overview:")
        items = list(columns.items())
        for name, info in items[:MAX_COLUMN_LINES]:
            extra = ""
            if "min" in info:
                extra = (
                    f", min={_fmt(info['min'])}, max={_fmt(info['max'])}"
                    f", mean={_fmt(info['mean'])}, median={_fmt(info['median'])}"
                )
            lines.append(
                f"  - {name} ({info['dtype']}): missing {info['missing']}, "
                f"unique {info['unique']}{extra}"
            )
        if len(items) > MAX_COLUMN_LINES:
            lines.append(f"  ... {len(items) - MAX_COLUMN_LINES} more column(s) omitted")

    dates = facts.get("dates")
    if dates:
        lines.append("")
        lines.append("Dates:")
        for name, info in dates.items():
            lines.append(
                f"  - {name}: {info['start']} to {info['end']}, "
                f"{info['observations']} observations, "
                f"frequency={info['inferred_frequency']} (inferred)"
            )

    missing = facts.get("missing")
    if missing:
        with_missing = {k: v for k, v in missing.items() if v["count"] > 0}
        lines.append("")
        if with_missing:
            lines.append("Missing values:")
            for name, info in with_missing.items():
                lines.append(f"  - {name}: {info['count']} ({info['pct']}%)")
        else:
            lines.append("Missing values: none")

    duplicates = facts.get("duplicates")
    if duplicates is not None:
        lines.append(
            f"Duplicates: {duplicates['duplicate_rows']} rows "
            f"({duplicates['duplicate_pct']}%)"
        )

    summary = facts.get("summary")
    if summary:
        lines.append("")
        lines.append("Summary statistics (numeric):")
        for name, stats in summary.items():
            parts = ", ".join(f"{stat}={_fmt(val)}" for stat, val in stats.items())
            lines.append(f"  - {name}: {parts}")

    correlations = facts.get("correlations")
    if correlations:
        lines.append("")
        lines.append("Correlations (Pearson, |r| >= 0.5):")
        for p in correlations:
            lines.append(
                f"  - {p['a']} ~ {p['b']}: r={p['r']} "
                f"({p['strength']}, {p['direction']})"
            )

    trends = facts.get("trends")
    if trends:
        lines.append("")
        lines.append(f"Trends vs time ({trends['date_column']}):")
        items = list(trends["series"].items())
        for name, info in items[:MAX_TREND_LINES]:
            lines.append(
                f"  - {name}: {info['direction']} "
                f"(r with time={info['corr_with_time']}, {info['strength']})"
            )
        if len(items) > MAX_TREND_LINES:
            lines.append(f"  ... {len(items) - MAX_TREND_LINES} more column(s) omitted")

    confounding = facts.get("time_confounding")
    if confounding:
        lines.append("")
        lines.append("Time-confounding flags (correlation may NOT be causal):")
        for f in confounding:
            lines.append(
                f"  - {f['a']} ~ {f['b']}: r={f['r']}, "
                f"but {f['a']} tracks time (r={f['a_corr_with_time']}) and "
                f"{f['b']} tracks time (r={f['b_corr_with_time']})"
            )

    outliers = facts.get("outliers")
    if outliers:
        lines.append("")
        lines.append("Outliers (IQR x1.5):")
        items = list(outliers.items())
        for name, info in items[:MAX_OUTLIER_LINES]:
            lines.append(
                f"  - {name}: {info['count']} ({info['pct']}%) outside "
                f"[{info['lower_bound']}, {info['upper_bound']}]"
            )
        if len(items) > MAX_OUTLIER_LINES:
            lines.append(f"  ... {len(items) - MAX_OUTLIER_LINES} more column(s) omitted")

    metrics = facts.get("derived_metrics")
    if metrics:
        lines.append("")
        lines.append("Suggested derived metrics (candidates — verify):")
        for m in metrics:
            lines.append(f"  - {m['name']} = {m['formula']}")

    return "\n".join(lines)


def build_facts_text(df) -> str:
    """Convenience: build and render facts in one call."""
    return format_facts(build_facts(df))


if __name__ == "__main__":
    import sys
    from pathlib import Path

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "test.csv"
    print(f"# analysis smoke test: {path}")
    print(build_facts_text(pd.read_csv(path)))
