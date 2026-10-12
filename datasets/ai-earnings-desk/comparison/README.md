# AI earnings comparison workbook

A cross-company comparison of NVIDIA, AMD, Broadcom, Intel, Micron, Microsoft, Alphabet and Meta for the
12 calendar quarters CY2023 Q3 to CY2026 Q2. It is built only from the SEC XBRL company facts already in this
corpus (`<TICKER>/xbrl/<TICKER>_companyfacts.json`, downloaded 2026-10-06), so it covers the newest quarters for
which SEC no longer publishes `Financial_Report.xlsx`. Nothing new was downloaded to build it.

The workbook is shaped for the reference repo's Excel Narrator. On every sheet, row 1 is the header and column A
is the row's subject, with one record per row. There are no merged cells and no title rows, headers carry their
units, and cells with no value are left blank, so the narrator skips them.

| File | What it is |
|---|---|
| `ai_earnings_comparison.xlsx` | The workbook: 13 sheets, listed below |
| `ai_earnings_provenance.csv` | One row per value (1,928 rows): company, calendar quarter, fiscal period, metric, value, units, XBRL tag(s), accession number(s), filed date(s), period start and end, derived (yes/no), and how it was derived. It also includes the 32 revenue values from the 4 quarters before the window, which are used only for growth. |
| `../scripts/build_comparison.py` | Builds both files and prints the internal checks. Re-run from the repository root with any Python that has openpyxl: `python -I datasets/ai-earnings-desk/scripts/build_comparison.py` |

Sources: SEC filings and XBRL data are public records. The companies' press releases in `<TICKER>/ir/` were read
only to validate the numbers. No IR content is copied into the workbook.

## Sheets

| Sheet | Rows | One row per | Column A |
|---|---:|---|---|
| Quarterly results | 96 | company and calendar quarter (8 × 12) | Company quarter, e.g. `NVIDIA CY2025 Q2` |
| Revenue, Revenue growth YoY, Gross margin, Operating margin, Net income, R&D expense, Capital expenditures, Free cash flow | 8 each | company; columns `CY2023 Q3` … `CY2026 Q2`, then `Units` | Company |
| Annual results | 24 | company and fiscal year whose Q4 falls in the window (3 each) | Company fiscal year, e.g. `NVIDIA FY2026` |
| Rankings | 40 | ranking: highest or lowest for CY2026 Q2 and for the whole window | Ranking, e.g. `Highest revenue, CY2026 Q2` |
| Companies | 8 | company: ticker, SEC entity name, CIK, fiscal year end, how fiscal quarters map to calendar quarters | Company |
| Definitions | 27 | metric or term: meaning, XBRL tags used per company, derivation, units | Term |

The Quarterly results columns are:
- **Who and when:** Company, Ticker, Calendar quarter, Fiscal period, Period start, Period end.
- **Amounts:** Revenue, Cost of revenue, Gross profit, R&D expense, Operating income, Net income, Diluted EPS, Operating cash flow, Capital expenditures, Free cash flow, Cash and cash equivalents at quarter end.
- **Ratios:** Gross margin, Operating margin, Net margin, R&D as % of revenue, Revenue growth YoY.
- **Notes:** fiscal Q4 rows, 14-week quarters, and anything not available.

Annual results has the same metric columns, taken from the full-year facts. Its growth column compares each fiscal year with the one before.

The window rankings state their basis in the label. They are one of three things:
- a sum of the 12 quarters (revenue, net income, R&D, capital expenditures, free cash flow)
- a ratio of 12-quarter totals (gross margin, operating margin, R&D as % of revenue)
- a change from CY2023 Q3 to CY2026 Q2 (revenue growth, and gross- and operating-margin change in percentage points)

## Rules

**Facts**
- Only us-gaap facts from 10-Q, 10-K and their /A amendments are used.
- When several filings report the same tag and period, the most recently filed value wins. Restatements and stock-split adjustments therefore win: NVIDIA and Broadcom both split 10-for-1 in 2024.

**Period lengths** (days, both ends counted)

| Period | Days |
|---|---|
| Quarter | 80–100 |
| Six months | 170–190 |
| Nine months | 260–285 |
| Full year | 350–380 |

