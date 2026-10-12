# UK PowerDesk: DUKES 2026 Power Station Analyst (starter kit)

Chat with the UK's official energy statistics digest (DUKES 2026, PDF) and its register of 1,364 major
power stations, 88 station changes since 2010, 11 interconnectors and capacity by fuel (DUKES Table 5.11,
XLSX). Persona: a strategy analyst at a UK utility, or a DESNZ/Ofgem policy adviser, linking headline trends
(no coal, record renewables, record imports) to the specific assets behind them.

Verdict: **ready with cleaning**. The XLSX is native, entity-rich and internally consistent. The
subset in `scripts/clean.py` is mandatory: the whole workbook would be about 4,300 chunks. Licence: Open
Government Licence v3.0.

## 1. Sources

| | XLSX (must have) | PDF (bonus) |
|---|---|---|
| Title | DUKES 2026 Table 5.11: Power stations in the United Kingdom (end of May 2026) | Digest of UK Energy Statistics (DUKES) 2026, Chapters 1 to 7 |
| Publisher | Department for Energy Security and Net Zero (DESNZ) | same |
| Download | https://assets.publishing.service.gov.uk/media/6a6a365b0ddb7e4831c629f3/DUKES_5.11.xlsx | https://assets.publishing.service.gov.uk/media/6aba9601a9c3d267bccceff7/DUKES_2026_Chapters_1-7.pdf |
| Landing page | https://www.gov.uk/government/statistics/electricity-chapter-5-digest-of-united-kingdom-energy-statistics-dukes (lists this exact file; updated 30 Jul 2026) | https://www.gov.uk/government/statistics/digest-of-uk-energy-statistics-dukes-2026 |
| Checked | 2026-10-06: HTTP 200, no redirect, xlsx MIME type, 1,247,443 bytes (1.19 MB) | 2026-10-06: HTTP 200, no redirect, `application/pdf`, 3,229,643 bytes (3.08 MB) |
| sha256 | `760286f4cb3168f04a5ee1b581dbfcb35e1d65a5ea0151bb2dd4dc12dca2c72a` | `4a3ad5bbf2ab659d00299e2fabe9f466c0fba4961e020577fd1cce6fbf3c7631` |
| Shape | 28 sheets, 14,572 non-empty rows (22 are archive lists DUKES 2004-2025); header in row 6 | 65 pages, text layer (about 1,800 characters per page, 116k characters, about 121 chunks); chart values are images |

The F0-friendly alternative is the Chapter 5 PDF only:
https://assets.publishing.service.gov.uk/media/6aba96e8997a4b2950ccf02c/DUKES_2026_Chapter_5.pdf
(11 pages, 0.70 MB, text layer). It holds the 52.1 %, Ratcliffe and interconnector-flow facts, but not
Cleve Hill or Dogger Bank, which are in Chapter 6.

### Licence and attribution

* PDF, p. 65: *"© Crown copyright 2026. This publication is licensed under the terms of the Open Government
  Licence v3.0 except where otherwise stated."*
* XLSX: the GOV.UK landing page footer states *"All content is available under the Open Government Licence
  v3.0, except where otherwise stated"*. The workbook's cover sheet has no separate licence text.
* OGL v3.0 allows commercial and non-commercial reuse, including adaptation (the cleaned workbook), if you
  attribute. Attribution text:
  `Contains public sector information licensed under the Open Government Licence v3.0. Source: DESNZ, Digest of UK Energy Statistics (DUKES) 2026, Table 5.11 and Chapters 1-7.`
* The cover sheet contains a named statistician's phone number and e-mail; it is not copied into the cleaned file.

## 2. Rebuild the cleaned workbook

```bash
mkdir -p raw && curl -L --fail -A "Mozilla/5.0" -o raw/DUKES_5.11.xlsx \
  "https://assets.publishing.service.gov.uk/media/6a6a365b0ddb7e4831c629f3/DUKES_5.11.xlsx"
python -I scripts/clean.py raw/DUKES_5.11.xlsx uk-powerdesk_clean.xlsx
```

`clean.py` only needs `openpyxl` and is commented step by step. What it does:

1. **Keeps 4 tables (one sheet each) and drops the rest.** It drops the cover, contents and notes sheets and
   the 22 archive sheets "DUKES 2025" to "DUKES 2004", which would add about 3,600 chunks of old station lists.
2. **Power stations** (from `5.11 Full list`):
   * Removes the 5 title/accessibility rows, so the header is row 1.
   * Makes **Site name** column A; the source's column A is the company.
   * Merges `Technology` + `Type` into one readable label (e.g. `Offshore wind`, `Solar PV`,
     `Nuclear, advanced gas-cooled reactor (AGR)`, `Combined cycle gas turbine (CCGT)`).
   * Keeps `Primary fuel` only where it adds information (fossil and bioenergy plants), expanding
     `MSW` to `Municipal solid waste (MSW)`.
   * Writes `CHP plant` = Yes only for the 11 CHP plants (blank = not CHP).
   * Rounds capacity to 2 decimals and stores the year commissioned as a number (it is text in the source).
   * Trims stray spaces (32 cells) and fixes `North west` to `North West`.
   * Drops postcode, OS reference, X/Y coordinates and the DESNZ site code.
   * **Sorts rows by technology, then largest first**, so similar plants share chunks and the biggest plant of
     each type comes first.
