#!/usr/bin/env python3
"""Clean the DHL Group "Statbook Q2 2026" workbook for the Excel Narrator Agent.

The reference narrator (services/excel_agent/narrator.py) reads each sheet like this:
  * row 1 is the header row,
  * column A is the "subject" of each row,
  * every other row becomes ONE sentence:
      "<sheet> — <A1> '<A>': <header 2> is <value>; <header 3> is <value>; ..."

The raw Statbook does not fit that shape: row 1 is a title and the periods are in row 2,
some sheets have an empty column B, ratios are stored as fractions (0.168 = 16.8 %),
numbers have 15 decimals, labels carry footnote markers ('Road Freight*', 'Net capex 1)'),
and the same label appears in several blocks of one sheet ('Europe' under revenue AND under
weight-per-day growth). This script fixes all of that and adds a small 'Notes' sheet that
keeps the footnotes and abbreviations instead of throwing them away.

Usage (run from a directory that is NOT the download folder):
    python -I clean.py <DHL-Group-Statbook-Q2-2026.xlsx> <out.xlsx>
"""

import re
import sys
from datetime import datetime

import openpyxl

# ---------------------------------------------------------------------------
# 1) Sheets to keep: raw name -> (new name, block prefixes).
#    The raw 'Title' sheet is dropped: it holds only the cover plus the names,
#    e-mails and phone numbers of Investor Relations staff (personal data).
#    Block prefixes: {raw row number where a block starts: text to put in front of
#    every label in that block}. "" switches the prefix off again.
# ---------------------------------------------------------------------------
SHEETS = {
    "Group overview": ("Group key figures", {3: "Revenue", 11: "EBIT", 18: ""}),
    "Revenue growth": ("Revenue growth", {3: "Organic revenue growth (%)"}),
    "P&L": ("Income statement", {}),
    "CF Statement": ("Cash flow statement", {}),
    "Balance Sheet": ("Balance sheet", {}),
    "DHL Express": ("DHL Express", {3: "Revenue", 9: ""}),
    "DGF": ("DHL Global Forwarding", {3: "Revenue", 9: "Gross profit", 14: ""}),
    "DSC": ("DHL Supply Chain", {3: "Revenue", 8: ""}),
    "DeC": ("DHL eCommerce", {3: "Revenue", 8: ""}),
    "P&P": ("Post & Parcel Germany", {3: "Revenue", 11: ""}),
    "Net debt": ("Net debt", {}),
    "Pensions": ("Pensions", {13: "Discount rate (%)"}),
}

# 2) Sub-heading rows (a label but no numbers) start a new block; this is the prefix they set.
HEADINGS = {
    "Absolute FX effect in revenue": "FX effect on revenue (EUR m)",
    "Weight and revenue per day": "",  # the rows below already name what they measure
    "TDI WpD growth per region": "TDI weight per day, growth vs prior year (%)",
    "Volumes Air and Ocean Freight": "",
    "Revenue by sector": "Share of revenue by sector (%)",
    "New business wins by sector": "Share of new business wins by sector (%)",
    "Volumes expressed in 000,000s": "Volume (million items)",
    "Volumes per working day expressed in 000,000s": "Volume per working day (million items)",
    "Revenue per working day expressed in € 000,000s": "Revenue per working day (EUR m)",
}

