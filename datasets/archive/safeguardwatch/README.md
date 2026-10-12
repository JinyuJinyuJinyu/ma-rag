# SafeguardWatch: Australia heavy-emitter compliance assistant (starter kit)

A multi-agent RAG assistant for a climate-transition / ESG analyst. It answers questions about
the ~210 largest industrial emitters covered by Australia's Safeguard Mechanism: who exceeded
their baselines, how many carbon credits they used, and what the 2026-27 Safeguard review
could change.

* **XLSX (the core):** Clean Energy Regulator (CER) facility tables for 2024-25 and 2023-24.
* **PDF (bonus):** DCCEEW *Safeguard Mechanism Review: Consultation Paper* (Aug 2026), plus the
  CER *2024-25 Safeguard Mechanism data insights* report (recommended companion).

Verified on 2026-10-06. Every file was re-downloaded and hash-checked, and every
number in the answer key was re-derived from the files in `raw/`.

---

## 1. Sources

| Role | File | Download URL (works, no redirect) | Landing page |
|---|---|---|---|
| XLSX, 2024-25 | `raw/cer_safeguard_baselines_emissions_2024-25_raw.xlsx` (47,991 B, sha256 `90e01939…f7cd`) | https://cer.gov.au/document/baselines-and-emissions-table-2024-25-0 | https://cer.gov.au/markets/reports-and-data/safeguard-data/2024-25-baselines-and-emissions-data |
| XLSX, 2023-24 | `raw/cer_safeguard_baselines_emissions_2023-24_raw.xlsx` (48,572 B, sha256 `d8364138…c4a7`) | https://cer.gov.au/document/2023-24-baselines-and-emissions-table-0 | cer.gov.au, Reports and data > Safeguard data (2023-24 page not re-checked) |
| PDF, main | DCCEEW Safeguard Mechanism Review consultation paper, Aug 2026 (48 pp, 1.37 MB, text layer) | https://storage.googleapis.com/files-au-climate/climate-au/p/prj3e1e2fec133c66d211d71/page/Safeguard_Mechanism_Review_consultation_paper_August_2026.pdf | https://consult.dcceew.gov.au/2026-27-safeguard-mechanism-review (a JavaScript app; use the direct PDF link) |
| PDF, companion | CER 2024-25 Safeguard Mechanism data insights (15 pp, 0.42 MB, text layer, "correct as at 1 April 2026") | https://cer.gov.au/document/2024-25-safeguard-mechanism-data-insights | cer.gov.au, Safeguard data |

Both XLSX files are "Data as at 13/08/2026". The CER also publishes the same tables as CSV.

### Licence and attribution

* **CER tables and report:** CC BY 4.0. The CER copyright page (cer.gov.au/about-us/our-policies/copyright,
  updated 28 Jan 2026) says: *"You can use most of the content on our website under the
  Creative Commons Attribution 4.0 international license unless it's marked otherwise."*
  The Coat of Arms, logos and third-party content are excluded.
  Attribution for the **cleaned** workbook (it is derived material):
  > Based on Clean Energy Regulator material licensed under a Creative Commons Attribution 4.0 licence.
* **DCCEEW consultation paper:** PDF p.2 states: *"All material in this publication is licensed
  under a Creative Commons Attribution 4.0 International Licence except content supplied by
  third parties, logos and the Commonwealth Coat of Arms."* Attribution: © Commonwealth of
  Australia 2026 (DCCEEW), CC BY 4.0.
* Commercial use is allowed (no non-commercial restriction). Do not imply government endorsement.

---

## 2. Rebuild the cleaned workbook

```bash
cd datasets/archive/safeguardwatch
python -I scripts/clean.py \
    raw/cer_safeguard_baselines_emissions_2024-25_raw.xlsx \
    safeguardwatch_clean.xlsx \
    raw/cer_safeguard_baselines_emissions_2023-24_raw.xlsx
```

The third argument is optional; with only two arguments you get the 2024-25 sheet,
Rankings and Guide. Output: `safeguardwatch_clean.xlsx`. The cell contents are identical on
every rebuild (checked), but the file hash changes because the save time is stored inside the xlsx.

### Cleaning steps (what `clean.py` does and why)

1. **Drop the 3 title rows** (title, a multi-line description, "Data as at 13/08/2026").
   Raw row 4 (`Facility name | Responsible emitter | …`) becomes row 1. Without this the
   narrator names the columns "Column2 … Column23".