3. **Station changes** (from `Changed and converted stations`): 88 openings, closures and conversions since
   2010, with **Site** in column A.
4. **Capacity by fuel** (from Table 5.11.A on `5.11 Summary`): 26 rows keyed
   `Major power producers - Natural Gas`, `Other generators - Solar`, `All generators - Total`, and so on.
5. **Interconnectors** (from Table 5.11.B on `5.11 Summary`): 11 links. Fixes the source typo
   `Interconnecion` to `Interconnexion` and makes `England-France` consistent with `England - France`.
6. **Turns `[note n]` markers into readable text.** The note text goes into a `Notes` column (or the row's
   existing note is kept). Two markers in the source point at the wrong note and are corrected first:
   `Viking [note 27]` should be note 29, and `Closed [note 29]` on Oldbury, Roosecote and Tilbury B is
   dropped (note 29 is the Viking note).
7. **Adds `About this data`** (11 rows): scope (major power producers only), the meaning of "Company",
   capacity date, the HV/LV caveat, abbreviations, the Viking name clash, and the Drax 2,580 vs 2,640 MW
   discrepancy.

## 3. Cleaned workbook (`uk-powerdesk_clean.xlsx`)

| Sheet | Rows | Columns | Narrated chars | Est. chunks |
|---|---|---|---|---|
| Power stations | 1,364 | 11 | 307,061 | 320 |
| Station changes | 88 | 7 | 18,556 | 20 |
| Capacity by fuel | 26 | 3 | 5,579 | 6 |
| Interconnectors | 11 | 5 | 2,289 | 3 |
| About this data | 11 | 2 | 3,142 | 4 |
| **Total** | **1,500** | | **336,627** | **about 351** |

Add the PDF (about 121 chunks) and the project is about 470 chunks. If you need fewer chunks, dropping the
60 stations under 1 MW saves about 14 chunks.

### Narration samples (reference Excel Narrator, offline mode)

```
Power stations — Site name 'Hornsea 02': Company is Orsted; Technology is Offshore wind; Installed capacity (MW) is 1386; Grid connection is HV; Country is England; Region is East Midlands; Year commissioned is 2022.
Station changes — Site 'Ratcliffe-on-Soar': Year is 2024; Fuel or plant type is Coal; Change is Closed; Capacity before (MW) is 2021; Capacity after (MW) is 0; Notes is Closed at end of September 2024.
Interconnectors — Interconnector 'North Sea Link (NSL)': Connects is England - Norway; Capacity (MW) is 1400; Year of first operation is 2021.
```

### Data-quality notes (read before demoing)

* **Checks that pass.** The 1,364 station capacities sum to 85,099.5 MW, exactly the major-power-producer
  total in Table 5.11.A. Per-technology sums match the fuel rows: offshore wind 16,884.9, onshore wind
  12,952.6, nuclear 5,883, pumped storage 2,900, solar 6,454 MW. There are no country/region mismatches,
  no exact duplicate rows, and years run from 1906 to 2026.
* **Wrong note markers in the source.** `Viking [note 27]` and `Closed [note 29]` (×3) point at the wrong
  note. `clean.py` fixes them.
* **Drax capacity differs between sheets.** The station list shows 2,580 MW; the changes table and note 20
  give 2,640 MW (four 660 MW units).
* **Ironbridge.** The source row note says "initially converted to 940 MW" while note 21 says 740 MW (two
  370 MW units, so 740 is consistent). This is kept as published.
* **Name clashes and repeats.** Three assets are called "Viking": an onshore wind farm (443 MW), a 50 MW gas
  plant and the England-Denmark interconnector. 17 site names repeat across different sites (e.g.
  `Manor Farm`). Some site names look like companies (`EP SHB LTD`, `Severn Power Ltd`,
  `Moray Offshore Windfarm West Limited`).
* **Names differ between sheets.** Only 10 of the 27 sites opened or converted in `Station changes` match a
  `Power stations` name exactly. Examples: `Pembroke B` vs `Pembroke`, `Staythorpe C` vs `Staythorpe`,
  `Kilroot OCGT (EPNI)` vs `EPNI`, `Drax` vs `Drax Power Station Biomass Units`.
* **Grid connection HV/LV is not defined** in the workbook. Do not read LV as "small": Great Yarmouth and
  Shoreham CCGTs (420 MW each) are LV.
* **Partially built wind farms** show only their operating part (Dogger Bank A 616 MW, Dogger Bank B 260 MW).
  The PDF says Dogger Bank "is due to expand further in 2026".
* **Do not compare capacity across the two files.** The PDF reports **de-rated** capacity for end-2025
  (74.5 GW total, p. 34). The XLSX reports **installed** capacity at end of May 2026 (113,858 MW, all
  generators). "Other generators" capacities for coal, oil, gas and other fuels are declared net capacity
  (DNC).
