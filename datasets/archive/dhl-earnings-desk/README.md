# DHL Earnings Desk: Equity Research Copilot (starter kit)

An earnings copilot for DHL Group's Q2 / H1 2026 results. Quarterly numbers by division, region and
product (Q1 2025 to Q2 2026, plus FY 2025) come from the **Statbook Q2 2026 workbook (XLSX)**.
Management commentary, guidance and risks come from the **2026 Half-year Report (PDF)**.

Verdict: **ready with cleaning**. The XLSX is small (291 rows, about 79 chunks) but clean and
internally consistent once `scripts/clean.py` has run. The licence is restrictive (see below).

## 1. Sources

| | XLSX (must have) | PDF (bonus) |
|---|---|---|
| Title | DHL Group Statbook Q2 2026 | DHL Group 2026 Half-year Report |
| Publisher | Deutsche Post AG / DHL Group, Investor Relations | same |
| Download | https://reporting-hub.group.dhl.com/ecomaXL/files/DHL-Group-Statbook-Q2-2026.xlsx | https://reporting-hub.group.dhl.com/ecomaXL/files/DHL-Group-Half-year-Report-2026.pdf |
| Landing page | https://reporting-hub.group.dhl.com/en/ (Statbooks back to Q4/FY 2020 are listed there) | https://reporting-hub.group.dhl.com/2026-q2/en/ |
| Checked | 2026-10-06: HTTP 200, no redirect, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, 253,267 bytes (0.24 MB) | 2026-10-06: HTTP 200, no redirect, `application/pdf`, 709,799 bytes (0.68 MB) |
| sha256 | `700f05d38bf1f778685b5e028c1b4981166616e250b109f455bb2544b956e91c` | `371570b21e0894a3ac8bfaf618d8ba1fa964815f80a4a5e57a7bd19e0877dfbd` |
| Shape | 13 sheets, 321 non-empty rows, header never in row 1, values stored as constants (no formulas) | 44 pages, A4, text layer (about 3,200 characters per page, about 94k characters, about 98 chunks); published 5 Aug 2026 |

### Licence and attribution

* **No open licence.** Both files are Deutsche Post AG investor-relations material. The Reporting Hub footer
  reads *"Copyright © 2026 DHL AG"* and links to the group disclaimer
  (https://group.dhl.com/en/disclaimer.html), which states: *"The content and
  structure of the Deutsche Post website is under copyright protection, and none of the information or data,
  particularly not the texts, portions thereof, or pictures, may be reproduced without the prior consent of
  Deutsche Post AG."* (The page timed out during verification; this wording comes from a search-engine
  extract of that page.) The PDF itself has no licence statement, only a forward-looking-statements disclaimer.
* Treat the files as **private, non-commercial material**. Download the files from DHL
  yourself and run `clean.py`. **Do not commit the raw or cleaned files to a public repository** and do not
  republish them.
* Attribution to show in the app and the report:
  `Source: DHL Group (Deutsche Post AG), Statbook Q2 2026 and 2026 Half-year Report, https://reporting-hub.group.dhl.com/`

## 2. Rebuild the cleaned workbook

```bash
# download into its own empty folder (never run anything from it)
mkdir -p raw && curl -L --fail -A "Mozilla/5.0" -o raw/DHL-Group-Statbook-Q2-2026.xlsx \
  "https://reporting-hub.group.dhl.com/ecomaXL/files/DHL-Group-Statbook-Q2-2026.xlsx"
python -I scripts/clean.py raw/DHL-Group-Statbook-Q2-2026.xlsx dhl-earnings-desk_clean.xlsx
```

`clean.py` only needs `openpyxl` and is commented step by step. What it does:

1. **Drops the `Title` sheet.** It holds only the cover plus names, e-mails and phone numbers of IR staff,
   which are personal data and must not be indexed.
2. **Makes row 1 the header.** It deletes the title row (row 1) so the periods in row 2 become the header, and
   sets A1 = `Line item (EUR m unless stated)`.
