"""Clean the World Bank / S&P Global "CPPI 2020 to 2024 Annex" workbook for the Excel Narrator.

Usage:
    python -I clean.py "CPPI_2020_to_2024_Annex.xlsx" portpulse_clean.xlsx

The Excel Narrator reads ROW 1 AS THE HEADER and COLUMN A AS THE ROW'S SUBJECT, and writes one
sentence per row ("Ports — Port 'Aarhus': UN/LOCODE is DKAAR; ..."). The raw annex is already
tidy (one sheet, header in row 1, Port in column A), so this script only makes the sentences
easier to read and to retrieve:

  1. Expand codes: ECA -> "Europe & Central Asia", HI -> "High income" (codes from the
     report's Annex notes a and b).
  2. Add "2024" to the columns that only describe 2024 (rank, sub-indices, berth share).
  3. Show the berth-time share as a percentage (0.9 -> 90).
  4. Add one helper column: change in CPPI score from 2023 to 2024.
  5. Move the three group-average columns (identical on every row of a group) into a
     separate "Group summary" sheet, one row per region / income group, together with each
     group's best- and worst-ranked port and its biggest improver and decliner. Row-by-row
     retrieval cannot find a "highest" or "lowest" value across 403 rows; these summary
     rows can be retrieved directly.
  6. Add a short "Glossary" sheet that explains the scores (higher is better, 2024 average = 0).

Blank cells are kept blank: the port was not in the index that year, and the narrator
skips empty cells.
"""

import sys
from collections import Counter

import openpyxl
from openpyxl.styles import Font

# Codes used in the annex (CPPI 2020-2024 report, Annex notes a and b).
REGION_NAMES = {
    "EAP": "East Asia & Pacific",
    "ECA": "Europe & Central Asia",
    "LAC": "Latin America & Caribbean",
    "MENA": "Middle East & North Africa",
    "NAM": "North America",
    "SAR": "South Asia",
    "SSA": "Sub-Saharan Africa",
}
INCOME_NAMES = {
    "HI": "High income",
    "UMI": "Upper middle income",
    "LMI": "Lower middle income",
    "LI": "Low income",
}

PORT_HEADERS = [
    "Port",                                   # column A = the subject of each sentence
    "UN/LOCODE",
    "Country or territory",
    "World Bank region",
    "Income group",
    "Maritime region",
    "CPPI 2020 score",
    "CPPI 2021 score",
    "CPPI 2022 score",
    "CPPI 2023 score",
    "CPPI 2024 score",
    "Global rank 2024 (1 = best of 403)",
    "Change 2023 to 2024 (CPPI points)",
    "Time at berth 2024 (% of time in port)",
    "Statistical index 2024",
    "Administrative index 2024",
]

# Each meaning is written to follow "Meaning is ..." and has no final full stop
# (the narrator adds one at the end of every sentence).
GLOSSARY = [
    ("CPPI score",
     "the Container Port Performance Index of the World Bank and S&P Global Market Intelligence. It "
     "measures how quickly a port turns container ships around (time in port), compared with ports "
     "handling ships of the same size and the same number of container moves. Higher is better. Scores "
     "are centred so that the average of all 403 ports in 2024 is 0, so a negative score means slower "
     "than the 2024 average. The 2020-2023 scores were recalculated on the same 2024 basis, so years "
     "are comparable within this edition but not with scores printed in earlier CPPI reports"),
    ("Global rank 2024",
     "the port's position among the 403 ports in the CPPI 2024. Rank 1 is the best (Yangshan, China) "
     "and rank 403 the worst (Durban, South Africa). Ranks use unrounded scores, so two ports with the "
     "same rounded score still have different ranks"),
    ("Statistical index and administrative index",
     "the two component indices of the CPPI. The administrative index measures vessel time in port "
     "directly, adjusted for ship size and call size; the statistical index applies factor analysis to "
     "the same port-time data. The combined CPPI score is the average of the two (report chapter 5)"),
    ("Time at berth (% of time in port)",
     "the share of a ship's total time in port that is spent at the berth; the rest is time waiting at "
     "anchor or arriving. The vessel's departure time is not included. A low share points to long "
     "waiting times at anchor"),
    ("Change 2023 to 2024 (CPPI points)",
     "the CPPI 2024 score minus the CPPI 2023 score, computed from the rounded scores in this workbook. "
     "A positive value means the port improved. It can differ by about 1 point from the report's "
     "Table 2.3, which uses unrounded scores"),
    ("Blank year scores",
     "a sign that the port was not in the index that year: 79 ports have no 2020 score, 55 no 2021 "
     "score, 64 no 2022 score and 16 no 2023 score. All 403 ports have a 2024 score"),
    ("Average CPPI 2024 (as published)",
     "the group average copied from the report's annex: an unweighted average of the ports' CPPI 2024 "
     "scores. The published Middle East & North Africa (22.6) and South Asia (30.9) averages count "
     "Pakistan's two ports (Karachi, Muhammad Bin Qasim) in South Asia, although their port rows are "
     "labelled Middle East & North Africa"),
]


def port_label(p):
    """Short description of a port used in the summary sheet, e.g. 'Durban (South Africa)'."""
    if p["Country or territory"] in p["Port"]:  # e.g. 'Freeport (Bahamas)' already names it
        return p["Port"]
    return f"{p['Port']} ({p['Country or territory']})"


