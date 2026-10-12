# NEM GridWatch: Reliability & Generation Pipeline Copilot (starter kit)

A multi-agent RAG assistant for an energy-market or reliability analyst covering Australia's National
Electricity Market (NEM). It answers "why and when" questions from AEMO's **2026 Electricity Statement of
Opportunities (ESOO)** (PDF). It answers "which asset, how big, when" questions from AEMO's **NEM
Generation Information, July 2026** register (XLSX), which is the release the ESOO's supply assumptions
are built on (ESOO section 3.2.1, Table 7).

Verified on 2026-10-06 by re-downloading both files and checking the cleaned workbook against the ESOO.
The cleaned totals reproduce ESOO Table 7 and the closures in Table 9.

## Sources

| File | URL | Size | sha256 |
|---|---|---|---|
| `nem-generation-information-july-2026.xlsx` (raw, in `raw/`) | https://www.aemo.com.au/-/media/files/electricity/nem/planning_and_forecasting/generation_information/2026/nem-generation-information-july-2026.xlsx | 910,557 bytes | `36e64862b715d7edaa736a9bb9c2f73559d63f6b3d1f7afe693e66d7beee038d` |
| 2026 ESOO PDF (124 pages, August 2026) | https://www.aemo.com.au/-/media/files/electricity/nem/planning_and_forecasting/nem_esoo/2026/2026-electricity-statement-of-opportunities.pdf | 4,706,708 bytes (4.49 MB) | `d1fd365ffb8574ae516c7d3694bad92d1825bc21a06f4318a1842f79af6f4d56` |

- Both direct `/-/media/` links download with `curl -L --fail -A "Mozilla/5.0"`. AEMO's HTML pages (the
  Generation Information page and the copyright page) return HTTP 403 with a Cloudflare challenge when
  fetched by a script, so open those in a browser.
- AEMO replaces the Generation Information workbook every quarter. Keep this July 2026 file, the one
  the 2026 ESOO uses, and check its sha256 above.

## Licence and attribution

- In both files: "© 2026 Australian Energy Market Operator Limited. The material in this publication may
  be used in accordance with the copyright permissions on AEMO's website." This is on the XLSX
  `Disclaimer` sheet and on page 2 of the ESOO.
- AEMO's Copyright Permissions page (https://aemo.com.au/privacy-and-legal-notices/copyright-permissions),
  as indexed by web search because the page blocks scripts: AEMO gives "general permission for anyone to
  use AEMO Material for any purpose, but only with accurate and appropriate attribution of the relevant
  AEMO Material and AEMO as its author". Confidential documents and reports commissioned by other bodies
  are excluded.
- This is not a Creative Commons licence, and it is **not limited to non-commercial use**.
  **Attribution is required.**
- Suggested attribution: *Source: Australian Energy Market Operator (AEMO), NEM Generation Information
  (July 2026) and 2026 Electricity Statement of Opportunities (August 2026). © 2026 AEMO, used in
  accordance with AEMO's copyright permissions. The cleaned workbook is a derived summary and is not
  published by AEMO.*

## Cleaning (`scripts/clean.py`)

```bash
python -I scripts/clean.py raw/nem-generation-information-july-2026.xlsx nem-gridwatch_clean.xlsx
```

The raw workbook cannot be narrated as it is. The reference narrator would produce about 2,100 chunks,
with headers like "Column2". The data sheet `Generator Information` has its header in row 4, column A
is an internal survey ID, and it has 165 columns: 26 data columns, 50 seasonal "Scheduled Capacity"
columns and 89 completely empty unnamed columns. Each generating unit is its own row, so Gladstone is
6 near-identical rows and Hallett is 13. The script does the following:

1. Reads `Generator Information` with row 4 as the header.
2. Keeps only units that exist or are firmly in the pipeline: In Service, In Commissioning, Announced
   Withdrawal, Committed, Committed* and Anticipated. The 893 speculative "Publicly Announced" units
   and the 1 "Withdrawn" unit stay out of the station sheets but are still counted in the totals.
3. Drops "Non-scheduled" units: 118 units, 1,542 MW in that scope. AEMO's own Summary table and the
   ESOO count only scheduled, semi-scheduled and significant non-scheduled generators (ESOO p. 15).
   With this rule every total matches the report. Without it, existing capacity sums to 78,524 MW
   instead of 77,004.5 MW, and Snuggery shows 84 MW instead of the ESOO's 63 MW.
4. Groups units into one row per station, status, technology and notified closure date. Each row gives
   total MW and MWh and a readable list of unit sizes, for example "2 x 350 + 2 x 375". The closure
   date is part of the key, so Torrens Island B splits into unit 1 (2026) and units 2-4 (2028),
   exactly as ESOO Table 9 shows it.