# 3) Label renames: put units in the label, mark percentages with "(%)" and
#    multiples with "(x)", and spell out codes a reader would not know.
RENAMES = {
    "EBIT margin": "EBIT margin (%)",
    "EBIT margin (in%)": "EBIT margin (%)",
    "Margin in %": "EBIT margin (%)",
    "Gross profit margin": "Gross profit margin (%)",
    "GP to EBIT conversion ratio DGF": "Gross profit to EBIT conversion ratio (%)",
    "Tax rate (in %)": "Tax rate (%)",
    "ROIC": "Return on invested capital, ROIC (%)",
    "Equity ratio": "Equity ratio (%)",
    "Net gearing": "Net gearing (%)",
    "Funding Ratio": "Pension funding ratio (%)",
    "Discount Rates Average": "Discount rate (%) - average",
    "Capex/depreciation": "Capex to depreciation of owned assets (x)",
    "Net Debt/EBITDA": "Net debt to EBITDA (x)",
    "Net debt/EBITDA": "Net debt to EBITDA (x)",
    "Basic EPS": "Basic earnings per share (EUR per share)",
    "Number of shares (in m)": "Number of shares (million)",
    "# Employees (Full Time Equivalent)": "Employees (full-time equivalents)",
    "# working days (Germany)": "Working days in Germany (days)",
    "New business wins in € m": "New business wins (EUR m)",
    "TDI Weight per day (in 000 kg)": "TDI (Time Definite International) weight per day (thousand kg)",
    "TDI Revenue per day (in m€)": "TDI (Time Definite International) revenue per day (EUR m)",
    "TDD Weight per day (in 000 kg)": "TDD (Time Definite Domestic) weight per day (thousand kg)",
    "TDD Revenue per day (in m€)": "TDD (Time Definite Domestic) revenue per day (EUR m)",
    "Air Freight Export (in 000 metric tons)": "Air freight export volume (thousand metric tons)",
    "GP/Export metric ton (in €)": "Air freight gross profit per export metric ton (EUR)",
    "Ocean Freight (in 000 TEU)": "Ocean freight volume (thousand TEU)",
    "GP/TEU (in €)": "Ocean freight gross profit per TEU (EUR)",
    "PPE": "Property, plant and equipment (PPE)",
    "MEA": "Middle East and Africa (MEA)",
    "APAC": "Asia Pacific (APAC)",
    "EMEA": "Europe, Middle East and Africa (EMEA)",
    "P&P Germany": "Post & Parcel Germany",
    "P&P Others Consolidation": "Other and consolidation (Post & Parcel Germany)",
    "Mail (MC+DM)": "Mail total (Mail Communication + Dialogue Marketing)",
    "Total DBO": "Total defined benefit obligation (DBO)",
    "Plan Assets incl. Asset ceiling": "Plan assets incl. asset ceiling",
    "Pension provision DBO": "Pension provision",
    "Pension assets DBO": "Pension assets",
    "Total EBIT": "EBIT",
    "Net other operating": "Net other operating income and expenses",
    "Operating Cash Flow": "Operating cash flow after changes in working capital",
    "Operating cash flow after changes in WC": "Operating cash flow after changes in working capital",
    "Operating cash flow before changes in WC": "Operating cash flow before changes in working capital",
    "Net profit attributable to DPAG shareholders": "Net profit attributable to Deutsche Post AG shareholders",
    "Equity attributable to DPAG shareholders": "Equity attributable to Deutsche Post AG shareholders",
}

# 4) Footnotes and abbreviations, kept as a small 'Notes' sheet (Topic -> Explanation).
NOTES = [
    ("Source", "DHL Group (Deutsche Post AG) Statbook Q2 2026, Investor Relations Reporting Hub. "
               "Amounts are in EUR millions unless a line item names another unit."),
    ("Change (%) columns", "'Q1 2026 vs Q1 2025 change (%)' and 'Q2 2026 vs Q2 2025 change (%)' are DHL's "
               "year-on-year changes, (current - prior) / |prior|. Costs, depreciation, taxes and cash outflows "
               "are negative numbers, so for those lines a NEGATIVE change means the cost or outflow GREW, "
               "and a positive change means it shrank."),
    ("Adjusted EBIT", "Group EBIT and DHL eCommerce EBIT for Q4 2025 and Q1 2026 are adjusted due to the "
               "finalized purchase price allocation (PPA) for UK eCommerce."),
    ("Road freight definition", "DHL Global Forwarding uses a new product-based road freight definition; "
               "comparable 2025 figures were added for information."),
    ("Organic revenue growth", "Revenue growth versus the prior-year quarter excluding currency (FX) effects "
               "and excluding inorganic effects (acquisitions and disposals)."),
    ("Net capex", "Cash paid to acquire property, plant and equipment and intangible assets plus proceeds "
               "from disposals of such assets."),
    ("Net cash for leases", "Repayments of non-current financial liabilities, interest expense on leases, "
               "lease receivables proceeds and lease interest received."),
    ("Net M&A", "Cash paid to acquire subsidiaries and other business units plus proceeds from their disposal, "
               "plus disposal and acquisition of equity-method and other investments."),
    ("Net interest", "Interest paid plus interest received, excluding interest cost on leases."),
    ("Net debt items", "Financial liabilities and current financial assets in the Net debt sheet exclude "
               "operating financial liabilities and assets. Positive fair values of non-current financial "
               "derivatives are recognized in non-current financial assets in the balance sheet."),
    ("Balance-sheet dates", "Balance sheet, Net debt and Pensions show values at quarter-end dates "
               "(for example 30 Jun 2026 = end of Q2 2026)."),
    ("TDI and TDD", "DHL Express products: TDI = Time Definite International, TDD = Time Definite Domestic. "
               "Weight per day is in thousand kg, revenue per day in EUR m."),
    ("TEU", "Twenty-foot equivalent unit, the standard size measure for ocean containers."),
    ("DBO and funding ratio", "DBO = defined benefit obligation of the pension plans; the funding ratio is "
               "plan assets divided by the DBO."),
    ("Divisions", "DHL Group reports five divisions: DHL Express, DHL Global Forwarding (freight), "
               "DHL Supply Chain (contract logistics), DHL eCommerce and Post & Parcel Germany, "
               "plus Group Functions."),
]

