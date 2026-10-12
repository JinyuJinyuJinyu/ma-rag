"""Subset WHO's Global Health Expenditure Database (GHED) for the Excel Narrator Agent.

The raw GHED_data.xlsx is 37 MB: sheet 'Data' has 4,612 rows (country x year) and
4,120 indicator columns, so narrating it whole would produce ~144,000 chunks.
We keep 10 headline financing indicators + population for 2023 (latest) and
2019 (pre-pandemic baseline), give them readable headers with units, and add:
  - a small 'Rankings 2023' sheet (derived: top/bottom 3 countries per indicator),
    so "which country has the highest X?" can be answered from ONE retrieved chunk
    (the reference retriever only returns the top 5 chunks);
  - an 'Indicator Definitions' sheet copied from GHED's own 'Codebook' sheet.

The narrator reads ROW 1 AS THE HEADER and COLUMN A AS THE ROW'S SUBJECT, so every
sheet has a one-row header and the country (or indicator) in column A.

Usage:
    python -I clean.py <GHED_data.xlsx> <out.xlsx>
Reading the raw file takes ~15-25 seconds (openpyxl read-only mode).
"""
import sys

import openpyxl

YEARS = [2023, 2019]

# (GHED code, readable header). "Health spending" = current health expenditure (CHE).
ID_COLUMNS = [
    ("location", "Country"),
    ("code", "ISO3 Code"),
    ("region", "WHO Region"),
    ("income", "World Bank Income Group (2021 classification)"),  # GHED uses the 2021 list for ALL years
]
INDICATORS = [
    ("che_gdp", "Health Spending (% of GDP)"),
    ("che_pc_usd", "Health Spending per Person (current US$)"),
    ("gghed_che", "Government Share of Health Spending (%)"),
    ("gghed_gdp", "Government Health Spending (% of GDP)"),
    ("gghed_gge", "Health Share of Government Spending (%)"),
    ("gghed_pc_usd", "Government Health Spending per Person (current US$)"),
    ("oops_che", "Out-of-Pocket (OOP) Share of Health Spending (%)"),
    ("oop_pc_usd", "Out-of-Pocket (OOP) Spending per Person (current US$)"),
    ("ext_che", "External Funding Share of Health Spending (%)"),
    ("vhi_che", "Voluntary Health Insurance Share of Health Spending (%)"),
]
POPULATION = ("pop", "Population (thousands)")

REGIONS = {"AFR": "African Region", "AMR": "Region of the Americas", "SEAR": "South-East Asia Region",
           "EUR": "European Region", "EMR": "Eastern Mediterranean Region", "WPR": "Western Pacific Region"}

# Short plain-language notes for the definitions sheet (the rest comes from GHED's Codebook).
NOTES = {
    "che_pc_usd": "Nominal US$ at market exchange rates; not adjusted for inflation or purchasing power, "
                  "so 2019 and 2023 values also move with exchange rates",
    "gghed_che": "Domestic general government sources only: government revenue for health plus social "
                 "health insurance contributions. Donor money is counted as external funding instead",
    "oops_che": "Payments made directly by households at the point of care",
    "ext_che": "Money of foreign origin (donors), whether channelled through government or not. "
               "Blank for many high-income countries where it is not reported",
    "vhi_che": "Classified by financing scheme, while the government, OOP and external shares are "
               "classified by source of funds, so the four shares can overlap and need not add up to 100%",
    "income": "World Bank income group from the 2021 classification, applied to every year in GHED",
}


def round_value(code, value):
    """Percentages and population to 1 decimal, US$ amounts to whole dollars."""
    if not isinstance(value, (int, float)):
        return value  # None stays None (the narrator skips empty cells)
    return round(value) if code.endswith("_usd") else round(value, 1)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)

    # ---- Read the needed columns of sheet 'Data' (header in row 1, one row per country-year).
    rows = wb["Data"].iter_rows(values_only=True)
    header = list(next(rows))
    wanted = ID_COLUMNS + INDICATORS + [POPULATION]
    col = {code: header.index(code) for code, _ in wanted}
    year_col = header.index("year")
    by_year = {y: [] for y in YEARS}
    for r in rows:
        if r[year_col] not in by_year:
            continue
        out = []
        for code, _ in wanted:
            v = r[col[code]]
            if code == "region":
                v = REGIONS.get(v, v)
            elif code == "income":
                v = f"{v} income" if v else None  # "Upper-middle" -> "Upper-middle income"
            else:
                v = round_value(code, v)
            out.append(v)
        by_year[r[year_col]].append(out)

    # ---- Codebook rows for our indicator codes (variable name, unit, formula).
    codebook = {}
    for r in wb["Codebook"].iter_rows(min_row=2, values_only=True):
        codebook[r[0]] = {"name": r[1], "unit": r[5], "formula": r[7]}
    wb.close()

    out = openpyxl.Workbook()
    out.remove(out.active)
    headers = [h for _, h in wanted]

    # ---- Sheets 1-2: one row per country, A-Z.
    for y in YEARS:
        ws = out.create_sheet(f"Health Spending {y}")
        ws.append(headers)
        for r in sorted(by_year[y], key=lambda r: r[0]):
            ws.append(r)
        ws.freeze_panes = "B2"
        print(f"Health Spending {y}: {len(by_year[y])} rows")

    # ---- Sheet 3: derived rankings for 2023 (top 3 and bottom 3 countries per indicator).
    ws = out.create_sheet("Rankings 2023")
    ws.append(["Indicator", "Highest 3 Countries", "Lowest 3 Countries", "Countries With Data"])
    latest = by_year[2023]
    for i, (code, name) in enumerate(INDICATORS, start=len(ID_COLUMNS)):
        # (value, country) pairs, low -> high. Values are already rounded, so equal values are ties.
        ranked = sorted((r[i], r[0]) for r in latest if r[i] is not None)
        fmt = (lambda v: f"{v:,.0f}") if code.endswith("_usd") else (lambda v: f"{v:.1f}")
        highest = ", ".join(f"{c} ({fmt(v)})" for v, c in reversed(ranked[-3:]))
        zeros = [c for v, c in ranked if v == 0]  # values are already rounded to 1 decimal
        if len(zeros) > 1:  # e.g. external funding: many countries at 0 -> no single "lowest"
            lowest = f"{len(zeros)} countries at 0.0 (for example {', '.join(zeros[:3])})"
        else:
            lowest = ", ".join(f"{c} ({fmt(v)})" for v, c in ranked[:3])
        ws.append([name, highest, lowest, len(ranked)])
    print(f"Rankings 2023: {len(INDICATORS)} rows")

    # ---- Sheet 4: indicator definitions (from GHED's Codebook + short notes).
    ws = out.create_sheet("Indicator Definitions")
    ws.append(["Indicator", "GHED Code", "Official GHED Name", "How It Is Calculated", "Unit", "Notes"])
    for code, name in INDICATORS + [POPULATION, ID_COLUMNS[3]]:
        cb = codebook[code]
        formula = None if cb["formula"] in (None, "-") else cb["formula"]
        # GHED's unit words are terse ("Ones" = plain US$ amounts); make them readable.
        unit = {"Percentage": "Percent", "Ones": "US$ per person", "Thousands": "Thousands of people"}.get(cb["unit"])
        ws.append([name, code, cb["name"], formula, unit, NOTES.get(code)])
    print(f"Indicator Definitions: {len(INDICATORS) + 2} rows")

    out.save(dst)
    print("saved", dst)


if __name__ == "__main__":
    main()
