# UHC Finance Lens: Global Health Financing & UHC Assistant (starter kit)

A multi-agent RAG assistant for a health-financing policy advisor. It combines the WHO/World
Bank **2025 UHC Global Monitoring Report** (PDF: who lacks service coverage and who faces
financial hardship) with WHO's **Global Health Expenditure Database (GHED)** (XLSX: how much
each country spends on health and who pays for it).

Verified on 2026-10-06 by re-downloading both files (sha256 below) and rebuilding the workbook.

## 1. Sources

| Role | File | URL (final, no redirect) | Size | sha256 (first 16) |
|---|---|---|---|---|
| XLSX | `GHED_data.xlsx` (served as `GHED_data.XLSX`, Content-Type `MS Excel`) | https://apps.who.int/nha/database/Home/IndicatorsDownload/en | 38,924,260 B (37.1 MiB) | `8cf7c04ede53281a` |
| PDF | `9789240117808-eng.pdf` | https://iris.who.int/server/api/core/bitstreams/04e720a0-bf22-4fb8-ad42-d6296bba739f/content | 4,223,931 B (4.03 MiB), 108 pages | `c0b1492c8843824c` |

Landing pages: https://apps.who.int/nha/database (GHED) and
https://www.who.int/publications/i/item/9789240117808 (report).
The GHED `Version` sheet says: "Last updated: December 12th, 2025. Year 2023 added for
December 2025 update. Data for 2024 are preliminary and subject to revision." The download
URL always serves the latest release, so record the sha256 of the copy you use.

### Licence and attribution (read this: the XLSX is NOT openly licensed)
- **PDF**, page 4: "This work is available under the Creative Commons
  Attribution-NonCommercial-ShareAlike 3.0 IGO licence (CC BY-NC-SA 3.0 IGO)". Non-commercial
  use only; adaptations must carry the same licence. Suggested citation: *Tracking universal
  health coverage: 2025 global monitoring report. Geneva: World Health Organization and the
  International Bank for Reconstruction and Development / The World Bank; 2025. Licence: CC
  BY-NC-SA 3.0 IGO.*
- **XLSX**: the GHED site says only "open access to the public". WHO's general *Terms and
  conditions of use for WHO data compilations, aggregations, evaluations and analyses*
  (https://www.who.int/about/policies/publishing/data-policy/terms-and-conditions) grant "the
  royalty-free, worldwide, non-exclusive right to use, reproduce, extract, download, copy,
  distribute, display or include the Datasets ... **for public health purposes**". They also
  say "Any other alteration or modification of the Datasets (including abbreviations,
  additions, or deletions) may be made only with the prior written authorization of WHO" and
  that datasets "shall not be used for or in conjunction with the promotion of a commercial
  enterprise". Ignore aggregator sites that label GHED as CC0.
  - Practical advice: download the raw file yourself and run `clean.py`
    locally. Keep the derived workbook in a private repository and do not republish it as
    "WHO data". Cite WHO. The project is non-commercial only.
- Attribution text for the app or slides: *"Health spending data: World Health Organization,
  Global Health Expenditure Database (GHED), December 2025 update, apps.who.int/nha/database,
  accessed 6 October 2026; underlying data reported by WHO Member States. UHC findings: WHO and
  World Bank, Tracking universal health coverage: 2025 global monitoring report, CC BY-NC-SA
  3.0 IGO."*

## 2. Rebuild the cleaned workbook

```bash
cd datasets/archive/uhc-finance-lens
python -I scripts/clean.py \
    raw/GHED_data.xlsx \
    uhc-finance-lens_clean.xlsx
```
The script needs only `openpyxl` and takes about 15-25 seconds; the raw file holds about 19
million cells.

## 3. Why the raw file cannot be ingested as-is, and what clean.py does

The raw workbook has 4 sheets: `Data` (4,612 country-years x **4,120 columns**), `Codebook`
(4,120 rows), `Metadata` (32,663 rows) and `Version` (a title block). The narrator would turn it
into about **144,000 chunks**, with column codes like `che_gdp` and floats like
`3.2148544799999996`. clean.py:
1. Keeps the years **2023** (latest; 195 countries) and **2019** (pre-pandemic baseline; 195).
   2024 is dropped: it is preliminary and covers 22 countries.
2. Keeps 4 ID columns, 10 headline financing indicators and population. GHED codes become
   readable headers with units, e.g. `oops_che` becomes `Out-of-Pocket (OOP) Share of Health
   Spending (%)` and `che_pc_usd` becomes `Health Spending per Person (current US$)`.
3. Expands codes: WHO region `AFR` becomes `African Region`, and so on; `Upper-middle` becomes
   `Upper-middle income`.