**Calendar quarter**
- The calendar quarter of a fiscal period is the calendar quarter that holds the period's midpoint. This maps each company's fiscal quarters one-to-one onto calendar quarters.
- The mapping was checked for all 16 quarters (window plus base) of all 8 companies. The Companies sheet gives each company's mapping. For example, Micron's CY2026 Q2 is its fiscal quarter of 27 Feb to 28 May 2026.
- Fiscal year N is the one that ends in calendar year N. The labels match the fiscal year and period of the filing that first reported each quarter.

**Income-statement items**
- The 3-month fact is used when there is one.
- Otherwise, typically for fiscal Q4, the quarter is the full year minus the nine-month year-to-date total, using the same tag. These values are marked derived.

**Cash-flow items**
- 10-Qs report cash flows year to date, so the quarters are differences between year-to-date totals:
  - Q1 = three months
  - Q2 = six months − three months
  - Q3 = nine months − six months
  - Q4 = full year − nine months
- These values are marked derived.
- Where a 3-month fact exists, it is used as is (Microsoft reports 3-month cash flows in its 10-Qs).

**Balance-sheet items:** the value at the period end date.

**Tags:** tags are tried in the order listed in the script, and the tag actually used is recorded for every value.
- Alphabet switched its revenue tag from `RevenueFromContractWithCustomerExcludingAssessedTax` to `Revenues` in CY2025 Q2. The two tags give identical values on the 7 periods that both cover.
- NVIDIA's capital expenditures use `PaymentsToAcquireProductiveAssets`. Its cash-flow line is "Purchases related to property and equipment and intangible assets".

**Gross profit:** Alphabet and Meta report no `GrossProfit`, so for them it is revenue minus cost of revenue, marked derived. For the other six companies, revenue − cost of revenue equals their reported gross profit in every quarter.

**Free cash flow:** operating cash flow minus capital expenditures, marked derived. Companies' own free-cash-flow measures can differ; for example, Intel's adjusted figure nets partner contributions and government incentives.

**Units and rounding**
- USD millions are rounded to whole millions. Every fact used was already a whole number of millions, so no rounding occurred.
- Diluted EPS is in USD, rounded to the cent.
- Percentages are shown to one decimal and computed from unrounded values.

## Departures from the standard method

1. **Fiscal Q4 diluted EPS is blank (24 cells).**
   - The standard method derives Q4 as full year minus nine months. That is exact for amounts, but not for per-share figures, because the share count changes between periods. 10-Ks report only full-year EPS, so the true Q4 figure is not in the XBRL data.
   - Checked against the companies' 23 Q4 press releases in the corpus, full year minus nine months matched only 12. It missed by up to $0.04 (Meta FY2024 Q4: $7.98 against a reported $8.02).
   - Dividing derived Q4 net income by a Q4 share count estimated from the annual and nine-month averages matched 18 of 23, still off by up to $0.04.
   - Rather than publish figures that are wrong half the time, those cells are blank, and the Notes column and Definitions say why. The Q4 press releases in `<TICKER>/ir/` have the reported figures.
   - Full-year EPS (Annual results) and Q1–Q3 EPS are reported values, and they match the releases checked.
   - The `additive=False` flag on the EPS metric in `build_comparison.py` controls this.
2. **Broadcom net income uses `ProfitLoss`.**
   - Broadcom tags its "Net income" line as `ProfitLoss`. It used `NetIncomeLoss` only for the annual facts in its FY2024 10-K (FY2022 to FY2024).
   - With `NetIncomeLoss` alone, all 12 of Broadcom's quarters would be blank.
   - `ProfitLoss` includes non-controlling interests, so the script uses it only for a company that reports no non-zero non-controlling interest since 2022. Broadcom reports none; Intel and Micron, which do, keep `NetIncomeLoss` only.
   - In the 3 periods since 2021 where Broadcom reported both tags, they are equal. Broadcom's quarterly figures match its press releases ($13,088M for FY2026 Q3).
3. **Intel's cash and cash equivalents use `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents`** at 6 quarter ends: CY2023 Q3 to CY2024 Q3, CY2025 Q1 and FY2023.
   - Until mid-2025, Intel tagged its balance-sheet line "Cash and cash equivalents" with that tag, and did not use `CashAndCashEquivalentsAtCarryingValue`.
   - Each value used matches the balance-sheet line in SEC's own `Financial_Report.xlsx` for the same filings: $7,621M, $7,079M, $6,923M, $11,287M, $8,785M and $8,947M.
   - The fallback is only used where `CashAndCashEquivalentsAtCarryingValue` is missing. At 2025-12-27, where both exist, they differ ($14,265M against $14,712M), and the balance-sheet value is the one used.

