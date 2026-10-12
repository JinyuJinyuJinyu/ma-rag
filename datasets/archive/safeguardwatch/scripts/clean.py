#!/usr/bin/env python3
"""Clean the Clean Energy Regulator (CER) Safeguard Mechanism "Baselines and emissions" tables
so the Excel Narrator Agent can read them (it treats ROW 1 as the header and COLUMN A as the
subject of each row sentence).

Usage (one year, or both years):
    python -I clean.py <raw_2024-25.xlsx> <out.xlsx>
    python -I clean.py <raw_2024-25.xlsx> <out.xlsx> <raw_2023-24.xlsx>

What it does, per yearly table:
  1. Drops the 3 title rows above the real header ("Facility name | Responsible emitter | ...").
  2. Renames headers so they are readable and carry units (the unit note lived in the
     deleted title rows: "All emissions values are in t CO2-e").
  3. Removes placeholders: '-' becomes an empty cell, and 0 becomes empty in four columns that
     are almost always 0 (the narrator skips empty cells, so rows stay short). Rounds tonnes
     stored as floats (e.g. 4729.999999999997) to whole tonnes.
  4. Expands codes: state abbreviations and the CER's long "Notes" sentences.
  5. Sorts facilities from largest to smallest covered emissions.
Then it adds two small helper sheets:
  - "Rankings": top 5 facilities for a few measures (row sentences alone cannot answer
    "which facility had the highest ...", because retrieval only returns a few rows).
  - "Guide": what each column means (from the CER publication notes) and the sign conventions.

Source data: Clean Energy Regulator, CC BY 4.0. Attribution for this derived file:
"Based on Clean Energy Regulator material licensed under a Creative Commons Attribution 4.0 licence."
"""
import sys

import openpyxl
from openpyxl import Workbook

# Old header -> new header (units added; CER abbreviations explained).
RENAME = {
    "Facility name": "Facility",
    "Responsible emitter": "Responsible emitter (company)",
    "State/Territory of operation": "State or territory",
    "ANZSIC": "Industry (ANZSIC)",
    "ERC": "Emission reduction contribution (ERC) factor",
    "Baseline emissions number": "Baseline (tCO2e)",
    "Covered emissions": "Covered emissions (tCO2e)",
    "Borrowing adjustment amount": "Borrowing adjustment to baseline (tCO2e)",
    "ACCUs issued": "ACCUs issued (carbon credits)",
    "ACCUs deemed surrendered": "ACCUs deemed surrendered (carbon credits)",
    "ACCUs surrendered": "ACCUs surrendered (carbon credits)",
    "SMCs surrendered": "SMCs surrendered (Safeguard credits)",
    "Net emissions number": "Net emissions (tCO2e)",
    "Net position number": "Net position vs baseline (tCO2e, negative = below baseline)",
    "SMCs issued": "SMCs issued (Safeguard credits)",
    "Units relinquished": "Units relinquished",
    "Cumulative MYMP net emissions number": "Multi-year period cumulative net emissions (tCO2e)",
    "Cumulative MYMP net position number": "Multi-year period cumulative net position (tCO2e)",
    "GHG Carbon dioxide": "Carbon dioxide (tCO2e)",
    "GHG Methane": "Methane (tCO2e)",
    "GHG Nitrous oxide": "Nitrous oxide (tCO2e)",
    "GHG Other": "Other greenhouse gases (tCO2e)",
    "Notes": "Notes",
}

# Columns that are 0 for almost every facility: an empty cell is clearer than "is 0".
ZERO_MEANS_NONE = {"Borrowing adjustment amount", "ACCUs issued", "ACCUs deemed surrendered",
                   "Units relinquished"}

STATES = {
    "WA": "Western Australia (WA)", "QLD": "Queensland (QLD)", "NSW": "New South Wales (NSW)",
    "VIC": "Victoria (VIC)", "SA": "South Australia (SA)", "NT": "Northern Territory (NT)",
    "TAS": "Tasmania (TAS)", "ACT": "Australian Capital Territory (ACT)",
    "National": "National (operates across several states)",
}