3. **Removes the empty column B** (Income statement, Cash flow statement, Balance sheet) and the trailing
   empty columns.
4. **Renames the headers.** The two `yoy in %` columns become `Q1 2026 vs Q1 2025 change (%)` and
   `Q2 2026 vs Q2 2025 change (%)`. Balance-sheet quarters become quarter-end dates (`30 Jun 2026`), as in the
   Net debt and Pensions sheets.
5. **Makes repeated labels unique** with block prefixes. Examples: `Revenue - DHL Express` / `EBIT - DHL Express`;
   `Revenue - Ocean Freight` / `Gross profit - Ocean Freight`; `Share of revenue by sector (%) - Retail`;
   `Volume (million items) - Parcel Germany`; `Discount rate (%) - UK`. Sub-heading rows become prefixes and are
   then deleted.
6. **Removes footnote rows and markers** (`Road Freight*`, `Net capex 1)`, `Financial liabilities1)`), the
   repeated period header in Global Forwarding, and the small working-days calendar at the end of P&P. The
   footnote text is **kept** in a new `Notes` sheet.
7. **Puts units in the labels and expands codes.** Examples: `Basic earnings per share (EUR per share)`,
   `Employees (full-time equivalents)`, `Capex to depreciation of owned assets (x)`, `Net debt to EBITDA (x)`,
   `TDI (Time Definite International) weight per day (thousand kg)`, `Ocean freight gross profit per TEU (EUR)`,
   `Middle East and Africa (MEA)`, `Property, plant and equipment (PPE)`, `Total defined benefit obligation (DBO)`.
8. **Fixes number formats.** Ratios stored as fractions are multiplied by 100: every row with `(%)` in its label
   and both change columns (margins, tax rate, ROIC, equity ratio, net gearing, organic growth, sector shares,
   weight-per-day growth, funding ratio, discount rates). EUR m values are rounded to 1 decimal, multiples and
   EPS to 2, discount rates to 2 (as in the PDF), and head counts to whole numbers.
9. **Renames the sheets.** `DGF` becomes `DHL Global Forwarding`, `DSC` becomes `DHL Supply Chain`, `DeC`
   becomes `DHL eCommerce`, `P&P` becomes `Post & Parcel Germany`, `P&L` becomes `Income statement`, and so on.
   It also adds the 15-row `Notes` sheet (footnotes, definitions, abbreviations, and the sign convention of the
   change columns).

## 3. Cleaned workbook (`dhl-earnings-desk_clean.xlsx`)

| Sheet | Rows | Columns | Narrated chars | Est. chunks |
|---|---|---|---|---|
| Group key figures | 24 | 10 | 6,629 | 7 |
| Revenue growth | 14 | 8 | 3,256 | 4 |
| Income statement | 21 | 10 | 6,096 | 7 |
| Cash flow statement | 14 | 10 | 4,234 | 5 |
| Balance sheet | 43 | 7 | 9,021 | 10 |
| DHL Express | 27 | 10 | 7,574 | 8 |
| DHL Global Forwarding | 29 | 10 | 8,377 | 9 |
| DHL Supply Chain | 33 | 10 | 8,331 | 9 |
| DHL eCommerce | 18 | 10 | 5,090 | 6 |
| Post & Parcel Germany | 31 | 10 | 9,256 | 10 |
| Net debt | 12 | 7 | 2,553 | 3 |
| Pensions | 10 | 7 | 2,222 | 3 |
| Notes | 15 | 2 | 3,030 | 4 |
| **Total** | **291** | | **75,669** | **about 79** |

Add the PDF (about 98 chunks) and the whole project is about 180 chunks, well under 600 chunks.

### Narration samples (reference Excel Narrator, offline mode)

