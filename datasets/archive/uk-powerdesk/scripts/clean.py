#!/usr/bin/env python3
"""Clean DUKES 2026 Table 5.11 ("Power stations in the United Kingdom") for the Excel Narrator Agent.

The reference narrator (services/excel_agent/narrator.py) reads each sheet like this:
  * row 1 is the header row,
  * column A is the "subject" of each row,
  * every other row becomes ONE sentence:
      "<sheet> — <A1> '<A>': <header 2> is <value>; <header 3> is <value>; ..."

The raw workbook (28 sheets) does not fit that shape:
  * every table starts with 5 rows of title/accessibility text, so the header is in row 6;
  * column A of the station list is the company, not the station;
  * one sheet ('5.11 Summary') holds two tables;
  * 22 archive sheets (DUKES 2004-2025) would add about 4,000 chunks of old lists;
  * cells carry note markers like 'Coal [note 20]', and two of these markers point at the wrong note.

This script keeps 4 useful tables (one sheet each), puts the subject in column A, turns note
markers into readable 'Notes' text, and adds a short 'About this data' sheet.

Usage (run from a directory that is NOT the download folder):
    python -I clean.py <DUKES_5.11.xlsx> <out.xlsx>
"""

import re
import sys

import openpyxl

NOTE_MARKER = re.compile(r"\s*\[note (\d+)\]", re.IGNORECASE)

# Two markers in the source point at the wrong note. Fix the cell text before reading markers.
MARKER_FIXES = {
    "Viking [note 27]": "Viking [note 29]",  # note 27 is about Dungeness B; the Viking note is note 29
    "Closed [note 29]": "Closed",  # note 29 is the Viking note; it does not belong to these closures
}

# The source splits technology over two columns ('Technology' + 'Type'); merge them into one label.
TECHNOLOGY = {
    ("Nuclear", "AGR"): "Nuclear, advanced gas-cooled reactor (AGR)",
    ("Nuclear", "PWR"): "Nuclear, pressurised water reactor (PWR)",
    ("Fossil Fuel", "CCGT"): "Combined cycle gas turbine (CCGT)",
    ("Fossil Fuel", "Single cycle"): "Fossil fuel, single cycle",
    ("Fossil Fuel", "Conventional steam"): "Fossil fuel, conventional steam",
    ("Bioenergy", "Bioenergy"): "Bioenergy",
    ("Pumped Hydro", "Pumped hydro"): "Pumped hydro storage",
    ("Wind", "Offshore"): "Offshore wind",
    ("Wind", "Onshore"): "Onshore wind",
    ("Solar", "PV"): "Solar PV",
    ("Hydro", "Large Hydro"): "Large hydro",
    ("Hydro", "Small Hydro"): "Small hydro",
}
FUEL_NAMES = {"MSW": "Municipal solid waste (MSW)", "Diesel/Gas Oil": "Diesel/gas oil"}
REGION_FIXES = {"North west": "North West"}  # one row is spelled differently

