"""Functions available to AI-generated code running in working_dir/app.py."""

from pathlib import Path

WORKDIR = Path(__file__).resolve().parent / "working_dir"
UPLOADS = WORKDIR / "uploads"
OUTPUT = WORKDIR / "output"
OUTPUT.mkdir(exist_ok=True)


def _pd():
    import pandas as pd
    return pd


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def get_preview(filename: str, rows: int = 5) -> str:
    pd = _pd()
    df = pd.read_csv(UPLOADS / filename)
    cols = list(df.columns)
    preview = df.head(rows).to_string(index=False)
    return f"Columns: {cols}\n{rows} rows (showing first {min(rows, len(df))}):\n{preview}"


def get_full_csv(filename: str):
    pd = _pd()
    return pd.read_csv(UPLOADS / filename)


def get_first_csv():
    pd = _pd()
    csvs = sorted(UPLOADS.glob("*.csv"))
    if not csvs:
        raise FileNotFoundError("No CSV files uploaded yet.")
    return pd.read_csv(csvs[0])


def first_csv_name() -> str:
    csvs = sorted(UPLOADS.glob("*.csv"))
    if not csvs:
        raise FileNotFoundError("No CSV files uploaded yet.")
    return csvs[0].name


def list_files() -> list[str]:
    return [f.name for f in sorted(UPLOADS.iterdir()) if f.suffix == ".csv"]


def get_output_path(filename: str) -> str:
    return str(OUTPUT / filename)


def get_output_dir() -> str:
    return str(OUTPUT)


def save_csv(df, filename: str = "output.csv", index: bool = False) -> str:
    if not filename.endswith(".csv"):
        filename += ".csv"
    df.to_csv(OUTPUT / filename, index=index)
    return filename


def save_chart(fig, filename: str = "chart.png") -> str:
    if not filename.endswith(".png"):
        filename += ".png"
    fig.savefig(OUTPUT / filename, format="png", dpi=150, bbox_inches="tight")
    return filename


def save_excel(df, filename: str = "output.xlsx", index: bool = False) -> str:
    if not filename.endswith(".xlsx"):
        filename += ".xlsx"
    df.to_excel(OUTPUT / filename, index=index)
    return filename


def load_workbook(filename: str):
    from openpyxl import load_workbook as _load
    return _load(OUTPUT / filename)


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

    wb.save(OUTPUT / filename)
    return filename


def sum_column(df, column: str) -> float:
    return float(df[column].sum())


def mean_column(df, column: str) -> float:
    return float(df[column].mean())


def group_aggregate(df, by: str, column: str, agg: str = "sum"):
    return getattr(df.groupby(by)[column], agg)()


def value_counts(df, column: str, top: int = 20):
    return df[column].value_counts().head(top)
