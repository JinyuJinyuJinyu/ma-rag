"""Clean AEMO's "NEM Generation Information - July 2026" workbook for the Excel Narrator.

Usage:
    python -I clean.py nem-generation-information-july-2026.xlsx nem-gridwatch_clean.xlsx

The Excel Narrator reads ROW 1 AS THE HEADER and COLUMN A AS THE ROW'S SUBJECT, and writes one
sentence per row. The raw workbook does not fit that: the data sheet has its header in row 4,
column A is an internal survey ID, there are 165 columns (89 of them empty), and every generating
UNIT is its own row (Gladstone power station = 6 near-identical rows). This script:

  1. Reads the 'Generator Information' sheet (header = row 4).
  2. Keeps units that exist or are firmly in the pipeline (In Service, In Commissioning, Announced
     Withdrawal, Committed, Committed*, Anticipated). Drops the 893 speculative "Publicly Announced"
     units and the 1 "Withdrawn" unit from the station sheets (they still count in the totals).
  3. Drops "Non-scheduled" units (small plants, 118 units / 1,542 MW in that scope). AEMO's own
     summary table and the ESOO (Tables 7 and 9) count only scheduled, semi-scheduled and
     significant non-scheduled units, so with this rule every total matches the report.
  4. Groups units into one row per station + status + technology (+ notified closure date), with
     total MW and MWh and a readable unit list such as "2 x 350 + 2 x 375".
  5. Fixes one mislabelled site name (see SITE_NAME_FIXES below).
  6. Writes readable values: state names instead of NSW1, ISO dates, OCGT/CCGT spelled out,
     far-future closure years (2100 or later) marked as placeholders.
  7. Adds two summary sheets (capacity by technology, capacity by state = ESOO Table 7) with the
     largest station / project per technology, because similarity search cannot find a "largest"
     value across hundreds of rows, and a Glossary sheet (AEMO's status definitions + notes).
"""

import sys
from collections import Counter, defaultdict
from datetime import datetime

import openpyxl
from openpyxl.styles import Font

STATES = {"NSW1": "New South Wales", "QLD1": "Queensland", "SA1": "South Australia",
          "TAS1": "Tasmania", "VIC1": "Victoria"}
EXISTING = ["In Service", "In Commissioning", "Announced Withdrawal"]
PIPELINE = ["Committed", "Committed*", "Anticipated"]
TECHNOLOGIES = ["Coal", "Gas Turbine", "Hydro", "Wind", "Solar PV", "Battery Storage", "Other"]
DETAIL_NAMES = {"OCGT": "Open cycle gas turbine (OCGT)", "CCGT": "Combined cycle gas turbine (CCGT)"}

# One known error in the July 2026 file: the in-service John Butters hydro unit (Tasmania, owner
# Hydro-Electric Corporation, DUID JBUTTERS) carries the site name of a Victorian battery
# project, "Joel Joel BESS 2 - KCI". Fix it by its AEMO Survey ID.
SITE_NAME_FIXES = {1097: "John Butters"}

STATION_HEADERS = ["Technology", "Technology detail", "Gas turbine fuel", "Dispatch type", "Status",
                   "Units (number x MW AC each)", "Capacity (MW AC)", "Storage (MWh)",
                   "Full commercial use date", "Expected closure year", "Notified closure date"]


def read_units(path):
    """Return the 'Generator Information' rows as dicts keyed by the row-4 header names."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb["Generator Information"].iter_rows(values_only=True))
    wb.close()
    header = [str(h).strip() if h is not None else None for h in rows[3]]  # row 4
    units = []
    for row in rows[4:]:
        if all(v is None or str(v).strip() == "" for v in row):
            continue  # skip blank lines
        units.append({h: v for h, v in zip(header, row) if h})
    return units


def text(value):
    """Strip text cells; turn empty strings into None so the narrator skips them."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def iso_date(value):
    return value.date().isoformat() if isinstance(value, datetime) else None


def closure_year(year):
    """AEMO uses far-future years (2100, 2129, 2200) for assets with no planned closure."""
    if year is None:
        return None
    if year >= 2100:
        return f"{year} (far-future placeholder, not a real closure plan)"
    return year


def join_unique(values):
    """One value -> that value; several different values -> 'a, b, c'; none -> None."""
    unique = sorted({v for v in values if v is not None}, key=str)
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return ", ".join(str(v) for v in unique)