4. Labels the income group as **(2021 classification)**. The Codebook defines `income` as
   "Country income group 2021 (World Bank)", and the same value is used for every year.
5. Rounds percentages and population (thousands) to 1 decimal and US$ to whole dollars. Blank
   cells stay blank, and the narrator skips them.
6. Writes one sheet per year (header in row 1, Country in column A, sorted A-Z).
7. Adds **`Rankings 2023`**, a derived table with the top 3 and bottom 3 countries for each
   indicator. A superlative question ("which country has the highest X?") then needs one
   retrieved chunk. Without it, a top-5 retriever cannot see all 194 country rows.
8. Adds **`Indicator Definitions`**, taken from GHED's own `Codebook` (official name, formula,
   unit) plus short plain-language notes.

## 4. Cleaned workbook: `uhc-finance-lens_clean.xlsx`

| Sheet | Column A (subject) | Rows | Narrated chars | Chunks |
|---|---|---|---|---|
| Health Spending 2023 | Country | 195 | 138,778 | 195 |
| Health Spending 2019 | Country | 195 | 139,354 | 196 |
| Rankings 2023 (derived) | Indicator | 10 | 2,853 | 4 |
| Indicator Definitions | Indicator | 12 | 4,925 | 7 |
| **Total** | | **412** | **285,910** | **402** |

Chunk counts are exact: the reference narrator followed by the reference LangChain splitter
(1200 characters, 240 overlap, splitting on newlines first). Each country row is about 710
characters, so two rows never fit in one 1200-character chunk and **each country-year becomes
its own chunk**. This is good for precision, but it means a quick stride-based
estimate (about 298) undercounts. The PDF adds about 239k characters (about 250 chunks), for a
project total of about 650 chunks.

Narration samples (the reference narrator with the LLM summary off):
```
Health Spending 2023 — Country 'Nigeria': ISO3 Code is NGA; WHO Region is African Region; World Bank Income Group (2021 classification) is Lower-middle income; Health Spending (% of GDP) is 4.2; Health Spending per Person (current US$) is 67; Government Share of Health Spending (%) is 14.3; Government Health Spending (% of GDP) is 0.6; Health Share of Government Spending (%) is 4.3; Government Health Spending per Person (current US$) is 10; Out-of-Pocket (OOP) Share of Health Spending (%) is 71.9; Out-of-Pocket (OOP) Spending per Person (current US$) is 48; External Funding Share of Health Spending (%) is 11.3; Voluntary Health Insurance Share of Health Spending (%) is 0.9; Population (thousands) is 227882.9.
Health Spending 2019 — Country 'India': ISO3 Code is IND; WHO Region is South-East Asia Region; World Bank Income Group (2021 classification) is Lower-middle income; Health Spending (% of GDP) is 3; Health Spending per Person (current US$) is 61; Government Share of Health Spending (%) is 35.1; Government Health Spending (% of GDP) is 1; Health Share of Government Spending (%) is 3.8; Government Health Spending per Person (current US$) is 21; Out-of-Pocket (OOP) Share of Health Spending (%) is 52; Out-of-Pocket (OOP) Spending per Person (current US$) is 32; External Funding Share of Health Spending (%) is 0.6; Voluntary Health Insurance Share of Health Spending (%) is 7.7; Population (thousands) is 1389030.3.
Rankings 2023 — Indicator 'Out-of-Pocket (OOP) Share of Health Spending (%)': Highest 3 Countries is Armenia (80.5), Bangladesh (79.3), Turkmenistan (77.4); Lowest 3 Countries is Tuvalu (0.0), Niue (0.4), Nauru (0.7); Countries With Data is 194.
```
The narrator prints whole-number floats without a decimal point (`52.0` becomes `52`). This is
expected.

## 5. Data caveats (tell the answer agent)
- **The shares are not a partition.** Government, OOP and external shares are classified by
  *source of funds*; voluntary health insurance by *financing scheme*. They can overlap, so
  their sum exceeds 100% for South Africa (115.7%), Namibia (114.0%) and the Bahamas (107.9%)
  in 2023, and for Burundi (103.7%) in 2019. "Government" means *domestic* general government,
  including social health insurance contributions; donor money is in the external share.
- **US$ values are nominal, at market exchange rates.** For example, Nigeria's health spending
  per person was flat (US$68 in 2019, US$67 in 2023) while its share of GDP rose from 3.0% to
  4.2%: this is a currency effect, not a spending cut.
- Gaps: Ukraine 2023 has **no** spending indicators (population only). Cuba 2023 lacks the US$
  indicators. The external share is blank for 19 mostly high-income countries and the VHI
  share for 16 (2023).
- Outliers are real, not errors: small island states top the GDP-share rankings (Tuvalu
  27.1%, Nauru 18.2%), and Afghanistan's government share is 1.7%.