2. **Rename headers so they carry units and explain abbreviations.** The deleted description
   held the only unit note ("All emissions values are in t CO2-e"). Examples:
   `Baseline emissions number` → `Baseline (tCO2e)`; `ERC` → `Emission reduction contribution (ERC) factor`;
   `Net position number` → `Net position vs baseline (tCO2e, negative = below baseline)`;
   `GHG Methane` → `Methane (tCO2e)`; `Cumulative MYMP …` → `Multi-year period cumulative … (tCO2e)`.
3. **Remove placeholders.** `-` becomes an empty cell (MYMP columns and Notes). This removes the
   "Notes is -" sentences found in 343 rows of an earlier draft. `0` becomes empty in four
   columns that are almost always 0 (borrowing adjustment, ACCUs issued, ACCUs deemed surrendered,
   units relinquished). Meaningful zeros are kept: ACCUs/SMCs surrendered, SMCs issued and net position.
   The one `N/A` baseline (Beaudesert Plant) becomes `N/A (no baseline set)`.
4. **Fix float noise.** Ten 2023-24 tonnage cells are stored as binary floats, for example North West
   Shelf N2O = `4729.999999999997`, and the narrator would print them in full. They are rounded to
   whole tonnes. The ERC factor keeps its decimals.
5. **Expand codes.** State codes become names (`WA` → `Western Australia (WA)`; `National` →
   `National (operates across several states)`). The CER's long Notes sentences become short
   plain-English versions, for example "eligible facility under 58B" → "Eligible facility under
   section 58B of the Safeguard Rule: covered emissions below the 100,000 tCO2e threshold, it may
   apply for SMCs, and it is not counted in scheme totals". The trailing full stop is removed so
   the narrator does not print "..".
6. **Sort** facilities by covered emissions, largest first.
7. **Add two helper sheets.**
   * `Rankings`: top 5 facilities per measure and year (highest covered emissions, most ACCUs
     surrendered, most SMCs surrendered, most SMCs issued, highest methane, furthest above
     baseline). One-row-per-facility sentences cannot answer "which facility had the highest…"
     because retrieval returns only top_k=5 chunks. In a BM25 check, Gorgon's own row ranked
     below 50 for that question, while the Rankings row ranked 2nd.
   * `Guide`: what every column means and its sign convention (from the CER publication notes).

Checks confirmed that every numeric column sums to the same total before and after
cleaning, that all facility names are kept, and that no float tonnage is left.

### Cleaned sheets

| Sheet | Rows | Columns | Chunks (reference narrator + splitter) |
|---|---|---|---|
| Safeguard 2024-25 | 228 | 23 | 229 |
| Safeguard 2023-24 | 233 | 23 | 234 |
| Rankings | 12 | 6 | 7 |
| Guide | 26 | 2 | 6 |
| **Total** | **499** | | **476** (under 600 chunks) |

Rows average 690 narrated characters, so the 1200-char splitter puts exactly **one facility per
chunk**, which is good for retrieval precision. A quick stride-based estimate (~363) is too
low because it assumes chunks are packed to the full 960-char stride.

### Narration samples (reference narrator, LLM summary off)