5. Makes values readable:
   - `NSW1` becomes "New South Wales".
   - Dates are ISO dates.
   - OCGT and CCGT are spelled out.
   - Float noise is rounded away.
   - Trailing full stops in site names are removed ("Snowy 2.0." becomes "Snowy 2.0").
   - Expected closure years of 2100 or later are marked "(far-future placeholder, not a real closure
     plan)".
6. Fixes one mislabelled row. Survey ID 1097 is the in-service John Butters hydro unit (Tasmania,
   Hydro-Electric Corporation, DUID JBUTTERS, 144 MW), but AEMO gives it the site name of a Victorian
   battery project, "Joel Joel BESS 2 - KCI".
7. Adds summary sheets with totals and the largest station or project per technology, because "largest"
   questions cannot be answered by retrieving a few rows. It also adds a Glossary sheet with AEMO's
   status definitions and notes on placeholders and scope.

## Cleaned workbook: `nem-gridwatch_clean.xlsx`

| Sheet | Rows | Columns | Column A (subject) |
|---|---|---|---|
| Existing fleet | 351 | 14 | Station |
| Pipeline projects | 152 | 14 | Project |
| Capacity by technology | 8 | 11 | Technology (7 types plus "All technologies") |
| Capacity by state | 6 | 5 | State (5 states plus "NEM total"); same layout as ESOO Table 7 |
| Glossary | 15 | 2 | Term |
| **Total** | **532** | | |

Estimated chunks: **191**. This is an exact count from running the reference narrator and the
reference splitter (1200 characters, 240 overlap). The raw workbook gives 2,104, and an earlier
unit-level subset gives 332.

Narration samples:

```
Existing fleet — Station 'Yallourn W': Owner is EnergyAustralia Yallourn Pty Ltd; State is Victoria; Technology is Coal; Technology detail is Brown Coal; Dispatch type is Scheduled; Status is Announced Withdrawal; Units (number x MW AC each) is 2 x 350 + 2 x 375; Capacity (MW AC) is 1450; Expected closure year is 2028; Notified closure date is 2028-07-01.
Capacity by technology — Technology 'Battery Storage': Existing incl. in commissioning (MW) is 7753.7; In service (MW) is 5945.2; In commissioning (MW) is 1808.6; Announced withdrawal (MW) is 0; Committed (MW) is 5500.6; Committed* (MW) is 1923.7; Anticipated (MW) is 12609.1; Publicly announced (MW) is 147521.6; Largest existing station is Western Downs Battery - Stage 1 And 2 (Queensland), 509.6 MW, In Service; Largest pipeline project is Waratah Super Battery (New South Wales), 849.6 MW, Committed.
Capacity by state — State 'New South Wales': Existing and in commissioning (MW) is 24549; Committed incl. Committed* (MW) is 6338; Anticipated (MW) is 9623; Proposed, publicly announced (MW) is 123487.
```

## Data-quality notes (found while verifying)

- **ESOO Table 7 has a typo.** On p. 51, New South Wales "Proposed" reads 23,487 MW. The register
  gives 123,487 MW, and only 123,487 makes the state values add up to the printed NEM total of
  343,738; the printed values add up to 243,740. Every other cell of Table 7 matches the register to
  the MW.
- **Mislabelled site (fixed):** John Butters hydro sits under the site name "Joel Joel BESS 2 - KCI".
- **Stale status:** Torrens Island B unit 1 is still "Announced Withdrawal" with a notified closure date
  of 2026-06-30, before the release date (Change Log: 2026-07-24). By AEMO's own definition it should be
  "Withdrawn", and ESOO Table 9 lists only units 2-4. The unit is kept so that totals match AEMO's
  published numbers.
- **Placeholder closure years:** 2100 (52 units in the full register, mostly Tasmanian hydro), 2129
  (Snowy 2.0), 2134 (Borumba) and 2200 (Callide C, a black-coal station, and Goat Hill). These are now
  marked in the text.
- **Scope:** the cleaned totals exclude non-scheduled units, the same scope as the ESOO. "Max Site
  Capacity (AC)" was dropped because it can differ from the nameplate sum: Yallourn W shows 1,600 MW
  against 1,450 MW of units, and Kurri Kurri 660 MW against 750 MW.
- Smaller issues:
  - Unit ID 122903 is duplicated (Whitwood Road, a publicly announced project, so not in the
    cleaned file).
  - 2 pipeline rows have no owner.
  - Some publicly announced projects have commercial-use dates of 2050-2060.