# The CER's long note sentences -> short plain-English versions (same meaning).
NOTE_REWRITES = {
    "As the facility surrendered 30% or more of its baseline in ACCUs, the required written "
    "explanation as to why more carbon abatement did not occur at the facility can be found on the "
    "baselines and emissions data page.":
        "Surrendered ACCUs equal to 30% or more of its baseline, so it had to publish a statement "
        "explaining why more on-site abatement did not occur.",
    "As the facility surrendered 30% or more of its baseline, the required written explanation as "
    "to why more carbon abatement did not occur at the facility can be found on the baselines and "
    "emissions data page.":
        "Surrendered 30% or more of its baseline, so it had to publish a statement explaining why "
        "more on-site abatement did not occur.",
    "The facility is an eligible facility under 58B.":
        "Eligible facility under section 58B of the Safeguard Rule: covered emissions below the "
        "100,000 tCO2e threshold, it may apply for SMCs, and it is not counted in scheme totals.",
    "Please refer to the Multi-year monitoring period reported emissions data table for more "
    "information.":
        "Uses a multi-year monitoring period (see the CER multi-year monitoring period table).",
    "Please refer to the excess emissions situation table for more information.":
        "Was in an excess emissions situation at the 1 April compliance deadline (see the CER "
        "excess emissions situation table).",
    "The facility is covered by a landfill baseline.": "Has a landfill baseline.",
    "The facility has no production variable applicable to its activities, and therefore no "
    "baseline has been set by the CER. DCCEEW is considering the setting of a relevant production "
    "variable.":
        "No baseline set: no production variable applies to its activities (DCCEEW is "
        "considering one).",
}


def clean_value(header, value):
    """Return the cleaned cell value for one column."""
    if isinstance(value, str):
        value = value.strip()
        if value in ("", "-"):           # '-' is a placeholder for "nothing to report"
            return None
        if value == "N/A":               # one facility has no baseline (see its Notes)
            return "N/A (no baseline set)"
    if isinstance(value, float):
        if header == "ERC":
            return round(value, 5)       # a factor such as 0.902: keep the decimals
        # Tonnes are whole numbers, but a few 2023-24 cells are stored as e.g.
        # 4729.999999999997, which the narrator would print in full.
        value = round(value)
    if header in ZERO_MEANS_NONE and value == 0:
        return None
    if header == "State/Territory of operation":
        # e.g. "QLD; SA" -> "Queensland (QLD); South Australia (SA)"
        return "; ".join(STATES.get(s.strip(), s.strip()) for s in value.split(";"))
    if header == "Notes":
        for long_text, short_text in NOTE_REWRITES.items():
            value = value.replace(long_text, short_text)
        return value.rstrip(".")         # the narrator adds its own full stop
    return value


def read_year(path):
    """Read one raw CER table -> (year label, new headers, cleaned rows sorted by emissions)."""
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    year = str(rows[0][0]).split()[0].replace("–", "-")   # "2024–25 Baselines..." -> "2024-25"
    header_idx = next(i for i, r in enumerate(rows) if r[0] == "Facility name")
    old_headers = [str(h).strip() for h in rows[header_idx]]
    cleaned = []
    for r in rows[header_idx + 1:]:
        if r[0] is None:                 # skip empty rows (there are none today)
            continue
        cleaned.append([clean_value(h, v) for h, v in zip(old_headers, r)])
    covered = old_headers.index("Covered emissions")
    cleaned.sort(key=lambda row: -row[covered])                    # largest emitter first
    return year, [RENAME[h] for h in old_headers], cleaned


# Rankings sheet: (label, column to rank by). Top 5 rows per measure and year.
RANKINGS = [
    ("Highest covered emissions (tCO2e)", "Covered emissions (tCO2e)"),
    ("Most ACCUs surrendered (carbon credits)", "ACCUs surrendered (carbon credits)"),
    ("Most SMCs surrendered (Safeguard credits)", "SMCs surrendered (Safeguard credits)"),
    ("Most SMCs issued (Safeguard credits)", "SMCs issued (Safeguard credits)"),
    ("Highest methane emissions (tCO2e)", "Methane (tCO2e)"),
    ("Furthest above baseline (annual net position, tCO2e)",
     "Net position vs baseline (tCO2e, negative = below baseline)"),
]