- Many 2023 values are WHO estimates (see the raw `Metadata` sheet, column "Data type"), and
  GHED revises past years every December. The 2019 figures here are the December 2025 vintage.
- Similar names can confuse keyword search: Niger and Nigeria; Congo and Democratic Republic
  of the Congo; Guinea, Guinea-Bissau and Equatorial Guinea; Sudan and South Sudan; Dominica
  and Dominican Republic.
- GHED does **not** contain the report's SDG 3.8.1 and 3.8.2 values. Those are in the PDF's
  **Annex 3** country table (PDF pages 96-103, printed pages 74-81): service coverage index
  (2023) and financial hardship (% of population, latest year) for every country. That table
  is the natural bridge between the two sources.
- Rankings use values rounded to 1 decimal. Equal displayed values are ties: 42 countries show
  a 0.0% external share, so the sheet says so instead of naming a single "lowest" country.

## 6. Verified demo answer key

Every answer was checked against `uhc-finance-lens_clean.xlsx` and the PDF text. PDF
page numbers are PDF page indexes, with the printed page in brackets.

| # | Question | Needs | Expected answer | Evidence |
|---|---|---|---|---|
| 1 | Which country had the highest out-of-pocket share of health spending in 2023, and what was it? **(best before/after demo; superlative)** | xlsx | **Armenia, 80.5%** of current health spending. The answer is unique: Bangladesh 79.3%, Turkmenistan 77.4%. Armenia was also highest in 2019 (84.8%). | `Rankings 2023`, row `Out-of-Pocket (OOP) Share of Health Spending (%)`; `Health Spending 2023` row `Armenia`. The PDF gives no country OOP shares. |
| 2 | In 2023, how was Nigeria's health spending financed? | xlsx | Out-of-pocket 71.9%, government 14.3%, external 11.3%, voluntary insurance 0.9%. Total health spending was 4.2% of GDP, US$67 per person. | `Health Spending 2023`, row `Nigeria` |
| 3 | Did India's reliance on out-of-pocket payments fall between 2019 and 2023? | xlsx | Yes. The OOP share fell from 52.0% (2019) to 43.9% (2023), the government share rose from 35.1% to 39.0%, and spending per person rose from US$61 to US$85. | `Health Spending 2019` and `Health Spending 2023`, rows `India` |
| 4 | What was the United States' health spending per person and as a share of GDP in 2023? | xlsx | US$13,473 per person, the highest of 193 countries (Switzerland is next at US$11,784); 16.7% of GDP; government share 54.0%. | `Health Spending 2023` row `United States of America`; `Rankings 2023` row `Health Spending per Person (current US$)` |
| 5 | According to the 2025 UHC monitoring report, how many people lack essential health services and how many face financial hardship? | pdf | 4.6 billion people lacked essential health services in 2023, and 2.1 billion faced financial hardship in 2022. Of these, 1.6 billion faced impoverishing OOP spending; that rate fell from 29% (2000) to 20% (2022). | PDF page 18 (printed xvi) and page 19 (xvii); Foreword, page 8 (vi) |
| 6 | How has the UHC service coverage index changed, and where is it heading by 2030? | pdf | It rose from 54 points (2000) to 71 (2023). Progress in the SDG era is one-third of the 2000-2015 annual rate. At the current pace it "will only reach 74 points in 2030", below 80. | PDF page 33 (printed 11); page 18 (xvi), Fig. 3 |
| 7 | Nigeria pays most of its health bill out of pocket. What are its UHC service coverage index and financial-hardship rate? | both | XLSX: OOP 71.9% of health spending, government only 14.3% (2023). PDF Annex 3: service coverage index **47** (2023); **47.4%** of the population in financial hardship (2022). | `Health Spending 2023` row `Nigeria`; PDF page 100 (printed 78), Annex 3 |
| 8 | What share of out-of-pocket spending goes on medicines? | pdf | More than half in most countries; the typical share is 56%. | PDF page 20 (xviii); page 57 (printed 35) |
| 9 | What's the best way to train for a marathon? | off-topic | The relevance guardrail should decline: this is not about health financing or UHC monitoring. | none |

## 7. PDF notes (Azure Document Intelligence)
- 108 pages with a text layer. Pages 1-3 and 105-108 (covers) are image-only. Pages 22, 32
  and 46 are section dividers. Figures carry their numbers only as axis labels.
- **The file is 4,223,931 bytes, over the Free (F0) tier's 4 MB limit**, and F0 analyses only
  2 pages per request. Split it into 54 two-page PDFs, or use the S0 tier.
- Annex 3 (PDF pages 96-103) is a country table. `result.content` flattens it to text, row by
  row, so check that country, index and percentage stay adjacent in the chunks.
