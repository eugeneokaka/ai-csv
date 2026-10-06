"""Functions available to AI-generated code running in working_dir/app.py."""

import os
from pathlib import Path

import analysis

WORKDIR = Path(__file__).resolve().parent / "working_dir"

# chat.run_code() injects CHAT_ID into the subprocess env. It scopes all
# reads/writes to this chat's own folders so chats never see each other.
CHAT_ID = os.environ.get("CHAT_ID", "")
UPLOADS = WORKDIR / "uploads" / CHAT_ID
OUTPUT = WORKDIR / "output" / CHAT_ID
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)


def _pd():
    import pandas as pd
    return pd


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


# Data files the helpers can read (CSV + Excel).
DATA_SUFFIXES = (".csv", ".xlsx")


def _read_frame(path):
    """Read a CSV or Excel file into a DataFrame (dispatch by suffix)."""
    pd = _pd()
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    return pd.read_csv(path)


def _data_files(directory: Path) -> list[Path]:
    return [
        f
        for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in DATA_SUFFIXES
    ]


def _unique_name(directory: Path, filename: str) -> str:
    """Auto-rename if file exists: file.csv -> file_1.csv -> file_2.csv"""
    path = directory / filename
    if not path.exists():
        return filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while path.exists():
        path = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return path.name


def get_preview(filename: str, rows: int = 5) -> str:
    # Check output first, then uploads
    path = OUTPUT / filename if (OUTPUT / filename).exists() else UPLOADS / filename
    df = _read_frame(path)
    cols = list(df.columns)
    preview = df.head(rows).to_string(index=False)
    return f"Columns: {cols}\n{rows} rows (showing first {min(rows, len(df))}):\n{preview}"


def get_full_csv(filename: str):
    # Check output first, then uploads
    path = OUTPUT / filename if (OUTPUT / filename).exists() else UPLOADS / filename
    return _read_frame(path)


def get_first_csv():
    # Check output first (latest by modification time)
    output_files = sorted(_data_files(OUTPUT), key=os.path.getmtime, reverse=True)
    if output_files:
        return _read_frame(output_files[0])
    # Fall back to uploads
    upload_files = sorted(_data_files(UPLOADS))
    if not upload_files:
        raise FileNotFoundError("No CSV/Excel files uploaded yet.")
    return _read_frame(upload_files[0])


def first_csv_name() -> str:
    # Check output first
    output_files = sorted(_data_files(OUTPUT), key=os.path.getmtime, reverse=True)
    if output_files:
        return output_files[0].name
    upload_files = sorted(_data_files(UPLOADS))
    if not upload_files:
        raise FileNotFoundError("No CSV/Excel files uploaded yet.")
    return upload_files[0].name


def list_files() -> list[dict]:
    """List all files from both uploads/ and output/ with source info."""
    files = []
    for f in sorted(UPLOADS.iterdir()):
        if f.is_file() and f.suffix in (".csv", ".xlsx", ".png", ".json"):
            files.append({"name": f.name, "source": "uploads"})
    for f in sorted(OUTPUT.iterdir(), key=os.path.getmtime, reverse=True):
        if f.is_file() and f.suffix in (".csv", ".xlsx", ".png", ".json"):
            files.append({"name": f.name, "source": "output"})
    return files


def get_output_path(filename: str) -> str:
    return str(OUTPUT / filename)


def get_output_dir() -> str:
    return str(OUTPUT)


def save_csv(df, filename: str = "output.csv", index: bool = False) -> str:
    if not filename.endswith(".csv"):
        filename += ".csv"
    final_name = _unique_name(OUTPUT, filename)
    df.to_csv(OUTPUT / final_name, index=index)
    return final_name


def save_chart(fig, filename: str = "chart.png") -> str:
    if not filename.endswith(".png"):
        filename += ".png"
    final_name = _unique_name(OUTPUT, filename)
    fig.savefig(OUTPUT / final_name, format="png", dpi=150, bbox_inches="tight")
    return final_name


def save_excel(df, filename: str = "output.xlsx", index: bool = False) -> str:
    if not filename.endswith(".xlsx"):
        filename += ".xlsx"
    final_name = _unique_name(OUTPUT, filename)
    df.to_excel(OUTPUT / final_name, index=index)
    return final_name


