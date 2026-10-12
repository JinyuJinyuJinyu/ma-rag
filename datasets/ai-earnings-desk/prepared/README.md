# Prepared copies

Cleaned copies of corpus files that can't be indexed as downloaded. Each keeps its original file name, so
indexed chunks cite the original. When a file listed in `../index_selection.csv` has a copy here,
`python main.py ingest` indexes the copy instead.

| File | Made from | Made by | What changed |
|---|---|---|---|
| `MSFT_FY2026-Q4_metrics.xlsx` | `../MSFT/ir/FY2026-Q4/MSFT_FY2026-Q4_metrics.xlsx` | `python -I ../scripts/prepare_msft_metrics.py` | Reshaped for the Excel Narrator: row 1 is the header and column A the metric. Microsoft's period labels are spelled out ("Q4'26" becomes "FY2026 Q4 (Apr-Jun 2026)"), fractions such as 0.38 become "38%", and the footnotes move to a Notes sheet. Values are as published. |

**Licence:** these are derived from Microsoft's investor-relations files (© Microsoft). They are not
included in this repository, so don't republish them.