Extra columns and rankings, all of which can be ignored:
- a Notes column in Quarterly results and Annual results
- a Units column on the per-metric sheets, so every narrated row states its unit
- gross- and operating-margin change rankings
- ticker, period type, fiscal period and units columns in the provenance CSV

## Validation

### Latest quarter (CY2026 Q2) against the company's own release

All values are in USD millions.

| Company | Fiscal period | Revenue (workbook) | Revenue (company) | Net income (workbook) | Net income (company) | Company source | Match |
|---|---|---:|---:|---:|---:|---|---|
| NVIDIA | FY2027 Q2 | 96,221 | 96,221 | 59,688 | 59,688 | `NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_press_release.html` | yes, exact |
| AMD | FY2026 Q2 | 11,536 | 11,536 | 2,297 | 2,297 | `AMD/ir/FY2026-Q2/AMD_FY2026-Q2_press_release.pdf` | yes, exact |
| Broadcom | FY2026 Q3 | 29,591 | 29,591 | 13,088 | 13,088 | `AVGO/ir/FY2026-Q3/AVGO_FY2026-Q3_press_release.pdf` | yes, exact |
| Intel | FY2026 Q2 | 16,128 | 16,128 | −11,033 | −11,033 (attributable to Intel) | `INTC/ir/FY2026-Q2/INTC_FY2026-Q2_press_release.pdf` | yes, exact |
| Micron | FY2026 Q3 | 41,456 | 41,456 | 28,243 | 28,243 | `MU/ir/FY2026-Q3/MU_FY2026-Q3_slides.pdf` (GAAP table; the corpus has no press release for this quarter) | yes, exact |
| Microsoft | FY2026 Q4 | 90,007 | 90,007 | 35,766 | 35,766 | `MSFT/ir/FY2026-Q4/MSFT_FY2026-Q4_press_release.docx` | yes, exact |
| Alphabet | FY2026 Q2 | 119,796 | 119,796 | 112,193 | 112,193 | `GOOGL/ir/FY2026-Q2/GOOGL_FY2026-Q2_press_release.pdf` | yes, exact |
| Meta | FY2026 Q2 | 60,801 | 60,801 | 15,848 | 15,848 | `META/ir/FY2026-Q2/META_FY2026-Q2_press_release.pdf` | yes, exact |

Diluted EPS also matches for the seven companies whose latest quarter is not a fiscal Q4: 2.46, 1.38, 2.68, −2.16, 24.67, 9.11 and 6.18. Microsoft's latest quarter is fiscal Q4, so its EPS cell is blank; its release reports $4.81.

### Derived fiscal-Q4 values against Q4 releases

There are 24 fiscal Q4s in the window (3 per company). Their derived revenue and net income were compared with each company's Q4 release:
- 47 of the 48 values match exactly.
- The exception is Alphabet FY2025 Q4 revenue: 113,829 against a reported 113,828. Alphabet's reported nine-month revenue (289,007) is $1M less than its three reported quarters added together (289,008), so full year minus nine months comes out $1M high.
- Micron FY2023 Q4 was checked against the prior-year column of its FY2024 Q4 release, because the corpus's Micron IR files start at FY2024 Q1.

### Do the quarters add up to the full year?

This is the latest complete fiscal year per company: the sum of the 4 quarterly values minus the annual value, for the 9 amount metrics.