```
Group key figures — Line item (EUR m unless stated) 'EBIT - Group EBIT': Q1 2025 is 1370; Q2 2025 is 1429.3; Q3 2025 is 1476.6; Q4 2025 is 1821.4; FY 2025 is 6097.3; Q1 2026 is 1477.7; Q1 2026 vs Q1 2025 change (%) is 7.9; Q2 2026 is 1857.7; Q2 2026 vs Q2 2025 change (%) is 30.
DHL Global Forwarding — Line item (EUR m unless stated) 'Revenue - Ocean Freight': Q1 2025 is 1457.3; Q2 2025 is 1316.2; Q3 2025 is 1323.6; Q4 2025 is 1232.9; FY 2025 is 5330.1; Q1 2026 is 1217.1; Q1 2026 vs Q1 2025 change (%) is -16.5; Q2 2026 is 1421; Q2 2026 vs Q2 2025 change (%) is 8.
Pensions — Line item (EUR m unless stated) 'Pension funding ratio (%)': 31 Mar 2025 is 86.4; 30 Jun 2025 is 85.3; 30 Sep 2025 is 86.8; 31 Dec 2025 is 88.2; 31 Mar 2026 is 88.8; 30 Jun 2026 is 89.4.
```

### Data-quality notes (read before demoing)

* **Sign convention of the change columns.** DHL computes `(current - prior) / |prior|`; this was checked on
  all 314 change cells. Costs, depreciation, taxes and cash outflows are stored as **negative** numbers, so
  for them a **negative change means the cost grew**. Example: DHL Express staff costs went from -1605.9 to
  -1619.7, and the change column shows -0.9 although costs rose 0.9 %. The `Notes` sheet explains this; check
  that answers about cost lines read the sign correctly.
* **Huge percentages on tiny bases** are arithmetically correct but look odd. Examples: DHL eCommerce net
  income from investments, Q1 2026 change -1593.9 %; cash-flow "Net interest", Q1 2026 change -811.5 %; free
  cash flow, Q2 2026 change +507.3 %.
* **Adjusted EBIT.** A source footnote says Group and eCommerce EBIT for Q4 2025 and Q1 2026 are adjusted
  (finalised purchase price allocation for UK eCommerce; the PDF, p. 24, describes the Evri transaction), so
  these can differ slightly from earlier publications.
* **Point-in-time ratios.** ROIC and Net debt to EBITDA exist only for FY 2025; equity ratio and net gearing
  only at 31 Dec 2025. Sector shares in Supply Chain have no FY 2025 value.
* **Small balance-sheet gap.** At 31 Mar 2025 total assets (71,630.1) and total equity and liabilities
  (71,630.3) differ by EUR 0.2m in the source. Every other check ties out exactly: segment revenue and EBIT sum
  to the group totals, and the four quarters sum to FY 2025.
* **Repeated line items across sheets.** `Staff costs`, `EBITDA`, `EBIT`, `Revenue - Europe` and others appear
  in several division sheets. Each sentence starts with the sheet name (`DHL Express — ...`), which keeps
  them apart, but questions should name the division.
* **Different metrics with similar names.** The PDF says Supply Chain "concluded additional contracts with
  a volume of EUR 3,991 million" in H1 2026. The XLSX row `New business wins (EUR m)` (Q1 + Q2 2026 =
  864.4) is a different metric, so do not mix them.
* The PDF only compares H1/Q2 2026 with 2025. Quarterly detail for Q3 2025, Q4 2025 and Q1 2026 exists **only**
  in the XLSX, which makes good before/after questions for the A2A ingestion step.

## 4. PDF notes (Azure Document Intelligence)

44 pages and 0.68 MB, so the file is under the 4 MB limit. On the Free (F0) tier only the first 2 pages of each
request are analysed, so split it into 22 two-page files (or use S0). Text layer verified with pdftotext; the
key-figure tables (for example "Key figures, Express", p. 4) are real text, not images.

PDF and XLSX cross-checks done:
* p. 4: Express Q2 2026 EBIT EUR 1,200m (+64.3 %), return on sales 16.8 % vs 12.4 %, and TDI revenue per day
  EUR 84.8m, all match the `DHL Express` sheet (1200.2, 64.3, 16.8, 84.8).