* **Scope.** The station list covers major power producers only: no rooftop solar or small embedded plant.
* **Aggregate questions** ("total capacity of company X", "how many solar farms in Wales") are unreliable with
  top-5 retrieval over 1,364 rows. Point them at `Capacity by fuel` or the PDF.
* **Offshore wind "Region"** is the onshore region as supplied (e.g. Hornsea 02 is "East Midlands").

## 4. PDF notes (Azure Document Intelligence)

65 pages and 3.08 MB, so the file is under the 4 MB F0 limit. F0 analyses only 2 pages per request, so split
it into 33 two-page files (or use S0, or use the 11-page Chapter 5 PDF: 6 requests). Text layer verified;
chart values are images, so only captions and prose are retrievable.

PDF and XLSX cross-checks done:
* PDF pp. 6 and 8: Ratcliffe-on-Soar closed on 30 September 2024. `Station changes`: Ratcliffe-on-Soar, Coal,
  Closed in 2024, 2,021 MW to 0, "Closed at end of September 2024".
* PDF pp. 41 and 43: Cleve Hill (373 MW) is the UK's largest solar farm. `Power stations`: Cleve Hill, WPO UK
  Services, Solar PV, 373 MW, 2025, and the largest Solar PV row (next: Sutton Bridge at 73.1 MW).

## 5. Verified demo answer key

| # | Question | Needs | Expected answer | Evidence |
|---|---|---|---|---|
| 1 | Which interconnector has the largest capacity, and which countries does it connect? (**best before/after A2A demo**) | xlsx | **Interconnexion France-Angleterre (IFA): 2,000 MW, England - France, first operation 1986.** It is unique; the next are North Sea Link and Viking at 1,400 MW each. | `Interconnectors`, row `Interconnexion France-Angleterre (IFA)`. The PDF gives only flows by country, no link names or capacities. The sheet is about 3 chunks, so top-5 retrieval finds it reliably. |
| 2 | Which offshore wind farm in the register has the largest installed capacity, and who operates it? | xlsx | Hornsea 02, operated by Orsted: 1,386 MW, England (East Midlands in the register), commissioned 2022. Unique (next: Hornsea 01 at 1,218 MW). | `Power stations`, row `Hornsea 02` (first row of the offshore wind block). The PDF only names "Hornsea 1 and 2" (p. 43) with no size or operator. |
| 3 | How large are the Drax biomass units, and how did Drax's coal-to-biomass conversion unfold? | xlsx | `Drax Power Station Biomass Units` (Part of Drax Group): 2,580 MW of biomass, Yorkshire and the Humber, commissioned 2013. Changes: biomass 660 MW (2013), 1,320 MW (2014), 1,980 MW (2015) and 2,640 MW (2018); the last 1,320 MW of coal closed in 2023 ("Ceased operations in April 2023"). Mention the 2,580 vs 2,640 MW difference. | `Power stations` row Drax; `Station changes` rows `Drax` 2013-2023. Drax does not appear in the PDF. |
| 4 | What share of UK electricity generation came from renewables in 2025, and how much did they generate? | pdf | 52.1 %, a record, up 1.6 percentage points on 2024 (the first year above half), and above 50 % for the second time. Generation was a record 153.0 TWh, up 5.9 %. | PDF p. 38 (also pp. 1, 28, 31). |
| 5 | DUKES says 2025 was the first year with no coal generation. Which plant was the last to close, when, and how much capacity did that remove? | both | PDF: Ratcliffe-on-Soar, the last coal-fired plant, closed on 30 September 2024, and 2025 was the first year on the series with no coal generation. XLSX: Ratcliffe-on-Soar, Coal, Closed in 2024, 2,021 MW to 0 MW. Major-power-producer coal capacity at end of May 2026 is 0 MW. | PDF pp. 6, 8, 31; `Station changes` row `Ratcliffe-on-Soar`; `Capacity by fuel` row `Major power producers - Coal`. |
| 6 | Norway was the UK's second-largest source of net electricity imports in 2025. Which interconnector carries that power, and what is its capacity? | both | PDF: France-UK links gave 21.9 TWh of net imports, followed by Norway-UK with 9.0 TWh. XLSX: North Sea Link (NSL), England - Norway, 1,400 MW, first operation 2021. | PDF p. 33; `Interconnectors` row `North Sea Link (NSL)`. |
| 7 | The digest says most of the 0.5 GW of offshore wind added in 2025 came from Dogger Bank. What does the register show for Dogger Bank? | both | PDF: 0.5 GW added, "mainly from additional capacity at Dogger Bank which is due to expand further in 2026". XLSX: Dogger Bank A, 616 MW (2024), and Dogger Bank B, 260 MW (2026). Both are SSE Group and Equinor, Yorkshire and the Humber, offshore wind. | PDF p. 43; `Power stations` rows `Dogger Bank A` and `Dogger Bank B`. |
| 8 | Can you recommend a good holiday destination in Spain? | off-topic | The guardrail should decline: the assistant only covers UK energy statistics and power stations (DUKES 2026). | none |