ABOUT = [
    ("Source", "Digest of UK Energy Statistics (DUKES) 2026, Table 5.11 Power stations in the United Kingdom, "
               "Department for Energy Security and Net Zero (DESNZ), published 30 July 2026. Stations, capacities "
               "and interconnectors are as at the end of May 2026. Crown copyright, Open Government Licence v3.0"),
    ("Scope of the station list", "The 'Power stations' sheet lists stations owned or operated by Major Power "
               "Producers (MPPs). Other power stations, including many renewable sites, rooftop solar and "
               "auto-generators, are only counted in the 'Capacity by fuel' totals. Locations and connection "
               "details are as supplied by the generators and are not verified by DESNZ"),
    ("Company", "The company that completes the Major Power Producer survey return. Often the site owner, "
               "but it may be the company that operates the site"),
    ("Installed capacity", "Megawatts (MW) operational at the end of May 2026. Wind farms still being built "
               "(for example Dogger Bank, due to expand further in 2026) appear with only their operating part"),
    ("Grid connection", "'HV' or 'LV' exactly as supplied by the generator. The workbook does not define the "
               "codes. Most large plants are HV, but some large gas plants (for example Great Yarmouth, 420 MW) "
               "are listed as LV"),
    ("Abbreviations", "CCGT = combined cycle gas turbine; OCGT = open cycle gas turbine; AGR = advanced "
               "gas-cooled reactor; PWR = pressurised water reactor; PV = photovoltaic; CHP = combined heat and "
               "power; MSW = municipal solid waste; MPP = major power producer; DNC = declared net capacity"),
    ("Capacity by fuel", "Totals at the end of May 2026 for major power producers, other generators and all "
               "generators. MPP totals include some sites operated by MPPs that are not in the MPP survey"),
    ("Station changes", "Major power producers' thermal capacity opened, closed, converted, increased or "
               "reduced since the end of 2010 (coal, gas, oil, nuclear, biomass and waste plants)"),
    ("Interconnectors", "Cables linking the UK grid with other countries, with capacity in MW and the year of "
               "first operation. Electricity flows by country are in the DUKES Chapter 5 text and Table 5.13"),
    ("Name clash: Viking", "Three different assets are called Viking: 'Viking Wind Farm', an onshore wind "
               "farm in Scotland (SSE Group, 443 MW); 'Viking', a 50 MW single-cycle gas plant in North East "
               "England (Whitetower Energy Ltd); and 'Viking' in the Interconnectors sheet, the England - Denmark "
               "cable (1,400 MW)"),
    ("Drax capacity", "The station list shows 2,580 MW for 'Drax Power Station Biomass Units'. The 'Station "
               "changes' sheet and DUKES note 20 give 2,640 MW (four converted 660 MW units). Both figures "
               "come from DESNZ"),
]