def describe_ranked(p):
    return f"{port_label(p)}, CPPI 2024 score {p['CPPI 2024 score']}, rank {p['Global rank 2024 (1 = best of 403)']}"


def describe_change(ports_with_change, pick):
    """Biggest improver (pick=max) or decliner (pick=min); ties are listed together."""
    key = "Change 2023 to 2024 (CPPI points)"
    if not ports_with_change:
        return None
    best = pick(p[key] for p in ports_with_change)
    # Do not call a gain a "decline" (or a fall an "improvement") in small groups.
    if pick is max and best <= 0:
        return "None: no port in this group improved"
    if pick is min and best >= 0:
        return "None: no port in this group declined"
    winners = [p for p in ports_with_change if p[key] == best]
    return " and ".join(
        f"{port_label(p)}, {p[key]:+d} points ({p['CPPI 2023 score']} in 2023 to {p['CPPI 2024 score']} in 2024)"
        for p in winners)


def main(raw_path, out_path):
    wb = openpyxl.load_workbook(raw_path, read_only=True, data_only=True)
    rows = list(wb["Annex"].iter_rows(values_only=True))
    wb.close()
    col = {name: i for i, name in enumerate(rows[0])}  # raw header name -> column index

    ports = []
    published_avg = {"World Bank region": {}, "Income group": {}, "Maritime region": {}}
    for r in rows[1:]:
        if r[0] is None:  # skip blank lines, if any
            continue
        raw = lambda name: r[col[name]]
        c23, c24 = raw("CPPI 2023"), raw("CPPI 2024")
        port = {
            "Port": raw("Port").strip(),
            "UN/LOCODE": raw("LOCODE"),
            "Country or territory": raw("Territory"),
            "World Bank region": REGION_NAMES[raw("World Bank Region")],
            "Income group": INCOME_NAMES[raw("World Bank Income Group")],
            "Maritime region": raw("Maritime services region"),
            "CPPI 2020 score": raw("CPPI 2020"),
            "CPPI 2021 score": raw("CPPI 2021"),
            "CPPI 2022 score": raw("CPPI 2022"),
            "CPPI 2023 score": c23,
            "CPPI 2024 score": c24,
            "Global rank 2024 (1 = best of 403)": raw("Rank"),
            "Change 2023 to 2024 (CPPI points)": (c24 - c23) if c23 is not None else None,
            "Time at berth 2024 (% of time in port)": round(raw("Berth hours in % of port hours") * 100),
            "Statistical index 2024": raw("Statistical Index"),
            "Administrative index 2024": raw("Administrative Index"),
        }
        ports.append(port)
        # Remember each port's published group averages; we keep the most common value per group
        # (one raw row, La Guaira, carries the wrong income-group average -3.4 instead of -2.5).
        for group_col, avg_col in (("World Bank region", "Region average CPPI"),
                                   ("Income group", "Income Group average CPPI"),
                                   ("Maritime region", "region's average cppi")):
            published_avg[group_col].setdefault(port[group_col], []).append(raw(avg_col))

    ports.sort(key=lambda p: p["Port"].lower())

    # ---- Group summary: one row for all ports, then one per region / income group ----
    summary_headers = ["Group", "Group type", "Ports in index", "Average CPPI 2024 (as published)",
                       "Best-ranked port 2024", "Worst-ranked port 2024",
                       "Biggest improvement 2023 to 2024", "Biggest decline 2023 to 2024"]
    groups = [("All 403 ports", "All ports", ports, round(sum(p["CPPI 2024 score"] for p in ports) / len(ports), 1))]
    for group_col in ("World Bank region", "Income group", "Maritime region"):
        for name in sorted(published_avg[group_col]):
            members = [p for p in ports if p[group_col] == name]
            avg = Counter(published_avg[group_col][name]).most_common(1)[0][0]
            groups.append((name, group_col, members, round(float(avg), 1)))

    summary_rows = []
    for name, kind, members, avg in groups:
        by_rank = sorted(members, key=lambda p: p["Global rank 2024 (1 = best of 403)"])
        with_change = [p for p in members if p["Change 2023 to 2024 (CPPI points)"] is not None]
        summary_rows.append([name, kind, len(members), avg,
                             describe_ranked(by_rank[0]), describe_ranked(by_rank[-1]),
                             describe_change(with_change, max), describe_change(with_change, min)])

    # ---- Write the cleaned workbook ----
    out = openpyxl.Workbook()
    sheets = [
        ("Ports", PORT_HEADERS, [[p[h] for h in PORT_HEADERS] for p in ports]),
        ("Group summary", summary_headers, summary_rows),
        ("Glossary", ["Term", "Meaning"], [list(t) for t in GLOSSARY]),
    ]
    out.remove(out.active)
    for title, headers, data in sheets:
        ws = out.create_sheet(title)
        ws.append(headers)          # row 1 = header
        for row in data:
            ws.append(row)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "B2"
    out.save(out_path)
    print(f"wrote {out_path}: " + ", ".join(f"{t} ({len(d)} rows)" for t, _, d in sheets))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -I clean.py <raw annex.xlsx> <out.xlsx>")
    main(sys.argv[1], sys.argv[2])
