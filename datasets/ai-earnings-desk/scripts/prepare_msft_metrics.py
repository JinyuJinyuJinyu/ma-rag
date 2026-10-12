"""Reshape Microsoft's Investor Metrics workbook into the shape the Excel Narrator Agent expects.

usage: python -I prepare_msft_metrics.py [corpus_dir]
    corpus_dir defaults to the folder above this script (datasets/ai-earnings-desk).

Reads  <corpus>/MSFT/ir/FY2026-Q4/MSFT_FY2026-Q4_metrics.xlsx (as downloaded from Microsoft's IR site).
Writes <corpus>/prepared/MSFT_FY2026-Q4_metrics.xlsx (same file name, so indexed chunks cite the original name).

The original has a title row, the header in row 2, an empty column A, section headings in column B, metric names
in column C, and percentages stored as fractions (0.38). The Excel Narrator reads row 1 as the header and
column A as each row's subject, so the prepared copy has:
- sheet "Metrics": one row per metric. Columns: Metric, Company, Segment (the section heading), one column per
  period with Microsoft's labels spelled out ("Q4'26" -> "FY2026 Q4 (Apr-Jun 2026)"; Microsoft's fiscal year
  ends on 30 June), and Units. Fractions become percentage text ("38%"); dollar amounts stay numbers in USD
  billions; "x% / y%" growth pairs (as reported / constant currency) stay as they are.
- sheet "Notes": the workbook's footnotes, plus a note saying how this copy was made.
Values are copied, never recomputed. The file is parsed as data only (openpyxl). Needs only openpyxl.
"""
import re
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

SOURCE = Path("MSFT/ir/FY2026-Q4/MSFT_FY2026-Q4_metrics.xlsx")
COMPANY = "Microsoft"
QUARTER_MONTHS = {1: ("Jul", "Sep", -1), 2: ("Oct", "Dec", -1), 3: ("Jan", "Mar", 0), 4: ("Apr", "Jun", 0)}
QUARTER_LABEL = re.compile(r"^Q([1-4])'(\d\d)$")
YEAR_LABEL = re.compile(r"^FY(\d\d)$")
GROWTH_UNITS = "% change year over year, as reported (GAAP) / in constant currency (non-GAAP)"


def period_header(label):
    """Spell out Microsoft's period labels: Q4'26 -> FY2026 Q4 (Apr-Jun 2026), FY26 -> FY2026 (Jul 2025-Jun 2026)."""
    label = str(label).strip()
    if m := QUARTER_LABEL.match(label):
        quarter, year = int(m[1]), 2000 + int(m[2])
        first, last, shift = QUARTER_MONTHS[quarter]
        return f"FY{year} Q{quarter} ({first}-{last} {year + shift})"
    if m := YEAR_LABEL.match(label):
        year = 2000 + int(m[1])
        return f"FY{year} (Jul {year - 1}-Jun {year})"
    return None


def decimals(number_format):
    """Decimal places of a number format such as 0%, 0.0%, "$"#,##0.0."""
    m = re.search(r"0\.(0+)", number_format)
    return len(m[1]) if m else 0


def value_text(cell):
    """Copy one value: fractions with a % format become percentage text, strings are tidied, numbers stay numbers."""
    value = cell.value
    if value is None:
        return None
    if isinstance(value, str):
        return re.sub(r"\s*/\s*", " / ", value.strip()) or None
    if "%" in cell.number_format:
        return f"{value * 100:.{decimals(cell.number_format)}f}%"
    return round(value, decimals(cell.number_format)) if isinstance(value, float) else value


def read_metrics(ws):
    header_row = next(r for r in range(1, ws.max_row + 1)
                      if sum(period_header(ws.cell(r, c).value) is not None for c in range(1, ws.max_column + 1)) >= 2)
    period_cols = [c for c in range(1, ws.max_column + 1) if period_header(ws.cell(header_row, c).value)]
    first_period = period_cols[0]
    units_note = str(ws.cell(header_row, first_period - 2).value or "")  # "(in billions, except for percentages)"
    if "billions" not in units_note:
        raise SystemExit(f"expected the units note '(in billions, ...)' left of the periods, found {units_note!r}")

    metrics, notes, segment = [], [], None
    for r in range(header_row + 1, ws.max_row + 1):
        section = ws.cell(r, first_period - 2).value
        name = ws.cell(r, first_period - 1).value
        values = [ws.cell(r, c) for c in period_cols]
        has_values = any(cell.value is not None for cell in values)
        if section and not name and not has_values:
            segment = str(section).strip()
        elif name and has_values:
            starred = str(name).rstrip().endswith("*")
            dollars = any("$" in cell.number_format for cell in values if isinstance(cell.value, (int, float)))
            units = GROWTH_UNITS if starred else ("USD billions" if dollars else "%")
            metrics.append([str(name).strip().rstrip("*").strip(), COMPANY, segment]
                           + [value_text(cell) for cell in values] + [units])
        elif name:
            notes.append(str(name).strip())
    headers = ["Metric", "Company", "Segment"] + [period_header(ws.cell(header_row, c).value) for c in period_cols] + ["Units"]
    return headers, metrics, notes


def write_sheet(wb, title, headers, rows):
    """Row 1 = header, column A = subject; no merged cells or title rows (shape expected by the Excel Narrator)."""
    ws = wb.create_sheet(title)
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "B2"
    for col in ws.columns:
        width = max(len(str(cell.value)) for cell in col if cell.value is not None)
        ws.column_dimensions[col[0].column_letter].width = min(max(12, width + 2), 60)


def main(corpus):
    corpus = Path(corpus)
    source = corpus / SOURCE
    wb_in = openpyxl.load_workbook(source, data_only=True)
    if len(wb_in.worksheets) != 1:
        raise SystemExit(f"expected one sheet in {source.name}, found {wb_in.sheetnames}")
    headers, metrics, notes = read_metrics(wb_in.worksheets[0])
    notes.append(f"Prepared for indexing by this project from {source.name} (Microsoft's Investor Metrics workbook "
                 "for fiscal Q4 2026): one row per metric, period labels spelled out, fractions shown as percentages. "
                 "Values are as published")  # no final period: the narrator adds one

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    write_sheet(wb, "Metrics", headers, metrics)
    write_sheet(wb, "Notes", ["Note", "Text"], [[i, text] for i, text in enumerate(notes, start=1)])
    out = corpus / "prepared" / source.name
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    print(f"{out}: Metrics {len(metrics)} rows x {len(headers)} columns, Notes {len(notes)} rows")
    print("periods:", ", ".join(headers[3:-1]))


if __name__ == "__main__":
    if len(sys.argv) > 2:
        sys.exit("usage: python -I prepare_msft_metrics.py [corpus_dir]")
    main(sys.argv[1] if len(sys.argv) == 2 else Path(__file__).resolve().parent.parent)