def group_into_stations(units):
    """One row per (site, status, technology, detail, notified closure date): sums MW and MWh and
    lists unit sizes. The closure date is part of the key so that units retiring on different
    dates (Torrens Island B unit 1 in 2026, units 2-4 in 2028) stay on separate rows."""
    groups = defaultdict(list)
    for u in units:
        key = (u["Site Name"], u["Commitment Status"], u["Technology Type"], u["Technology Detail"],
               iso_date(u["Closure Date"]))
        groups[key].append(u)

    stations = []
    for (site, status, tech, detail, _), members in sorted(groups.items(), key=lambda kv: [str(k) for k in kv[0]]):
        sizes = Counter()  # unit size (MW AC) -> number of turbines / inverters / units of that size
        for u in members:
            if u["Unit Capacity (MW AC)"]:
                sizes[round(u["Unit Capacity (MW AC)"], 2)] += u["Unit Count"]
        storage = [u["Agg Nameplate Storage Capacity (MWh)"] for u in members if u["Agg Nameplate Storage Capacity (MWh)"]]
        stations.append({
            "name": site,
            "Owner": join_unique(text(u["Site Owner"]) for u in members),
            "State": STATES[members[0]["Region"]],
            "Technology": tech,
            "Technology detail": DETAIL_NAMES.get(detail, detail),
            "Gas turbine fuel": join_unique(text(u["Gas Turbine Fuel Type"]) for u in members),
            "Dispatch type": join_unique(u["Dispatch Type"] for u in members),
            "Status": status,
            "Units (number x MW AC each)": " + ".join(f"{n} x {mw:g}" for mw, n in sorted(sizes.items())) or None,
            "Capacity (MW AC)": round(sum(u["Aggregated Nameplate Capacity (MW AC)"] or 0 for u in members), 2),
            "Storage (MWh)": round(sum(storage), 2) if storage else None,
            "Full commercial use date": join_unique(iso_date(u["Full Commercial Use Date"]) for u in members),
            "Expected closure year": join_unique(closure_year(u["Expected Closure Year"]) for u in members),
            "Notified closure date": join_unique(iso_date(u["Closure Date"]) for u in members),
        })
    return stations


def describe(station):
    return f"{station['name']} ({station['State']}), {station['Capacity (MW AC)']:g} MW, {station['Status']}"


def largest(stations, tech=None):
    """Largest station by MW (ties listed together); tech=None means all technologies."""
    pool = [s for s in stations if tech is None or s["Technology"] == tech]
    if not pool:
        return None
    top = max(s["Capacity (MW AC)"] for s in pool)
    return " and ".join(describe(s) for s in pool if s["Capacity (MW AC)"] == top)