def load_workbook(filename: str):
    # openpyxl only opens .xlsx — catch the common mistake of passing a .csv
    # with a clear hint so the auto-fix loop doesn't burn a retry on a cryptic
    # openpyxl InvalidFileException.
    if not filename.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        raise ValueError(
            f"load_workbook() only opens Excel files (.xlsx). You passed "
            f"{filename!r}. To read a CSV use helper.get_full_csv({filename!r}), "
            f"and save Excel files with helper.save_excel(df, 'name.xlsx')."
        )
    from openpyxl import load_workbook as _load
    # Check output first, then uploads
    path = OUTPUT / filename if (OUTPUT / filename).exists() else UPLOADS / filename
    return _load(path)


def delete_output(filename: str) -> bool:
    path = OUTPUT / filename
    if path.exists():
        path.unlink()
        return True
    return False


def save_chart_to_excel(df, x_column: str, y_column: str, filename: str = "chart.xlsx", chart_type: str = "bar") -> str:
    """Save data to Excel with an embedded Excel-native chart (not matplotlib)."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference

    if not filename.endswith(".xlsx"):
        filename += ".xlsx"

    final_name = _unique_name(OUTPUT, filename)

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"

    # Write headers
    for col_idx, col_name in enumerate(df.columns, 1):
        ws.cell(row=1, column=col_idx, value=col_name)

    # Write data
    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        for col_idx, val in enumerate(row, 1):
            ws.cell(row=row_idx, column=col_idx, value=val)

    # Find column indices for chart
    cols = list(df.columns)
    x_col = cols.index(x_column) + 1 if x_column in cols else 1
    y_col = cols.index(y_column) + 1 if y_column in cols else 2

    num_rows = len(df)

    # Create chart
    if chart_type == "line":
        chart = LineChart()
    else:
        chart = BarChart()

    chart.title = f"{y_column} by {x_column}"
    chart.y_axis.title = y_column
    chart.x_axis.title = x_column
    chart.style = 10
    chart.width = 20
    chart.height = 12

    cats = Reference(ws, min_col=x_col, min_row=2, max_row=num_rows + 1)
    values = Reference(ws, min_col=y_col, min_row=1, max_row=num_rows + 1)
    chart.add_data(values, titles_from_data=True)
    chart.set_categories(cats)

    # Place chart on a new sheet
    chart_ws = wb.create_sheet("Chart")
    chart_ws.add_chart(chart, "A1")

    wb.save(OUTPUT / final_name)
    return final_name


def sum_column(df, column: str) -> float:
    return float(df[column].sum())


def mean_column(df, column: str) -> float:
    return float(df[column].mean())


def group_aggregate(df, by: str, column: str, agg: str = "sum"):
    return getattr(df.groupby(by)[column], agg)()


def value_counts(df, column: str, top: int = 20):
    return df[column].value_counts().head(top)


# --- Analysis helpers (pure: DataFrame in -> factual result out) ---
# These compute real numbers so results are never guessed. See analysis.py.

def get_dataset_info(df):
    return analysis.get_dataset_info(df)


def get_column_info(df):
    return analysis.get_column_info(df)


def get_date_info(df):
    return analysis.get_date_info(df)


def get_missing_values(df):
    return analysis.get_missing_values(df)


def get_duplicate_info(df):
    return analysis.get_duplicate_info(df)


def get_summary_statistics(df):
    return analysis.get_summary_statistics(df)


def calculate_correlations(df, threshold: float = 0.5, max_pairs: int = 5):
    return analysis.calculate_correlations(df, threshold=threshold, max_pairs=max_pairs)


def analyze_trends(df, date_col: str | None = None):
    return analysis.analyze_trends(df, date_col=date_col)


def detect_time_confounding(correlations, trends, threshold: float = 0.5):
    return analysis.detect_time_confounding(correlations, trends, threshold=threshold)


def detect_outliers(df, k: float = 1.5):
    return analysis.detect_outliers(df, k=k)


def suggest_derived_metrics(df):
    return analysis.suggest_derived_metrics(df)