| Company | Fiscal year | Result |
|---|---|---|
| NVIDIA | FY2026 | all 9 exact |
| AMD | FY2025 | all 9 exact |
| Broadcom | FY2025 | all 9 exact |
| Intel | FY2025 | all 9 exact |
| Micron | FY2025 | all 9 exact |
| Microsoft | FY2026 | all 9 exact |
| Alphabet | FY2025 | revenue and cost of revenue +$1M (Alphabet's own rounding, as above); other 7 exact |
| Meta | FY2025 | revenue, gross profit and R&D −$1M (Meta's own rounding); other 6 exact |

Q4 is defined as the full year minus nine months, so for the income statement this test checks that the three reported quarters add up to the reported nine-month total. For cash flow, the quarters are year-to-date differences, so they add up by construction. The script runs the same check for every fiscal year in the window, and the largest difference anywhere is $2M (Meta FY2024 gross profit, from the same rounding).

### Implausible or unusual values

There is no negative revenue and no gross margin above 100%. Values that look extreme were all checked against the companies' own figures and are real:
- **Micron, negative gross margin:**
  - CY2023 Q3 (fiscal Q4 FY2023): −10.8%, gross profit −435 on revenue 4,010
  - CY2023 Q4: −0.7%, −35 on 4,726
  - FY2023: −9.1%, −1,416 on 15,540

  This was the 2023 memory downturn, and the figures match Micron's FY2024 Q1 and Q4 releases.
- **Intel, large losses:**
  - CY2024 Q3: operating margin −68.2% and net margin −125.3%, a net loss of −16,639 on revenue of 13,284 (Q3 2024 release)
  - FY2024: net margin −35.3% (−18,756 on 53,101)
  - CY2026 Q1: −27.5% (−3,728 on 13,577)
  - CY2026 Q2: −68.4% (−11,033 on 16,128)
- **Growth above 200%:**
  - NVIDIA CY2023 Q3 to CY2024 Q1: 205–265%
  - Micron CY2026 Q2: 345.7% (revenue 41,456 against 9,301)
- **Alphabet CY2026 Q2:** net income of 112,193 on revenue of 119,796, and free cash flow of −5,855 (capital expenditures of 44,924). Its release shows the same net income.
- **No missing quarters:** all 8 companies have all 16 quarters (12 in the window plus 4 base quarters). Apart from the 24 fiscal-Q4 EPS cells, no cell is blank.

### Other checks printed by the script

- Every fiscal year end matches the company's stated rule; for example, NVIDIA's is the last Sunday in January. There are no gaps between consecutive fiscal years.
- No filing reports two different values for the same tag and period.
- Seven values differ from the first-filed figure:
  - six EPS values restated for the NVIDIA and Broadcom stock splits
  - Meta's FY2023 capital expenditures (27,266 revised to 27,045)

  Meta revised all four 2023 year-to-date capex figures together, so the derived 2023 quarters are on one basis.

### Narration check

The narration check was run with the reference narrator, offline, with `--preview-rows 2`:
- 13 sheets and 259 data rows.
- Quarterly results 96, the 8 per-metric sheets 8 each, Annual results 24, Rankings 40, Companies 8, Definitions 27.
- Every sheet passes the row-1-header test.
- About 141,000 narrated characters, or about 147 chunks.

Sample sentences:

> Quarterly results — Company quarter 'NVIDIA CY2023 Q3': Company is NVIDIA; Ticker is NVDA; Calendar quarter is CY2023 Q3; Fiscal period is FY2024 Q3; Period start is 2023-07-31; Period end is 2023-10-29; Revenue (USD millions) is 18120; … Diluted EPS (USD) is 0.37; … Gross margin (%) is 74; Operating margin (%) is 57.5; Net margin (%) is 51; R&D as % of revenue is 12.7; Revenue growth YoY (%) is 205.5.

> Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; Runner-up is NVIDIA; Runner-up value is 96221; Units is USD millions.

> Companies — Company 'NVIDIA': Ticker is NVDA; SEC entity name is NVIDIA CORP; CIK is 0001045810; Fiscal year end is Last Sunday in January (52/53-week year), latest full year ended 2026-01-25; Fiscal quarters vs calendar quarters is Each fiscal quarter ends about 4 weeks after the end of the calendar quarter it is assigned to. …

## Known limitations

- **Segment revenue is not available.** SEC company facts exclude segment-level (dimensional) facts, so figures such as NVIDIA's Data Center revenue are missing. Definitions says so.
- **Fiscal Q4 diluted EPS is blank,** as explained under Departures above.
- **Calendar quarters are an alignment, not identical periods.**
  - NVIDIA's and Broadcom's quarters end about a month after the calendar quarter they are assigned to, and Micron's about a month before.
  - Broadcom's FY2024 Q1 (CY2023 Q4) had 14 weeks.
- **The data is a snapshot from 2026-10-06.** Download newer company facts and re-run the script to extend it. The window and company list are constants at the top of the script.
