# PortPulse: Container Port Performance Copilot (starter kit)

A multi-agent RAG assistant for a shipping-line network planner. It answers "why" questions from the
World Bank / S&P Global *Container Port Performance Index 2020 to 2024* report (PDF) and
"how much / which port" questions from the report's official data annex (XLSX). Both files come from
the same publication and describe the same 403 ports. All 403 annex rows in the PDF match the XLSX
on every number.

Verified on 2026-10-06 by re-downloading both files and checking them against each other.

## Sources

| File | Where | Size | sha256 |
|---|---|---|---|
| `CPPI_2020_to_2024_Annex.xlsx` (raw, in `raw/`) | Reliable direct link (DSpace REST): `https://openknowledge.worldbank.org/server/api/core/bitstreams/6d1086f0-13ed-4d69-92ad-11d93f3e7df6/content` | 67,886 bytes | `6ac577884a8f6fd839626923ae02a020dfe28b95c234e06189700dd236d8aa9e` |
| CPPI 2020-2024 report PDF (96 pages) | `https://documents1.worldbank.org/curated/en/099091925073051415/pdf/P509479-9781ba16-3dbc-4064-9d8f-8eaf9951e09d.pdf` | 9,064,809 bytes (8.64 MB) | `7d46ba0a64476e9c3be9b91e7e1b7b075f7953b98558253faac079a6da880311` |

- Landing pages: https://www.worldbank.org/en/topic/transport/publication/cppi-2024 and the Open Knowledge
  Repository record https://hdl.handle.net/10986/43744 (DOI 10.1596/43744, issued 2025-09-22).
- Download warning: the repository's "download" link
  (`https://openknowledge.worldbank.org/bitstreams/6d1086f0-13ed-4d69-92ad-11d93f3e7df6/download`) is
  unreliable. In testing, about 1 in 3 requests returned a 650 KB HTML login page with HTTP 200, so
  `curl --fail` does not catch it. Use the REST `/content` link above and check the file with
  `file CPPI_2020_to_2024_Annex.xlsx` (it should say "Microsoft Excel 2007+").

```bash
curl -L --fail -A "Mozilla/5.0" -o CPPI_2020_to_2024_Annex.xlsx \
  "https://openknowledge.worldbank.org/server/api/core/bitstreams/6d1086f0-13ed-4d69-92ad-11d93f3e7df6/content"
curl -L --fail -A "Mozilla/5.0" -o cppi_2020_2024.pdf \
  "https://documents1.worldbank.org/curated/en/099091925073051415/pdf/P509479-9781ba16-3dbc-4064-9d8f-8eaf9951e09d.pdf"
```

## Licence and attribution

- PDF, copyright page (p. 4): "This work is available under the Creative Commons Attribution
  Non-commercial 3.0 IGO license (CC BY-NC 3.0 IGO) ... you are free to copy, distribute, transmit, and
  adapt this work, for noncommercial purposes as long as full attribution to this work is given."
