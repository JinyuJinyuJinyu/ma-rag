# OTP Insight: Australian Airline Punctuality Copilot (starter kit)

A multi-agent RAG copilot for an airline or airport operations analyst. It explains 2025
domestic punctuality (on-time arrivals and departures, and cancellations) by airline, route,
month and airport. Sources: the BITRE annual report (PDF) and three BITRE workbooks (XLSX).

Verified on 2026-10-06 by re-downloading every file (sha256 below) and rebuilding the workbook.

## 1. Sources

| Role | File | URL (final, no redirect) | Size | sha256 (first 16) |
|---|---|---|---|---|
| XLSX (required) | `BITRE_OTP_2025_Master.xlsx` | https://www.bitre.gov.au/sites/default/files/documents/BITRE_OTP_2025_Master.xlsx | 69,669 B | `534ccf9bf9c0aa6d` |
| XLSX (monthly, recommended) | `OTP_Time_Series_Master_Current_LATEST_0.xlsx` | https://www.bitre.gov.au/sites/default/files/documents/OTP_Time_Series_Master_Current_LATEST_0.xlsx | 10.1 MB | `3751779cf1ac2f8b` |
| XLSX (airports, recommended) | `WebAirport_CY_1985-2025.xlsx` | https://www.bitre.gov.au/sites/default/files/documents/WebAirport_CY_1985-2025.xlsx | 1.16 MB | `82a5d05bff15063b` |
| PDF | `BITRE_OTP_Report_2025.pdf` | https://www.bitre.gov.au/sites/default/files/documents/BITRE_OTP_Report_2025.pdf | 1.80 MB, 32 pages | `b3f998296e4e0a03` |

Landing page: https://www.bitre.gov.au/resource/aviation/airline-time-performance-annual-reports
(release date 6 March 2026).

The time-series file name ends in `LATEST`. BITRE overwrites it every month; the copy used here
runs from January 2023 to August 2026. Its 2025 monthly rows add up exactly to the annual
master (489 of 489 route-airline keys match), so the 2025 numbers were stable when checked.

### Licence and attribution
- **PDF**, page 2: "With the exception of (a) the Coat of Arms; and (b) the Department of
  Infrastructure ... photos and graphics, copyright in this publication is licensed under a
  Creative Commons Attribution 4.0 Australia Licence." The suggested citation is "Bureau of
  Infrastructure and Transport Research Economics (BITRE), 2026, Domestic airline on time
  performance 2025, Statistical Report, BITRE, Canberra ACT."