- Staged projects can show several dates on one row (for example Valley of the Winds:
  "2029-12-31, 2030-04-30, 2030-07-31"). Hybrid sites appear once per technology (for example Bungaban
  Wind Farm: wind 1,155 MW plus battery 344.08 MW).

## PDF ingestion notes

- 124 pages, 4.49 MB, real text layer (about 2,600 characters per page, 318K characters). That is
  about **386 chunks** with the reference splitter, so the project total is about 191 + 386, or
  roughly 577 chunks.
- **Azure Document Intelligence F0** accepts at most 4 MB per file and analyses only the first 2 pages
  per request, so this PDF needs splitting, or use the paid S0 tier.
  `mutool clean -gggg -z 2026-electricity-statement-of-opportunities.pdf pages_051_052.pdf 51-52`
  produced 62 two-page files of at most 0.74 MB, with the text layer intact.
- Useful pages: Executive Summary p. 3-11 (reliability gaps p. 3 and 10, RRO and T-3 p. 11, Kurri Kurri
  and Snowy 2.0 p. 8). Section 3.2.1 and Table 7 are on p. 50-51 (Table 7 links the PDF to the XLSX),
  and Table 9 (announced retirements) is on p. 57.

## Verified demo answer key

| # | Needs | Question | Verified answer | Evidence |
|---|---|---|---|---|
| 1 | xlsx | When is Yallourn W due to close, who owns it, and what are its unit sizes? | EnergyAustralia Yallourn Pty Ltd, Victoria, brown coal. Units 2 x 350 + 2 x 375 MW (1,450 MW), Announced Withdrawal, notified closure date 2028-07-01. | Existing fleet, row 'Yallourn W' (also ESOO Table 9, p. 57) |
| 2 | xlsx | What are the power rating, storage size and expected full commercial use date of Neoen's Great Western Battery? | 500 MW / 4,500 MWh lithium-ion battery in New South Wales (Neoen Australia Pty Ltd), Anticipated, full commercial use date 2029-11-30. | Pipeline projects, row 'Great Western Battery' |
| 3 | xlsx (superlative, **best before/after demo**) | What is the largest battery project in the NEM pipeline? | Waratah Super Battery, New South Wales: 849.6 MW / 1,679 MWh, Committed, owner Munmorah Battery ProjectCo Pty Ltd, full commercial use date 2026-09-01. The answer is unique; the next is Baranduda Electrical Energy Reserve at 544.4 MW. The ESOO never names it. | Capacity by technology, row 'Battery Storage'; Pipeline projects, row 'Waratah Super Battery' |
| 4 | pdf | Under the Committed and Anticipated Developments assessment, from when is the reliability standard forecast to be exceeded in each region? | From 2030-31 in New South Wales and Victoria, from 2031-32 in South Australia, from 2032-33 in Queensland and from 2033-34 in Tasmania. | ESOO p. 3 (also p. 10) |
| 5 | both | The ESOO says that without anticipated projects, reliability instruments would be needed in NSW and Queensland for 2029-30 after Gladstone and Eraring close. When do they close and how much capacity leaves? | PDF: T-3 instruments would have been requested for 2029-30 after the Gladstone and Eraring closures. XLSX: Gladstone (Queensland, Gladstone Power Station Participants) 6 x 280 = 1,680 MW closes 2029-03-31. Eraring (NSW, Origin Energy Eraring Pty Ltd) 4 x 720 = 2,880 MW closes 2029-04-30. Together 4,560 MW, both Announced Withdrawal. | ESOO p. 11; Existing fleet, rows 'Gladstone' and 'Eraring' |
| 6 | both | The ESOO assumes Snowy 2.0 arrives in December 2028. What does the register say? | PDF: 2.2 GW / 350 GWh from December 2028. XLSX: Snowy Hydro Ltd, Committed, 3 x 363 + 3 x 371 MW = 2,202 MW, 349,980 MWh, full commercial use date 2028-12-31. It is also the largest project in the whole pipeline. | ESOO p. 8; Pipeline projects, row 'Snowy 2.0'; Capacity by technology, row 'All technologies' |
| 7 | both | ESOO Table 7 lists NSW proposed capacity as 23,487 MW. Does the register agree? | No. The register gives 123,487 MW for NSW, and the table's own NEM total of 343,738 MW only adds up with 123,487 (the printed state values sum to 243,740). The PDF cell is missing a leading "1". | ESOO p. 51 (Table 7); Capacity by state, rows 'New South Wales' and 'NEM total' |
| 8 | off-topic | Can you give me a good recipe for banana bread? | The guardrail should refuse: the question is outside NEM reliability and generation. | n/a |