GUIDE = [
    ("Source", "Clean Energy Regulator (CER) Safeguard Mechanism baselines and emissions tables "
               "for 2023-24 and 2024-25, data as at 13 August 2026, licensed CC BY 4.0"),
    ("Units", "All emissions values are tonnes of carbon dioxide equivalent (tCO2e). One ACCU or "
              "one SMC covers one tonne"),
    ("Empty cell", "An empty cell means zero or not applicable"),
    ("Facility", "Facility name. A facility appears twice in a year when operational control "
                 "changed hands during the year (each responsible emitter reports its own part)"),
    ("Responsible emitter (company)", "The company with operational control of the facility, "
                                      "responsible for its Safeguard obligations"),
    ("State or territory", "Where the facility operates; 'National' means it operates across "
                           "several states, for example airlines, rail freight and pipelines"),
    ("Industry (ANZSIC)", "Industry classification (Australian and New Zealand Standard "
                          "Industrial Classification), with the ANZSIC code in brackets"),
    ("Emission reduction contribution (ERC) factor",
     "Scaling factor that applies the facility's baseline decline rate (default decline 4.9% a "
     "year, lower for trade-exposed baseline-adjusted (TEBA) facilities). Most facilities have "
     "0.951 in 2023-24 and 0.902 in 2024-25"),
    ("Baseline (tCO2e)", "The facility's legislated limit on net emissions for the year. "
                         "Calculated baselines below 100,000 t are raised to the 100,000 t minimum"),
    ("Covered emissions (tCO2e)", "Scope 1 emissions reported under the NGER Scheme that count "
                                  "towards the Safeguard Mechanism"),
    ("Borrowing adjustment to baseline (tCO2e)",
     "Positive when the facility borrowed from next year's baseline; negative when an earlier "
     "borrowing is paid back"),
    ("ACCUs issued (carbon credits)", "Australian Carbon Credit Units issued for projects at the "
                                      "facility; added back to net emissions to avoid double counting"),
    ("ACCUs deemed surrendered (carbon credits)",
     "ACCUs from the facility's projects delivered to the Commonwealth under a carbon abatement "
     "contract; treated as surrendered"),
    ("ACCUs surrendered (carbon credits)", "Australian Carbon Credit Units surrendered to cover "
                                           "emissions above the baseline; subtracted from net emissions"),
    ("SMCs surrendered (Safeguard credits)", "Safeguard Mechanism Credit units surrendered to "
                                             "cover emissions above the baseline; subtracted from net emissions"),
    ("Net emissions (tCO2e)", "Covered emissions plus ACCUs issued, minus ACCUs deemed "
                              "surrendered, ACCUs surrendered and SMCs surrendered"),
    ("Net position vs baseline (tCO2e, negative = below baseline)",
     "Net emissions minus baseline. Negative = below the baseline, 0 = exactly at the baseline "
     "(usually after surrendering units), positive = above the baseline"),
    ("SMCs issued (Safeguard credits)", "Safeguard Mechanism Credits issued because the "
                                        "facility's net emissions were below its baseline"),
    ("Units relinquished", "Units the responsible emitter had to hand back because SMCs were "
                           "issued based on incorrect data"),
    ("Multi-year period cumulative net emissions (tCO2e)",
     "For facilities on a multi-year monitoring period (MYMP): net emissions over the whole "
     "extended period"),
    ("Multi-year period cumulative net position (tCO2e)",
     "For facilities on a multi-year monitoring period: net position against the baseline over "
     "the whole extended period"),
    ("Carbon dioxide (tCO2e)", "Part of covered emissions that is carbon dioxide"),
    ("Methane (tCO2e)", "Part of covered emissions that is methane, in CO2-equivalent tonnes"),
    ("Nitrous oxide (tCO2e)", "Part of covered emissions that is nitrous oxide, in "
                              "CO2-equivalent tonnes"),
    ("Other greenhouse gases (tCO2e)", "Covered emissions from all other greenhouse gases. The "
                                       "four gas columns add up to covered emissions"),
    ("Notes", "CER notes, shortened: ACCU surrenders of 30% or more of baseline, eligible "
              "(section 58B) facilities, multi-year monitoring periods, excess emissions "
              "situations, landfill baselines and relinquished units"),
]


def ranking_rows(year, headers, rows):
    """One Rankings row per measure: '1st' ... '5th' = 'Facility (company) with value'."""
    out = []
    name_i, emitter_i = 0, headers.index("Responsible emitter (company)")
    for label, column in RANKINGS:
        i = headers.index(column)
        numeric = [r for r in rows if isinstance(r[i], (int, float)) and r[i] > 0]
        top5 = sorted(numeric, key=lambda r: -r[i])[:5]
        out.append([f"{label}, {year}"] +
                   [f"{r[name_i]} ({r[emitter_i]}) with {r[i]:,}" for r in top5])
    return out


def main():
    raw_paths = [sys.argv[1]] + sys.argv[3:]   # 2024-25 first, optional 2023-24 second
    out_path = sys.argv[2]

    wb = Workbook()
    wb.remove(wb.active)
    rankings = []
    for path in raw_paths:
        year, headers, rows = read_year(path)
        ws = wb.create_sheet(f"Safeguard {year}")
        ws.append(headers)
        for r in rows:
            ws.append(r)
        rankings += ranking_rows(year, headers, rows)
        print(f"Safeguard {year}: {len(rows)} facility rows x {len(headers)} columns")

    ws = wb.create_sheet("Rankings")
    ws.append(["Ranking", "1st", "2nd", "3rd", "4th", "5th"])
    for r in rankings:
        ws.append(r)
    print(f"Rankings: {len(rankings)} rows")

    ws = wb.create_sheet("Guide")
    ws.append(["Term", "Meaning"])
    for r in GUIDE:
        ws.append(list(r))
    print(f"Guide: {len(GUIDE)} rows")

    wb.save(out_path)
    print("wrote", out_path)


if __name__ == "__main__":
    main()