- **XLSX**: the workbooks contain no licence text. The airport workbook's Notes sheet says
  "© Commonwealth of Australia ... should not be reproduced or used in any form without
  acknowledgement". The department's website copyright page
  (https://www.infrastructure.gov.au/copyright) says you are "free to use information available
  on the department's website, provided you comply with the Copyright Act 1968". It applies
  CC BY 4.0 International where material carries a CC logo, and asks for attribution as
  "© Commonwealth of Australia".
- **Commercial use**: allowed (CC BY, no NC clause). **Attribution**: required.
- Attribution text for the app or slides: *"Source: Bureau of Infrastructure and Transport
  Research Economics (BITRE), Airline On Time Performance 2025 and Airport Traffic Data
  1985-2025. © Commonwealth of Australia, CC BY 4.0."*

## 2. Rebuild the cleaned workbook

```bash
cd datasets/archive/otp-insight
python -I scripts/clean.py \
    raw/BITRE_OTP_2025_Master.xlsx \
    otp-insight_clean.xlsx \
    --monthly  raw/OTP_Time_Series_Master_Current_LATEST_0.xlsx \
    --airports raw/WebAirport_CY_1985-2025.xlsx
```
The script needs only `openpyxl`, which the reference repo already requires. With just the
first two arguments it builds the two sheets from the annual master.

## 3. What was wrong with the raw files, and what clean.py does

Annual master (`BITRE_OTP_2025_Master.xlsx`, one sheet):
1. The sheet is named `2024`, but every row has Year = 2025. It is renamed `Route OTP 2025`
   and the constant Year column is dropped.
2. Columns P to U are empty, so the narrator would emit `Column16`...`Column21`. They are dropped.
3. Headers contain line breaks (`OnTime Arrivals \n(%)`). They are rewritten with units.
4. Percentages are raw floats (`78.26797385620915`). They are rounded to 1 decimal, as in the PDF.
5. The sheet ends with 64 blank rows. The narrator skips them; clean.py also ignores them.
6. The nine `All Ports-All Ports` rows are whole-network totals. They include routes that are
   not listed (listed routes are only 75.8% of scheduled flights, and Hinterland and Skytrans
   fly no listed route). They move to their own sheet, `Airline Totals 2025`, with a Scope
   column that says so.
7. `All Airlines` is ambiguous. It becomes `All airlines (route total)` on routes and
   `All airlines (industry total)` on the network sheet.
8. **Partial-year routes are not flagged in the XLSX.** The PDF route tables mark
   Townsville-Cairns and Cairns-Townsville as "Jan 2025 only", and Ayers Rock-Melbourne and
   Melbourne-Ayers Rock as "Feb-Dec 2025 only". The monthly file confirms this. A
   `Reporting Period` column records it. Without it, Townsville-Cairns (90.9%, 134 flights in
   January) looks like the best route of the year; the PDF names Emerald-Brisbane (89.8%) as
   best among full-year routes.

Monthly time series (`2023-26 OTP` sheet): only the 108 network rows for 2025 are kept
(9 airline entities x 12 months). They are pivoted to one row per airline and measure, with
one column per month, so one narrated sentence holds all 12 months. "Which month was worst?"
then needs only one retrieved chunk; the reference retriever uses top_k = 5.

Airport workbook (`Airport Passengers` sheet): the real header is row 7, below 3 title rows
and 2 merged group-header rows, and column A is a helper key (`SYDNEY2025`). Only Year = 2025
is kept. Names are title-cased, the `-` rank placeholder becomes blank (BITRE ranks only
airports with 50,000 or more movements), and inbound and outbound are dropped in favour of
totals. **Values are passenger movements, not passengers**: each domestic passenger counts
twice (a departure and an arrival), so the headers say "(arrivals + departures)".

## 4. Cleaned workbook: `otp-insight_clean.xlsx`

| Sheet | Column A (subject) | Rows | Narrated chars | Chunks |
|---|---|---|---|---|
| Route OTP 2025 | Route (directional, e.g. `Canberra-Sydney`) + Airline | 480 | 233,687 | 241 |
| Airline Totals 2025 | Airline (8 airlines + industry total) | 9 | 4,442 | 6 |
| Monthly OTP 2025 | Airline + Measure; columns January ... December 2025 | 90 | 30,041 | 31 |
| Airport Passengers 2025 | Airport (100 airports + national total) | 101 | 27,190 | 28 |
| **Total** | | **680** | **295,366** | **306** |

Chunk counts are exact: the reference narrator (LLM summary off) followed by the reference
LangChain `RecursiveCharacterTextSplitter` (1200 characters, 240 overlap, splitting on newlines
first, so whole row sentences stay together). A quick stride-based estimate gives 308.
The PDF adds about 61k characters (about 64 chunks), for a project total of about 370 chunks.

Narration samples (the reference narrator with the LLM summary off):
```
Route OTP 2025 — Route 'Canberra-Sydney': Airline is QantasLink; Departing Airport is Canberra; Arriving Airport is Sydney; Reporting Period is Full year 2025; Flights Scheduled is 4220; Flights Flown is 3884; Flights Cancelled is 336; On-Time Departures (flights) is 2764; On-Time Arrivals (flights) is 2705; Delayed Departures (flights) is 1120; Delayed Arrivals (flights) is 1179; On-Time Departure Rate (%) is 71.2; On-Time Arrival Rate (%) is 69.6; Cancellation Rate (%) is 8.
Monthly OTP 2025 — Airline 'All airlines (industry total)': Measure is On-Time Arrival Rate (%); January 2025 is 75.8; February 2025 is 80.3; March 2025 is 80.2; April 2025 is 82.4; May 2025 is 82.2; June 2025 is 79; July 2025 is 73; August 2025 is 74.6; September 2025 is 78.9; October 2025 is 74.1; November 2025 is 69; December 2025 is 73.8.
Airport Passengers 2025 — Airport 'Emerald': Rank by Passenger Movements (1 = busiest) is 36; Domestic Passenger Movements (arrivals + departures) is 189805; International Passenger Movements (arrivals + departures) is 0; Total Passenger Movements (arrivals + departures) is 189805.
```
The narrator prints whole-number floats without a decimal point (`8.0` becomes `8`, `69.0`
becomes `69`). This is expected.

## 5. Data caveats (tell the answer agent)
- **The PDF repeats the route tables.** PDF pages 13-25 (printed pages 9-21) list every route
  x airline row of the master XLSX, with the same numbers, and PDF page 7 lists the airline
  totals. Route and airline-total questions can be answered from either source. The facts
  only the XLSX has are the monthly figures (the PDF has charts with no values) and airport
  passenger movements. Use those for the before/after A2A demo.
- Routes are directional: `Canberra-Sydney` (6.2% cancelled) and `Sydney-Canberra` (5.6%) are
  different rows. BITRE's "60 routes" are 60 city pairs, which make 120 directional entries.
- Small samples: 42 airline-route rows cover fewer than 12 months (for example QantasLink
  Adelaide-Gold Coast has 9 flights in one month), so their percentages are unstable. Ranking
  questions should use the `All airlines (route total)` rows.
- Skytrans network row: on-time plus delayed flights (5,695) exceed flights flown (5,674) by
  21. This publisher inconsistency carries into the industry total. The rates are unaffected
  (they are computed from flights flown).
- Only routes averaging more than 8,000 passengers a month with 2 or more competing airlines
  are listed. Reporting airlines carry over 98% of domestic passengers (PDF page 3).
- "On time" means within 15 minutes of schedule. Cancellations are flights removed within 7
  days of departure (PDF page 32).

## 6. Verified demo answer key

Every answer was checked against `otp-insight_clean.xlsx` and the PDF text.

| # | Question | Needs | Expected answer | Evidence |
|---|---|---|---|---|
| 1 | Across all Australian domestic airlines, which month of 2025 had the worst on-time arrival rate, and how bad was it? **(best before/after demo; superlative)** | xlsx | **November 2025: 69.0%** (30,791 of 44,636 flights flown arrived on time). The answer is unique: next worst is July at 73.0%; best is April at 82.4%. | `Monthly OTP 2025`, rows (All airlines (industry total), On-Time Arrival Rate (%)) and (…, On-Time Arrivals (flights)). The PDF has only a chart with no values. |
| 2 | How many flights were cancelled industry-wide in March 2025, and what was the cancellation rate? | xlsx | 2,309 of 46,007 scheduled flights cancelled, **5.0%**, the highest month of 2025 (next is July at 3.1%). | `Monthly OTP 2025`, rows Flights Cancelled / Flights Scheduled / Cancellation Rate (%) for the industry total |
| 3 | How many passenger movements did Emerald Airport handle in 2025, and how did it rank? | xlsx | 189,805 passenger movements, all domestic (0 international); **rank 36** of the 61 ranked airports. | `Airport Passengers 2025`, row `Emerald` |
| 4 | How does BITRE define an on-time arrival and a cancellation? | pdf | An arrival is on time if the flight reaches the gate before 15 minutes after the scheduled arrival time; diverted and cancelled flights never count as on time. A cancellation is a flight removed from service within 7 days of scheduled departure. On-time % is measured against flights operated; cancellation % against flights scheduled. | PDF page 32 (printed page 28), Definitions |
| 5 | The 2025 report names Emerald as the best airport for on-time arrivals. How busy is Emerald compared with Sydney? | both | PDF: Emerald had the highest on-time arrivals (86.2%) and departures (88.4%). XLSX: Emerald had 189,805 passenger movements (rank 36); Sydney had 42,566,527 (rank 1: 25,308,725 domestic + 17,257,802 international), about 224 times as many. | PDF page 6 (printed page 2) and page 7 Table 3; `Airport Passengers 2025` rows `Emerald` and `Sydney` |
| 6 | The report says 2025 on-time arrivals were below the long-term average. By how much, and which month dragged the year down most? | both | PDF: 76.9% in 2025 against a long-term average of 80.5% (since November 2003). XLSX: November 2025 was the worst month at 69.0%. | PDF page 5 (printed page 1); `Monthly OTP 2025` industry On-Time Arrival Rate row |
| 7 | Canberra-Sydney had the highest cancellation rate of any route. Which airline accounted for most of those cancellations? | pdf (the XLSX repeats it) | 474 of 7,659 flights cancelled (6.2%). QantasLink cancelled 336 of 4,220 (8.0%), about 71% of the total; Virgin Australia 107 of 2,936 (3.6%); Qantas 31 of 503 (6.2%). | PDF page 6 (route ranking) and page 17 (printed page 13) route table; same rows in `Route OTP 2025` |
| 8 | Can you recommend a good hotel near Sydney Airport? | off-topic | The relevance guardrail should decline: hotels are outside the airline-punctuality knowledge base. | none |

## 7. PDF notes (Azure Document Intelligence)
- 32 pages, 1.80 MB, text layer present. Page 1 is an image-only cover. Pages 8-12 are charts
  whose extracted text is only axis labels (noise). Pages 13-31 are tables. Page 32 holds the
  definitions.
- Free tier (F0) analyses only the first 2 pages of each request, so split the PDF into
  16 two-page files, or use the S0 tier. The file is under the 4 MB limit.
- The reference extractor uses `result.content`, so table cells arrive as plain text lines.
  Route-table chunks are retrievable but less tidy than the XLSX narration.