- XLSX: the repository record that holds the annex has `dc.rights = CC BY-NC 3.0 IGO`
  (https://creativecommons.org/licenses/by-nc/3.0/igo), rights holder World Bank.
- **Non-commercial only.** A commercial product is not allowed.
- Attribution text (from the report): *World Bank, 2025. The Container Port Performance Index 2020 to
  2024: Trends and Lessons Learned, Washington DC: World Bank, License: Creative Commons Attribution
  Non-commercial CC BY-NC 3.0 IGO.*
- The cleaned workbook is an adaptation, so add the report's disclaimer: *"This is an adaptation of an
  original work by the World Bank. Views and opinions expressed in the adaptation are the sole
  responsibility of the author or authors of the adaptation and are not endorsed by the World Bank."*

## Cleaning (`scripts/clean.py`)

```bash
python -I scripts/clean.py raw/CPPI_2020_to_2024_Annex.xlsx portpulse_clean.xlsx
```

The raw annex is already tidy: one sheet `Annex`, 403 rows x 18 columns, header in row 1, Port in
column A, no duplicate ports or LOCODEs, and numbers stored as numbers. The narrated sentences are
still hard to read, so the script makes these light changes:

1. Expands codes using the report's Annex notes: `ECA` becomes "Europe & Central Asia", `SSA`
   "Sub-Saharan Africa", `HI` "High income", `LMI` "Lower middle income", and so on. The PDF uses the
   full names, so this also helps keyword (BM25) retrieval.
2. Renames columns so that they carry the year and the direction: `Rank` becomes "Global rank 2024
   (1 = best of 403)", and the sub-indices become "Statistical index 2024" and "Administrative
   index 2024". The PDF annex header confirms that these are 2024 values.
3. `Berth hours in % of port hours` is stored as a fraction (0.9) but would be narrated as "0.9".
   The script converts it to a percentage: "Time at berth 2024 (% of time in port)" = 90.
4. Adds one helper column, "Change 2023 to 2024 (CPPI points)", calculated from the rounded scores.
5. The three group-average columns repeat the same value on every row of a group (for example, "-81"
   on all 39 Sub-Saharan rows). The script moves them to a new **Group summary** sheet with one row
   per group, as listed under "Cleaned workbook" below. That sheet also gives each group's best- and
   worst-ranked port and its biggest improver and decliner, so "highest" and "lowest" questions can
   be answered from a single retrieved row.
6. Adds a small **Glossary** sheet: higher is better, the 2024 average is about 0, what a blank year
   means, and what time at berth means.

Blank year scores stay blank because the narrator skips empty cells. A blank means the port was not in
the index that year.

## Cleaned workbook: `portpulse_clean.xlsx`

| Sheet | Rows | Columns | Column A (subject) |
|---|---|---|---|
| Ports | 403 | 16 | Port |
| Group summary | 32 | 8 | Group (1 "All 403 ports" row, 7 World Bank regions, 4 income groups, 20 maritime regions) |
| Glossary | 7 | 2 | Term |
| **Total** | **442** | | |

Estimated chunks: **224**. This is an exact count from running the reference narrator and the
reference splitter (1200 characters, 240 overlap) over the workbook. The raw file gives 203.

Narration samples:

```
Ports — Port 'Durban': UN/LOCODE is ZADUR; Country or territory is South Africa; World Bank region is Sub-Saharan Africa; Income group is Upper middle income; Maritime region is Southern Africa; CPPI 2020 score is -108; CPPI 2021 score is -246; CPPI 2022 score is -220; CPPI 2023 score is -206; CPPI 2024 score is -721; Global rank 2024 (1 = best of 403) is 403; Change 2023 to 2024 (CPPI points) is -515; Time at berth 2024 (% of time in port) is 52; Statistical index 2024 is -454; Administrative index 2024 is -989.
Group summary — Group 'Sub-Saharan Africa': Group type is World Bank region; Ports in index is 39; Average CPPI 2024 (as published) is -81; Best-ranked port 2024 is Dakar (Senegal), CPPI 2024 score 23, rank 108; Worst-ranked port 2024 is Durban (South Africa), CPPI 2024 score -721, rank 403; Biggest improvement 2023 to 2024 is Cape Town (South Africa), +238 points (-519 in 2023 to -281 in 2024); Biggest decline 2023 to 2024 is Durban (South Africa), -515 points (-206 in 2023 to -721 in 2024).
Glossary — Term 'Time at berth (% of time in port)': Meaning is the share of a ship's total time in port that is spent at the berth; the rest is time waiting at anchor or arriving. The vessel's departure time is not included. A low share points to long waiting times at anchor.
```

## Data-quality notes (found while verifying)

- **Sign convention:** CPPI scores are centred so that the 2024 average of all 403 ports is 0 (the
  workbook mean is 0.01). Negative means slower than average. Durban's -721 is real: it matches the
  mean of its statistical (-454) and administrative (-989) indices, -721.5, within rounding. Add the convention to the answer
  agent's prompt as well.
- **One wrong cell in the raw XLSX:** La Guaira (Venezuela, lower-middle income) has an income-group
  average of -3.4 (the upper-middle value). The PDF annex prints -2.5. The Group summary uses -2.5.
- **Published MENA (22.6) and South Asia (30.9) averages do not match their own port rows.** The port
  rows list 37 MENA ports with a mean of 23.3 and 15 South Asia ports with a mean of 30.3. The
  published figures count Pakistan's two ports (Karachi and Muhammad Bin Qasim, labelled MENA) in
  South Asia: MENA without them averages 22.6, and South Asia with them averages 31.1. The Group
  summary keeps the published values and its glossary explains the difference.
- **Missing years:** 79 ports have no 2020 score, 55 none for 2021, 64 none for 2022 and 16 none for
  2023. All ports have 2024 values. The few exact zeros (for example Acajutla 2020) are real values
  that the PDF annex also shows.
- **Rounding:** XLSX scores are integers; the PDF tables use one decimal. Port Said (137.4) and
  Dalian (136.5) both appear as 137; the rank (3 vs 4) separates them. The change column can differ by
  about 1 point from Table 2.3 (Imbituba: +125 here, +124.1 in the PDF).
- **Edition:** in this report the 2020-2023 scores were recalculated on a 2024 basis (PDF p. 69). Do
  not mix them with scores printed in earlier CPPI reports.
- **Name variants:** "Tanger-Mediterranean" (XLSX) is "Tanger-Med" in PDF Table 2.1, and "Lagos" is
  "Lagos (Nigeria)" in Table 2.2. Hybrid search copes with both.

## PDF ingestion notes

- 96 pages, 8.64 MB, real text layer (about 2,500 characters per page). Pages 2, 3, 17, 25, 36, 43, 57
  and 96 are blank or image-only.
- **Azure Document Intelligence F0** accepts at most 4 MB per file and analyses only the first 2 pages
  per request. Split the PDF into 2-page files. A simple splitter (`pdfseparate` + `pdfunite`) produces
  5.8-6.1 MB pieces, because every page copies the shared resources, and those are still too big.
  Instead use `mutool clean -gggg -z cppi_2020_2024.pdf pages_031_032.pdf 31-32` (MuPDF), which
  produced pieces of at most 0.64 MB, all with the text layer intact.
- Pages 72-91 are the annex table and repeat the XLSX. To keep port-level numbers in the XLSX path
  (and keep the before/after demo honest), ingest pages 1-71 and 92-96 only. That is about 194
  chunks, against about 249 for all 96 pages. Project total: about 224 + 194, or roughly 420 chunks.
- Useful pages (PDF page numbers, not printed numbers): Executive Summary p. 14-15, Table 2.1 Top 20
  p. 31, Tables 2.2/2.3 improvers p. 32-33, port profiles (Dakar, JNP, Mersin, Port Said) p. 34,
  Box 4.1 Singapore p. 53, Box 4.2 Djibouti p. 54-55, Box 4.3 South Africa p. 56, methodology p. 64-66
  and 69.

## Verified demo answer key

| # | Needs | Question | Verified answer | Evidence |
|---|---|---|---|---|
| 1 | xlsx | What CPPI 2024 score and global rank did the Port of Rotterdam get, and what was its 2023 score? | Score 4, global rank 197 of 403; 2023 score 47 (down 43 points); 65% of time in port at berth. | Ports, row 'Rotterdam' (NLRTM) |
| 2 | xlsx (superlative, **best before/after demo**) | Which North American port had the best CPPI 2024 score? | Philadelphia (United States): 92, global rank 26. The answer is unique; the next is Halifax at 56 (rank 55). | Group summary, row 'North America'; Ports, row 'Philadelphia'. The PDF never says this: Philadelphia appears only in Table 2.2 (p. 32) and the annex. |
| 3 | xlsx (superlative) | Which port ranked last in the CPPI 2024, and what was its score? | Durban (South Africa): -721, rank 403 of 403. It fell from -206 in 2023 (-515 points, the largest drop of any port). Unique; next worst is Coega (Ngqura) Port at -284. | Ports, row 'Durban'; Group summary, row 'All 403 ports'. In the PDF only in the annex table (p. 76). |
| 4 | pdf | According to the report, why was the post-COVID recovery in port performance partially reversed in 2024? | The Red Sea crisis sent ships around the Cape of Good Hope, and climate-related disruption cut Panama Canal transits. Schedules became unreliable and congestion rose. The disruptions were geopolitical and climatic rather than demand-driven. | PDF p. 14 (Executive Summary) |
| 5 | both | The report says Cape Town improved strongly in 2024. By how much, why, and where does it rank now? | PDF: "nearly 240 points", from new cranes and equipment, upgraded warehousing, hydraulic shore-tension units, a CSIR predictive wind model and a helicopter piloting service. XLSX: -519 to -281 (+238, the largest gain of all 403 ports), yet still ranked 400 of 403. | PDF p. 56 (Box 4.3); Ports, row 'Cape Town'; Group summary, row 'All 403 ports' |
| 6 | both | Box 4.1 says Singapore stayed resilient in the Red Sea crisis. What happened to its CPPI score? | PDF: off-schedule (off-proforma) arrivals rose from about 77% to 85%, the port handled a record 40 million TEU, and it ranked 29th of 403 with a score of 88. XLSX: 115 in 2023 to 88 in 2024 (-27), rank 29, 87% of port time at berth. | PDF p. 53; Ports, row 'Singapore' |
| 7 | both | The report calls Dakar the top Sub-Saharan port in 2024. Do the numbers agree? | Yes. PDF: Dakar rose from -82 to 23 after DP World investment in new cranes, yard expansion and a port community system. XLSX: -82 to 23 (+105), global rank 108, and the best-ranked of the 39 Sub-Saharan ports. | PDF p. 34; Ports, row 'Dakar'; Group summary, row 'Sub-Saharan Africa' |
| 8 | off-topic | What's a good recipe for banana bread? | The guardrail should refuse: the question is not about port performance or shipping. | n/a |