```
Safeguard 2024-25 — Facility 'Gorgon Operations': Responsible emitter (company) is CHEVRON AUSTRALIA PTY LTD; State or territory is Western Australia (WA); Industry (ANZSIC) is Oil and gas extraction (070); Emission reduction contribution (ERC) factor is 0.902; Baseline (tCO2e) is 8543442; Covered emissions (tCO2e) is 9021856; ACCUs surrendered (carbon credits) is 478414; SMCs surrendered (Safeguard credits) is 0; Net emissions (tCO2e) is 8543442; Net position vs baseline (tCO2e, negative = below baseline) is 0; SMCs issued (Safeguard credits) is 0; Carbon dioxide (tCO2e) is 8760179; Methane (tCO2e) is 252429; Nitrous oxide (tCO2e) is 8460; Other greenhouse gases (tCO2e) is 788.

Safeguard 2024-25 — Facility 'Kestrel Mine': Responsible emitter (company) is Kestrel Coal Group Pty Ltd; State or territory is Queensland (QLD); Industry (ANZSIC) is Coal mining (060); Emission reduction contribution (ERC) factor is 0.902; Baseline (tCO2e) is 708388; Covered emissions (tCO2e) is 1263099; ACCUs surrendered (carbon credits) is 504711; SMCs surrendered (Safeguard credits) is 50000; Net emissions (tCO2e) is 708388; Net position vs baseline (tCO2e, negative = below baseline) is 0; SMCs issued (Safeguard credits) is 0; Carbon dioxide (tCO2e) is 412496; Methane (tCO2e) is 848423; Nitrous oxide (tCO2e) is 2161; Other greenhouse gases (tCO2e) is 19; Notes is Surrendered ACCUs equal to 30% or more of its baseline, so it had to publish a statement explaining why more on-site abatement did not occur.

Rankings — Ranking 'Highest covered emissions (tCO2e), 2024-25': 1st is Gorgon Operations (CHEVRON AUSTRALIA PTY LTD) with 9,021,856; 2nd is Start up and Operations of the Ichthys LNG Project (INPEX Operations Australia Pty Ltd) with 6,255,335; 3rd is Port Kembla Steelworks (BLUESCOPE STEEL (AIS) PTY. LTD.) with 6,061,526; 4th is North West Shelf Project (Woodside Energy Ltd.) with 5,750,304; 5th is Qantas Airways Limited - National Facility (Qantas Airways Limited) with 4,622,401.
```

---

## 3. Data-quality notes (read before demoing)

* **Eligible (s58B) facilities are in the table but not in the scheme totals.** These are 19 rows
  in 2024-25 and 12 in 2023-24. To reproduce the official 132.8 Mt and 208 facilities, exclude them:
  the 2024-25 rows sum to 133.84 Mt, or 132.80 Mt without the s58B rows. Coal mining is 31.69 Mt
  (19.50 Mt methane) without them, matching the paper's "31.7 Mt … 19.5 Mt methane"; with them it
  is 31.78 Mt.
* **Deemed surrenders.** The "10.8 million ACCUs" and "139 facilities" in the PDFs include
  `ACCUs deemed surrendered`: 10.55m surrendered plus 0.28m deemed is 10.83m, and 138 rows rise to 139.
* **The two PDFs disagree for 2023-24.** The DCCEEW paper (PDF p.17) says 7.1m ACCUs were
  surrendered in 2023-24. The CER insights graph (p.11) says 7.6m, and the table sums to 7.57m
  (plus 0.04m deemed). The table includes later surrenders such as Carborough Downs' 546,907
  ACCUs, which followed an excess emissions situation. Avoid that number in demos.
* **Duplicate facility names.** When operational control changed hands, a facility appears twice in
  a year, once per responsible emitter: Telfer Gold Mine in 2024-25, and Blackwater Mine and Daunia
  Mine in 2023-24. These are not data errors.
* **The 30% flag is the regulator's, not a formula.** Paraburdoo Mine (2024-25) surrendered
  ACCUs equal to 63.9% of its baseline but is not flagged. Three 2023-24 rows are also unflagged
  (Carborough Downs, Ironbark No. 1 and NKW01, which have multi-year periods or late surrenders).
  Use the Notes column, not a computed ratio.
* **Accounting identities hold on every row.** Net emissions = covered + issued - deemed -
  surrendered ACCUs - surrendered SMCs, net position = net - baseline, and the four gas columns
  sum to covered emissions (±2 t).
* Aggregate questions (sums, counts, "how many WA facilities") are beyond top-k retrieval. Use the
  Rankings sheet, the PDFs' scheme totals, or add a pandas tool.
* The 2024-25 and 2023-24 Qantas facility names differ slightly ("… - National Facility" vs
  "… National Transport Facility").

## 4. PDF notes

* Consultation paper: 48 pp, 1.37 MB, Word-generated text layer (~1,850 chars/page).
  Insights report: 15 pp, 0.42 MB. Both are under 4 MB.
* **Azure Document Intelligence F0** analyses only the first 2 pages per request. Split the files
  into 2-page pieces (24 + 8 = 32 files) or use the paid S0 tier (63 pages in total).
* Neither PDF names Gorgon, Chevron, FLNG, Shell, Kestrel, Port Kembla or any Rio Tinto/Hamersley
  entity. Only "Moomba" appears, once. Facility questions therefore show the A2A XLSX ingestion
  step clearly: the system cannot answer before ingestion and can after.

---

## 5. Verified demo answer key

Page numbers are PDF page indexes (as shown in a PDF viewer), not printed page numbers.