def read_rows(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


def is_blank(row):
    return all(v is None or str(v).strip() == "" for v in row)


def split_notes(value, notes):
    """'Coal [note 20]' -> ('Coal', ['<text of note 20>']). Non-text values pass through unchanged."""
    if not isinstance(value, str):
        return value, []
    text = MARKER_FIXES.get(value.strip(), value.strip())
    found = [notes[n] for n in NOTE_MARKER.findall(text) if n in notes]
    return NOTE_MARKER.sub("", text).strip(), found


def number(value, decimals):
    """Round numbers stored with many decimals; turn '2015' (text) into 2015."""
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    if isinstance(value, float):
        value = round(value, decimals)
        return int(value) if value.is_integer() else value
    return value


def power_stations(rows, out):
    header = [str(h) for h in rows[5]]  # the real header is row 6
    col = {name: header.index(name) for name in header}
    ws = out.create_sheet("Power stations")
    ws.append(["Site name", "Company", "Technology", "Primary fuel", "Secondary fuel", "CHP plant",
               "Installed capacity (MW)", "Grid connection", "Country", "Region", "Year commissioned"])
    stations = []
    for r in rows[6:]:
        if is_blank(r):
            continue
        tech_source = r[col["Technology"]].strip()
        technology = TECHNOLOGY[(tech_source, r[col["Type"]].strip())]
        fuel = r[col["Primary Fuel"]].strip()
        # For wind, solar, hydro and nuclear the fuel just repeats the technology, so leave it out.
        fuel = FUEL_NAMES.get(fuel, fuel) if tech_source in ("Fossil Fuel", "Bioenergy") else None
        region = r[col["Region"]].strip()
        stations.append([
            r[col["Site Name"]].strip(),
            r[col["Company Name [note 30]"]].strip(),
            technology,
            fuel,
            r[col["Secondary Fuel"]],  # only 4 stations have one
            "Yes" if r[col["CHP"]].strip() == "Yes" else None,  # blank = not a CHP plant
            number(r[col["InstalledCapacity (MW)"]], 2),
            r[col["Grid Connection Type"]].strip(),
            r[col["Country"]].strip(),
            REGION_FIXES.get(region, region),
            number(r[col["Year Commissioned"]], 0),
        ])
    # Postcode, OS reference, X/Y coordinates and the DESNZ site code are dropped (not useful in text).
    # Sort by technology, then largest first, so similar plants share chunks and the biggest comes first.
    order = list(TECHNOLOGY.values())
    stations.sort(key=lambda s: (order.index(s[2]), -s[6]))
    for s in stations:
        ws.append(s)
    return len(stations)


def station_changes(rows, notes, out):
    ws = out.create_sheet("Station changes")
    ws.append(["Site", "Year", "Fuel or plant type", "Change", "Capacity before (MW)", "Capacity after (MW)",
               "Notes"])
    kept = 0
    for r in rows[6:]:  # header is row 6
        if is_blank(r):
            continue
        year, site, fuel, status, before, after, note = (list(r) + [None] * 7)[:7]
        fuel, fuel_notes = split_notes(fuel, notes)
        status, status_notes = split_notes(status, notes)
        if isinstance(note, str) and note.strip():
            note = note.strip().rstrip(".")  # keep the note written in the source row
        else:
            note = ". ".join(fuel_notes + status_notes)  # otherwise use the DUKES note(s) the markers point to
        ws.append([site.strip(), year, fuel, status, number(before, 2), number(after, 2), note or None])
        kept += 1
    return kept


def summary_tables(rows, notes, out):
    """'5.11 Summary' holds Table 5.11.A (capacity by fuel) and Table 5.11.B (interconnectors)."""
    start_a = next(i for i, r in enumerate(rows) if r[0] == "Generator Type")
    start_b = next(i for i, r in enumerate(rows) if r[0] == "Interconnector")

    ws = out.create_sheet("Capacity by fuel")
    ws.append(["Generator group and fuel", "Installed capacity at end of May 2026 (MW)", "Notes"])
    kept_a = 0
    for r in rows[start_a + 1:start_b - 1]:  # stop before the title row of Table 5.11.B
        if is_blank(r):
            continue
        group, group_notes = split_notes(r[0], notes)
        fuel, fuel_notes = split_notes(r[1], notes)
        group = group.capitalize().replace("producer", "producers")  # 'Other Generators' -> 'Other generators'
        ws.append([f"{group} - {fuel}", number(r[2], 1), ". ".join(group_notes + fuel_notes) or None])
        kept_a += 1

    ws = out.create_sheet("Interconnectors")
    ws.append(["Interconnector", "Connects", "Capacity (MW)", "Year of first operation", "Notes"])
    kept_b = 0
    for r in rows[start_b + 1:]:
        if is_blank(r):
            break
        name, name_notes = split_notes(r[0], notes)
        name = name.replace("Interconnecion", "Interconnexion")  # typo in the source
        connects = re.sub(r"\s*-\s*", " - ", r[1].strip())  # 'England-France' -> 'England - France'
        ws.append([name, connects, number(r[2], 0), number(r[3], 0), ". ".join(name_notes) or None])
        kept_b += 1
    return kept_a, kept_b


def main():
    raw_path, out_path = sys.argv[1], sys.argv[2]
    raw = openpyxl.load_workbook(raw_path, read_only=True, data_only=True)
    # DUKES notes: 'Note 20' -> text. Used to turn markers such as '[note 20]' into readable notes.
    notes = {}
    for r in read_rows(raw["Notes"])[4:]:
        if r[0] and str(r[0]).strip().lower().startswith("note "):
            notes[str(r[0]).split()[1]] = str(r[1]).strip().rstrip(".")  # the narrator adds its own full stop

    out = openpyxl.Workbook()
    out.remove(out.active)
    n_stations = power_stations(read_rows(raw["5.11 Full list"]), out)
    n_changes = station_changes(read_rows(raw["Changed and converted stations"]), notes, out)
    n_fuel, n_links = summary_tables(read_rows(raw["5.11 Summary"]), notes, out)
    about = out.create_sheet("About this data")
    about.append(["Topic", "Explanation"])
    for topic, text in ABOUT:
        about.append([topic, text])
    # The archive sheets 'DUKES 2004' ... 'DUKES 2025', the cover, contents and notes sheets are not copied.
    out.save(out_path)
    print(f"Power stations: {n_stations} | Station changes: {n_changes} | Capacity by fuel: {n_fuel} | "
          f"Interconnectors: {n_links} | About this data: {len(ABOUT)}")
    print(f"saved {out_path}")


if __name__ == "__main__":
    main()