def read_status_definitions(path):
    """AEMO's 'Further description of Commitment Status' table on the Background Information sheet."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = [list(r) + [None, None, None] for r in wb["Background Information"].iter_rows(values_only=True)]
    wb.close()
    start = next(i for i, r in enumerate(rows) if r[1] == "Commitment Status" and r[2] == "Description")
    definitions = []
    for r in rows[start + 1:]:
        if not text(r[1]):
            break
        meaning = " ".join(str(r[2]).split()).rstrip(".")  # collapse double spaces / line breaks
        definitions.append((f"Status: {text(r[1])}", meaning))
    return definitions


def main(raw_path, out_path):
    units = read_units(raw_path)
    for u in units:  # tidy names: fix the known error, drop stray spaces and trailing full stops
        u["Site Name"] = SITE_NAME_FIXES.get(u["Survey ID"], str(u["Site Name"]).strip().rstrip("."))
        u["Technology Detail"] = text(u["Technology Detail"])

    in_scope = [u for u in units if u["Dispatch Type"] != "Non-scheduled"]
    existing = group_into_stations([u for u in in_scope if u["Commitment Status"] in EXISTING])
    pipeline = group_into_stations([u for u in in_scope if u["Commitment Status"] in PIPELINE])

    # ---- Capacity by technology (same numbers as AEMO's Summary sheet) ----
    mw = defaultdict(float)  # (status, technology) -> MW AC
    for u in in_scope:
        mw[(u["Commitment Status"], u["Technology Type"])] += u["Aggregated Nameplate Capacity (MW AC)"] or 0
    tech_headers = ["Technology", "Existing incl. in commissioning (MW)", "In service (MW)", "In commissioning (MW)",
                    "Announced withdrawal (MW)", "Committed (MW)", "Committed* (MW)", "Anticipated (MW)",
                    "Publicly announced (MW)", "Largest existing station", "Largest pipeline project"]
    tech_rows = []
    for tech in TECHNOLOGIES + ["All technologies"]:
        techs = TECHNOLOGIES if tech == "All technologies" else [tech]
        total = lambda statuses: round(sum(mw[(s, t)] for s in statuses for t in techs), 1)  # 1 decimal, as AEMO
        tech_rows.append([tech, total(EXISTING), total(["In Service"]), total(["In Commissioning"]),
                          total(["Announced Withdrawal"]), total(["Committed"]), total(["Committed*"]),
                          total(["Anticipated"]), total(["Publicly Announced"]),
                          largest(existing, None if tech == "All technologies" else tech),
                          largest(pipeline, None if tech == "All technologies" else tech)])

    # ---- Capacity by state (same layout and numbers as ESOO 2026 Table 7) ----
    state_mw = defaultdict(float)
    category = {"In Service": "existing", "In Commissioning": "existing", "Announced Withdrawal": "existing",
                "Committed": "committed", "Committed*": "committed",
                "Anticipated": "anticipated", "Publicly Announced": "proposed"}
    for u in in_scope:
        if u["Commitment Status"] in category:
            state_mw[(STATES[u["Region"]], category[u["Commitment Status"]])] += u["Aggregated Nameplate Capacity (MW AC)"] or 0
    state_headers = ["State", "Existing and in commissioning (MW)", "Committed incl. Committed* (MW)",
                     "Anticipated (MW)", "Proposed, publicly announced (MW)"]
    state_rows = []
    for state in sorted(STATES.values()) + ["NEM total"]:
        states = sorted(STATES.values()) if state == "NEM total" else [state]
        state_rows.append([state] + [round(sum(state_mw[(s, c)] for s in states))
                                     for c in ("existing", "committed", "anticipated", "proposed")])

    # ---- Glossary: AEMO's own status definitions + notes on this workbook ----
    glossary = read_status_definitions(raw_path) + [
        ("Full commercial use date", "the date the unit is expected to be (or was) approved for full "
         "operation in the NEM after all commissioning tests"),
        ("Expected closure year", "the year the owner expects to close the station. Years of 2100 or "
         "later are far-future placeholders, not real closure plans (for example the Callide C coal "
         "station shows 2200 and Snowy 2.0 shows 2129)"),
        ("Notified closure date", "a firm closure date, available only when the owner has formally "
         "notified AEMO under National Electricity Rules clause 2.10.1"),
        ("Units (number x MW AC each)", "the turbines, inverters or generating units at the station and "
         "the size of each, for example 2 x 350 + 2 x 375 for Yallourn W"),
        ("Dispatch type", "how the station takes part in central dispatch: scheduled, semi-scheduled "
         "(mostly wind and solar) or significant non-scheduled"),
        ("Scope of this workbook", "stations and projects from AEMO's NEM Generation Information, July "
         "2026, that are in service, in commissioning, announced for withdrawal, committed or anticipated. "
         f"Small non-scheduled units ({sum(1 for u in units if u['Dispatch Type'] == 'Non-scheduled' and u['Commitment Status'] in EXISTING + PIPELINE)} units) "
         "are left out, as in AEMO's summary table and the 2026 ESOO. Publicly announced projects are "
         "counted only in the capacity totals"),
        ("KCI", "site names ending in KCI come from AEMO's Key Connection Information list of projects"),
    ]

    # ---- Write the workbook ----
    out = openpyxl.Workbook()
    out.remove(out.active)
    sheets = [
        ("Existing fleet", ["Station", "Owner", "State"] + STATION_HEADERS,
         [[s["name"], s["Owner"], s["State"]] + [s[h] for h in STATION_HEADERS] for s in existing]),
        ("Pipeline projects", ["Project", "Owner", "State"] + STATION_HEADERS,
         [[s["name"], s["Owner"], s["State"]] + [s[h] for h in STATION_HEADERS] for s in pipeline]),
        ("Capacity by technology", tech_headers, tech_rows),
        ("Capacity by state", state_headers, state_rows),
        ("Glossary", ["Term", "Meaning"], [list(g) for g in glossary]),
    ]
    for title, headers, rows in sheets:
        ws = out.create_sheet(title)
        ws.append(headers)  # row 1 = header
        for row in rows:
            ws.append(row)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "B2"
    out.save(out_path)
    print(f"wrote {out_path}: " + ", ".join(f"{t} ({len(r)} rows)" for t, _, r in sheets))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -I clean.py <raw generation information.xlsx> <out.xlsx>")
    main(sys.argv[1], sys.argv[2])