| # | Needs | Question | Verified answer | Evidence |
|---|---|---|---|---|
| 1 | xlsx (superlative, **best before/after demo**) | Which Safeguard facility had the highest covered emissions in 2024-25? | **Gorgon Operations** (Chevron Australia Pty Ltd, WA): 9,021,856 tCO2e. Unique: 2nd is Ichthys LNG (INPEX) at 6,255,335. | `Rankings` row "Highest covered emissions (tCO2e), 2024-25"; `Safeguard 2024-25` row "Gorgon Operations". Not in either PDF. |
| 2 | xlsx | How did Gorgon Operations deal with being over its baseline in 2024-25? | Covered 9,021,856 vs baseline 8,543,442 (478,414 over). Chevron surrendered 478,414 ACCUs and no SMCs, so net emissions = 8,543,442 and net position = 0. No SMCs issued. | `Safeguard 2024-25` row "Gorgon Operations" |
| 3 | xlsx | Which facility was issued the most Safeguard Mechanism Credits (SMCs) for 2024-25? | **FLNG** (Shell Australia Pty Ltd): 1,421,640 SMCs. Covered 2,253,798 vs baseline 3,675,438 (net position -1,421,640). Next: Capcoal Mine 1,144,413, Moomba Plant 704,395. | `Rankings` row "Most SMCs issued (Safeguard credits), 2024-25"; `Safeguard 2024-25` row "FLNG" |
| 4 | xlsx | Was Port Kembla Steelworks above or below its baseline in 2024-25? | Below. BlueScope Steel (AIS): covered 6,061,526 vs baseline 6,350,820, net position -289,294, so 289,294 SMCs issued. | `Safeguard 2024-25` row "Port Kembla Steelworks" |
| 5 | pdf | What would an ACCU bought from the cost containment measure cost in 2025-26, and how does that compare with the market price? | $82.68 in 2025-26, against an average ACCU spot price of $36.99, so there have been no purchases to date. The price was set at $75 per ACCU in 2023-24 and rises by CPI plus 2% a year. | Consultation paper PDF p.24 ($82.68 / $36.99); p.23 ($75, CPI + 2%) |
| 6 | both | The CER credits carbon capture and storage at Moomba with cutting oil-and-gas emissions intensity. What do the facility tables show for Moomba Plant? | 2023-24: covered 2,196,816 vs baseline 2,048,421, and Santos surrendered 150,974 ACCUs (net position 0). 2024-25: covered 1,078,118 vs baseline 1,782,513, 704,395 below, so 704,395 SMCs were issued. Covered emissions fell 1,118,698 t (-50.9%). | Insights PDF p.10 (CCS at Moomba); `Safeguard 2023-24` and `Safeguard 2024-25` rows "Moomba Plant" |
| 7 | both | The review paper says 23 facilities surrendered ACCUs worth more than 30% of their baseline in 2024-25. Was Kestrel Mine one of them, and how did it cover its excess? | Yes. Kestrel Mine (Kestrel Coal Group): covered 1,263,099 vs baseline 708,388. It surrendered 504,711 ACCUs (71% of baseline) plus 50,000 SMCs, so net position is 0, and its Notes carry the 30% statement flag. The rule: a facility surrendering ACCUs above 30% of baseline must explain why it is not cutting emissions on site. The table has exactly 23 flagged rows in 2024-25 (18 in 2023-24). | Consultation paper PDF p.17 (23 and 18), p.16 (rule); insights PDF p.12; `Safeguard 2024-25` row "Kestrel Mine" |
| 8 | off-topic | What's the weather forecast for Perth this weekend? | The guardrail should decline: it is unrelated to the Safeguard/ESG corpus. | n/a |

BM25-only retrieval check (reference chunking, no embeddings): Q1 → Rankings chunk at rank 2;
Q2 → Gorgon row rank 2; Q3 → Rankings chunk rank 1; Q5 → PDF p.24 chunk rank 1; Q6 → both
Moomba rows in the top 3; Q7 → Kestrel row rank 1. Hybrid vector search should only improve on this.

**Avoid** asking "Which Rio Tinto mines are on the 30% list?" The table
never says "Rio Tinto" (the emitters are Hamersley Iron / Hamersley HMS), and the answer needs
5 of 23 rows. In the BM25 check Hope Downs 1 ranked 23rd, so top_k=5 cannot return the full list.