* p. 23: pension discount rates at 31 Dec 2025 / 30 Jun 2026 of 4.10/4.10 (Germany), 5.40/5.80 (UK),
  3.38/3.48 (other) and 4.39/4.51 (total) match the `Pensions` sheet.

## 5. Verified demo answer key

| # | Question | Needs | Expected answer | Evidence |
|---|---|---|---|---|
| 1 | In which quarter of 2025 did DHL Group earn its highest EBIT, and what was the EBIT margin in that quarter? (**best before/after A2A demo**) | xlsx | **Q4 2025: EUR 1,821.4m at an 8.2 % EBIT margin.** It is unique; the other quarters are Q3 1,476.6m, Q2 1,429.3m and Q1 1,370.0m. FY 2025: EUR 6,097.3m at 7.4 %. Q4 2025 EBIT is "adjusted" (PPA footnote). | `Group key figures`, rows `EBIT - Group EBIT` and `EBIT margin (%)`. Not in the PDF (no Q3/Q4 2025 figures there). |
| 2 | What was DHL's pension funding ratio at 30 June 2026, and how has it moved since 31 March 2025? | xlsx | 89.4 % at 30 Jun 2026, up from 86.4 % at 31 Mar 2025. Path: 85.3 % (30 Jun 2025), 86.8 % (30 Sep 2025), 88.2 % (31 Dec 2025), 88.8 % (31 Mar 2026). | `Pensions`, row `Pension funding ratio (%)`. Not in the PDF. |
| 3 | Which sector accounted for the largest share of DHL Supply Chain revenue in Q2 2026? | xlsx | Retail, 27.3 % (unique), ahead of Consumer at 22.1 % and Life Sciences & Healthcare at 13.5 %. | `DHL Supply Chain`, rows `Share of revenue by sector (%) - ...`. The PDF names sectors only qualitatively. |
| 4 | What is DHL Group's updated full-year 2026 guidance, and when was it raised? | pdf | Raised on 7 July 2026: reported Group EBIT above EUR 6.5bn (previously above EUR 6.2bn) and divisional EBIT above EUR 5.9bn (previously above EUR 5.6bn). Unchanged: Post & Parcel Germany above EUR 0.9bn and Group Functions around EUR -0.4bn. Capex excluding leases EUR 3.0-3.3bn; free cash flow excluding acquisitions and divestitures around EUR 3.0bn. | PDF p. 12, "Expected developments". |
| 5 | The report says ocean freight rates normalised from 2025 through Q1 2026 and then rose sharply in Q2 2026. What do the quarterly figures show for ocean freight revenue and gross profit per TEU? | both | Revenue (EUR m): 1,457.3 (Q1 2025), 1,316.2 (Q2), 1,323.6 (Q3), 1,232.9 (Q4), 1,217.1 (Q1 2026) and 1,421.0 (Q2 2026, +8.0 %). GP/TEU (EUR): 396.5, 364.8, 335.6, 309.1, 320.5 and 333.2 (-8.7 %). Ocean gross profit Q2 2026: 285.2, -1.8 %. | PDF p. 5 (narrative; Q2 revenue +8.0 %, gross profit -1.8 %) plus `DHL Global Forwarding`, rows `Revenue - Ocean Freight`, `Ocean freight gross profit per TEU (EUR)` and `Gross profit - Ocean Freight`. |
| 6 | How did the DHL Express EBIT margin develop quarter by quarter from Q1 2025 to Q2 2026, and what does management credit for the Q2 2026 jump? | both | Margin: 10.8 %, 12.4 %, 11.8 %, 16.4 %, 13.3 % and 16.8 %. EBIT EUR 662.3m to 1,200.2m (Q2 2026, +64.3 % vs 730.4m). Management: air-freight capacity constraints added about EUR 150m, plus strict cost discipline and yield management. H1 EBIT was EUR 1,999m at 15.2 %. | `DHL Express`, rows `EBIT margin (%)` and `EBIT`; PDF p. 4. |
| 7 | Can you recommend a good science-fiction novel for the weekend? | off-topic | The guardrail should decline: the question is not about DHL Group's results. | none |