QUARTER_END = {"Q1": "31 Mar", "Q2": "30 Jun", "Q3": "30 Sep", "Q4": "31 Dec"}


def column_name(cell, previous, quarter_end_dates):
    """Turn one raw header cell (row 2) into a readable column name."""
    if isinstance(cell, datetime):  # Net debt and Pensions use real dates
        return cell.strftime("%d %b %Y")  # e.g. '30 Jun 2026'
    text = str(cell).strip()
    if text.lower().startswith("yoy"):  # 'yoy in %' describes the quarter just before it
        prior = f"{previous[:-4]}{int(previous[-4:]) - 1}"  # 'Q2 2026' -> 'Q2 2025'
        return f"{previous} vs {prior} change (%)"
    if quarter_end_dates and text[:2] in QUARTER_END:  # balance sheet: 'Q2 2026' -> '30 Jun 2026'
        return f"{QUARTER_END[text[:2]]} {text[-4:]}"
    return text


def tidy(value, label, is_change_column):
    """Round numbers and turn fractions into percentages."""
    if not isinstance(value, (int, float)):
        return value
    if "Discount rate" in label:  # pension discount rates are quoted with 2 decimals: 0.0436 -> 4.36
        return round(value * 100, 2)
    if is_change_column or "(%)" in label:  # stored as fractions: 0.168 -> 16.8
        return round(value * 100, 1)
    if "(x)" in label or "per share" in label:  # multiples and EPS keep 2 decimals
        return round(value, 2)
    if "full-time equivalents" in label:  # head counts are whole numbers
        return round(value)
    return round(value, 1)  # EUR m and everything else: 1 decimal


def clean_sheet(raw_ws, new_ws, blocks):
    rows = [list(r) for r in raw_ws.iter_rows(values_only=True)]
    header = rows[1]  # row 2 holds the periods
    # Keep only columns with a period in row 2 (drops the empty column B and trailing blanks).
    value_cols = [j for j, h in enumerate(header) if j > 0 and h is not None and str(h).strip()]
    names, previous = [], None
    for j in value_cols:
        name = column_name(header[j], previous, quarter_end_dates=(raw_ws.title == "Balance Sheet"))
        names.append(name)
        previous = name
    new_ws.append(["Line item (EUR m unless stated)"] + names)

    prefix, kept = "", 0
    for row_number, row in enumerate(rows[2:], start=3):
        if row_number in blocks:
            prefix = blocks[row_number]
        label = " ".join(str(row[0] or "").split())  # also removes line breaks inside labels
        values = [row[j] if j < len(row) else None for j in value_cols]
        has_numbers = any(isinstance(v, (int, float)) for v in values)

        if label.startswith("Working Days (Germany)"):  # small calendar table at the end of P&P
            break
        if not has_numbers and not label:  # empty row or repeated period header
            continue
        if re.match(r"^(\*|\d\)|\+)", label):  # footnote text (kept in the Notes sheet)
            continue
        if not has_numbers:  # sub-heading row -> new prefix
            prefix = HEADINGS[label]
            continue

        label = re.sub(r"(\*|\d\))$", "", label).strip()  # 'Road Freight*' -> 'Road Freight'
        label = RENAMES.get(label, label)
        if prefix:
            label = f"{prefix} - {label}"
        cells = [tidy(v, label, name.endswith("change (%)")) for v, name in zip(values, names)]
        new_ws.append([label] + cells)
        kept += 1
    return kept


def main():
    raw_path, out_path = sys.argv[1], sys.argv[2]
    raw = openpyxl.load_workbook(raw_path, read_only=True, data_only=True)  # cached values, no formulas
    out = openpyxl.Workbook()
    out.remove(out.active)

    total = 0
    for raw_name, (new_name, blocks) in SHEETS.items():
        kept = clean_sheet(raw[raw_name], out.create_sheet(new_name), blocks)
        print(f"{new_name}: {kept} rows")
        total += kept

    notes = out.create_sheet("Notes")
    notes.append(["Topic", "Explanation"])
    for topic, text in NOTES:
        notes.append([topic, text.rstrip(".")])  # the narrator adds its own full stop
    print(f"Notes: {len(NOTES)} rows")

    out.save(out_path)
    print(f"saved {out_path}: {len(out.sheetnames)} sheets, {total + len(NOTES)} rows")


if __name__ == "__main__":
    main()
