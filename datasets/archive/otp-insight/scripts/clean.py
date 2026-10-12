"""Clean BITRE airline on-time performance (OTP) data for the Excel Narrator Agent.

The narrator reads ROW 1 AS THE HEADER and COLUMN A AS THE ROW'S SUBJECT, and
writes one sentence per row ("<Sheet> — <Header A> '<value>': <Header> is <value>; ...").
So every sheet below has a clean one-row header, a meaningful column A, units in
the header names, and percentages rounded to 1 decimal (as in the BITRE report).

Usage (only the first two arguments are required):
    python -I clean.py <BITRE_OTP_2025_Master.xlsx> <out.xlsx>
        [--monthly OTP_Time_Series_Master_Current_LATEST_0.xlsx]
        [--airports WebAirport_CY_1985-2025.xlsx]

Sheets written:
    Route OTP 2025           one row per route x airline (from the annual master)
    Airline Totals 2025      one row per airline, whole network (from the annual master)
    Monthly OTP 2025         one row per airline x measure, one column per month (needs --monthly)
    Airport Passengers 2025  one row per airport, passenger movements (needs --airports)
"""
import argparse
import datetime

import openpyxl

# Shared measure names (units in brackets). BITRE calls one flight a "sector".
# On-time rates are % of flights FLOWN; the cancellation rate is % of flights
# SCHEDULED (BITRE report, Definitions page). "On time" = within 15 minutes.
MEASURES = [
    "Flights Scheduled",
    "Flights Flown",
    "Flights Cancelled",
    "On-Time Departures (flights)",
    "On-Time Arrivals (flights)",
    "Delayed Departures (flights)",
    "Delayed Arrivals (flights)",
    "On-Time Departure Rate (%)",
    "On-Time Arrival Rate (%)",
    "Cancellation Rate (%)",
]

# The master file does not mark routes that were reported for only part of 2025.
# The BITRE report flags these four in its route tables; the monthly time series
# confirms them (Townsville/Cairns has data for January only, Ayers Rock for Feb-Dec).
PARTIAL_YEAR_ROUTES = {
    "Townsville-Cairns": "January 2025 only",
    "Cairns-Townsville": "January 2025 only",
    "Ayers Rock-Melbourne": "February-December 2025 only",
    "Melbourne-Ayers Rock": "February-December 2025 only",
}


def measures(row):
    """Raw columns F..O of a BITRE OTP row: 7 counts, then 3 percentages (rounded to 1 decimal)."""
    counts = list(row[5:12])
    percents = [round(v, 1) for v in row[12:15]]
    return counts + percents


def read_rows(path, sheet=None):
    """Return the data rows (below the header) of a sheet, skipping blank rows."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] is not None]
    wb.close()
    return rows


def add_sheet(wb, title, header, rows):
    ws = wb.create_sheet(title)
    ws.append(header)
    for r in rows:
        ws.append(r)
    ws.freeze_panes = "B2"
    print(f"{title}: {len(rows)} rows")


def airline_name(raw, total_label):
    return total_label if raw == "All Airlines" else raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("master")
    parser.add_argument("out")
    parser.add_argument("--monthly", help="OTP_Time_Series_Master_Current_LATEST_0.xlsx")
    parser.add_argument("--airports", help="WebAirport_CY_1985-2025.xlsx")
    args = parser.parse_args()

    out = openpyxl.Workbook()
    out.remove(out.active)

    # ---- 1 + 2. Annual master: one sheet (misnamed '2024'; every row is Year 2025).
    # Raw columns: A Route, B Departing Port, C Arriving Port, D Airline, E Year,
    # F-L counts, M-O percentages (long floats), P-U empty (dropped by not copying them).
    route_rows, network_rows = [], []
    for r in read_rows(args.master):
        route, dep, arr, airline, year = r[0], r[1], r[2], r[3], r[4]
        assert year == 2025, f"unexpected year {year!r}"
        if route == "All Ports-All Ports":
            # Whole-network total for one airline (includes routes not listed individually).
            name = airline_name(airline, "All airlines (industry total)")
            scope = "Whole domestic network, including routes not listed individually"
            network_rows.append([name, scope] + measures(r))
        else:
            name = airline_name(airline, "All airlines (route total)")
            period = PARTIAL_YEAR_ROUTES.get(route, "Full year 2025")
            route_rows.append([route, name, dep, arr, period] + measures(r))

    add_sheet(out, "Route OTP 2025",
              ["Route", "Airline", "Departing Airport", "Arriving Airport", "Reporting Period"] + MEASURES,
              route_rows)
    add_sheet(out, "Airline Totals 2025", ["Airline", "Scope"] + MEASURES, network_rows)

    # ---- 3. Monthly time series -> one row per (airline, measure), one column per month.
    # This "wide" layout puts all 12 months of a measure into ONE narrated sentence,
    # so "which month was worst?" can be answered from a single retrieved chunk.
    # We keep only the network rows ('All Ports-All Ports') for calendar 2025.
    if args.monthly:
        months = [datetime.datetime(2025, m, 1) for m in range(1, 13)]
        values = {}  # (airline, month) -> list of 10 measure values
        for r in read_rows(args.monthly, "2023-26 OTP"):
            if r[0] == "All Ports-All Ports" and r[4] in months:
                values[(airline_name(r[3], "All airlines (industry total)"), r[4])] = measures(r)
        airlines = sorted({a for a, _ in values}, key=lambda a: (not a.startswith("All airlines"), a))
        rows = []
        for a in airlines:
            for i, measure in enumerate(MEASURES):
                rows.append([a, measure] + [values[(a, m)][i] for m in months])
        add_sheet(out, "Monthly OTP 2025",
                  ["Airline", "Measure"] + [m.strftime("%B %Y") for m in months], rows)

    # ---- 4. Airport passenger movements, calendar year 2025.
    # Raw sheet 'Airport Passengers' has 3 title rows, 2 group-header rows and the real
    # header in row 7: A helper key, B AIRPORT, C Year, D Rank, E-G domestic in/out/total,
    # H-J international in/out/total, K-M total in/out/total. Data starts in row 8.
    # A "movement" is one arrival or departure: a domestic passenger counts twice.
    if args.airports:
        wb = openpyxl.load_workbook(args.airports, read_only=True, data_only=True)
        rows = []
        for r in wb["Airport Passengers"].iter_rows(min_row=8, values_only=True):
            if r[2] != 2025 or not r[1]:
                continue
            name = "Total Australia (all airports)" if r[1] == "TOTAL AUSTRALIA" else r[1].title()
            name = name.replace("Mcarthur", "McArthur")
            rank = r[3] if isinstance(r[3], int) else None  # '-' = not ranked (< 50,000 movements)
            rows.append([name, rank, r[6], r[9], r[12]])
        wb.close()
        add_sheet(out, "Airport Passengers 2025",
                  ["Airport", "Rank by Passenger Movements (1 = busiest)",
                   "Domestic Passenger Movements (arrivals + departures)",
                   "International Passenger Movements (arrivals + departures)",
                   "Total Passenger Movements (arrivals + departures)"],
                  rows)

    out.save(args.out)
    print("saved", args.out)


if __name__ == "__main__":
    main()
