"""Passage-level retrieval gold: does a chunk hold the evidence an answer needs?

Each golden-set item lists evidence units: one per fact the answer needs. A unit names the indexed
files that state the fact and regexes that must all appear in one chunk:

    {files: [NVDA_FY2027-Q2_cfo_commentary.pdf, NVDA_FY2027-Q2_transcript.pdf],
     all: ['data center', '89[.,]0 billion|\\$?89,0\\d\\d']}

A chunk supports the unit when it comes from one of the files and matches every regex. Matching is
case-insensitive on normalized text (see normalize()), so a pattern written with single spaces also
matches the line breaks and spacing that Document Intelligence puts in tables ("18 %").

When files word the same fact differently (a workbook row and a PDF table), the unit lists the forms
as alternatives, and any one of them supports it:

    {fact: "NVIDIA's fiscal Q2 2027 ended July 26, 2026",
     any_of: [{files: [ai_earnings_comparison.xlsx], all: ['FY2027 Q2', '2026-07-26']},
              {files: [NVDA_FY2027-Q2_cfo_commentary.pdf], all: ['Q2 FY ?27', 'July 26, 2026']}]}

Units are matched by text, not chunk id, so the gold survives re-chunking and re-ingestion.
"""

import itertools
import math
import re
import unicodedata
from functools import lru_cache

SPREADSHEET_SUFFIXES = (".xlsx", ".csv")  # what the A2A Excel agent ingests
# Minus, hyphen, non-breaking hyphen, figure dash, en and em dash all become "-"; curly quotes straight;
# soft hyphens go
_TRANSLATE = str.maketrans({"−": "-", "‐": "-", "‑": "-", "‒": "-", "–": "-",
                            "—": "-", "―": "-", "‘": "'", "’": "'", "“": '"',
                            "”": '"', "­": None})


def normalize(text: str) -> str:
    """NFKC, dashes and minus signs as '-', straight quotes, and every run of whitespace as one space."""
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    return " ".join(text.split())


# The Excel narrator's column views ("Revenue — column 'CY2025 Q1', by Company: NVIDIA is 44062; ..."), which
# follow a workbook's row passages. A vertical question's `chunks` label counts row chunks only: the layout the
# narrator always writes, whatever column views the index also holds.
_COLUMN_VIEW = re.compile(r" - column '[^']*', by ")


def is_column_view(text: str) -> bool:
    return bool(_COLUMN_VIEW.search(normalize(text)))


def row_chunks(chunks: list[dict]) -> list[dict]:
    """The chunks that aren't column views."""
    return [c for c in chunks if not is_column_view(c.get("content") or "")]


def is_spreadsheet(source_file: str) -> bool:
    return (source_file or "").lower().endswith(SPREADSHEET_SUFFIXES)


@lru_cache(maxsize=4096)
def _compiled(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


def alternatives(unit: dict) -> list[dict]:
    """The unit's forms: its any_of list, or the unit itself."""
    return unit.get("any_of") or [unit]


def unit_files(unit: dict) -> list[str]:
    """Every file named by any form of the unit, in order."""
    files = []
    for alt in alternatives(unit):
        files += [f for f in alt.get("files") or [] if f not in files]
    return files


def supports(unit: dict, source_file: str, normalized_text: str) -> bool:
    """True if a chunk (its source file and normalize()d text) holds the unit's fact."""
    for alt in alternatives(unit):
        files = alt.get("files")
        if files and source_file not in files:
            continue
        if all(_compiled(p).search(normalized_text) for p in alt["all"]):
            return True
    return False


def spreadsheet_only(unit: dict) -> bool:
    """A unit only a workbook states: the XLSX ablation makes it impossible to retrieve."""
    return all(alt.get("files") and all(is_spreadsheet(f) for f in alt["files"]) for alt in alternatives(unit))


def documents_only(unit: dict) -> bool:
    """A unit no workbook states."""
    return all(alt.get("files") and not any(is_spreadsheet(f) for f in alt["files"]) for alt in alternatives(unit))


def names_spreadsheet(unit: dict) -> bool:
    """A unit some workbook states (a workbook row, maybe also a document)."""
    return not documents_only(unit)


def needs(units: list[dict]) -> str:
    """Where an item's evidence lives: xlsx (some fact only in a workbook), docs (every fact has a
    document source; workbooks may also hold some) or both (facts only in workbooks AND facts only in
    documents: a cross-source question). Items without evidence get "none"."""
    if not units:
        return "none"
    xlsx_only = any(spreadsheet_only(u) for u in units)
    docs_only = any(documents_only(u) for u in units)
    if xlsx_only and docs_only:
        return "both"
    return "xlsx" if xlsx_only else "docs"


def files_needed(units: list[dict]) -> int:
    """The fewest files whose chunks can hold every unit: 1 for a question one file answers, 2 or more for
    a multi-file question, 0 without units. A unit that several files state needs only one of them."""
    options = [set(unit_files(unit)) for unit in units]
    files = sorted(set().union(*options)) if options else []
    for size in range(1, len(files) + 1):
        if any(all(option & set(combo) for option in options) for combo in itertools.combinations(files, size)):
            return size
    return 0


def files_label(count: int) -> str:
    """How the reports group questions by files_needed()."""
    return "3+ files" if count >= 3 else ("1 file" if count == 1 else f"{count} files")


def chunks_label(count: int, k: int) -> str:
    """How the reports group vertical questions by the chunks their rows need, against the k chunks a search
    returns: one chunk, at most k (a top k can hold every row), or more than k (never complete at k)."""
    if count <= 1:
        return "1 chunk"
    if count <= k:
        return f"2–{k} chunks" if k > 2 else "2 chunks"
    return f"more than {k} chunk" + ("s" if k != 1 else "")


def chunks_order(k: int) -> list[str]:
    """chunks_label()'s groups, fewest chunks first."""
    return list(dict.fromkeys(chunks_label(count, k) for count in (1, 2, k + 1)))


def required_files(units: list[dict]) -> list[str]:
    """The files that are the only source of some unit, in unit order: a search that finds nothing from
    one of them can't hold every fact the answer needs."""
    required = []
    for unit in units:
        files = unit_files(unit)
        if len(files) == 1 and files[0] not in required:
            required.append(files[0])
    return required


def coverage(units: list[dict], chunks: list[dict]) -> list[list[int]]:
    """For each chunk, in rank order, the indexes of the units it supports."""
    out = []
    for chunk in chunks:
        text = normalize(chunk.get("content") or "")
        out.append([i for i, unit in enumerate(units) if supports(unit, chunk.get("source_file"), text)])
    return out


def evidence_scores(units: list[dict], chunks: list[dict], k: int, exclude_spreadsheets: bool = False) -> dict:
    """Passage-level retrieval scores of one ranked chunk list against an item's evidence units.

    - hit: some chunk in the top k supports some unit
    - recall: the share of units supported by the top k
    - complete: every unit is supported (the context holds everything the answer needs)
    - mrr: 1 / rank of the first supporting chunk (0 when none)
    - precision: the share of the top k that supports some unit
    - ndcg: rank-discounted gain, where a chunk gains 1 for each unit no earlier chunk supported; the
      ideal puts one supporting chunk per unit at the top, and the score is capped at 1 (one chunk
      holding two facts at rank 1 is as good as it gets)
    - first_rank, and missed: the indexes (into the item's own list) of the units nothing supported

    With exclude_spreadsheets (the XLSX ablation) units only a workbook states are left out, and an
    item left with no units scores None: it can't be retrieved by design.
    """
    kept = [i for i, u in enumerate(units) if not (exclude_spreadsheets and spreadsheet_only(u))]
    units = [units[i] for i in kept]
    if not units:
        return None
    top = list(chunks)[:k]
    supported = coverage(units, top)
    covered, relevant_ranks, dcg = set(), [], 0.0
    for rank, hits in enumerate(supported, 1):
        if hits:
            relevant_ranks.append(rank)
        new = set(hits) - covered
        if new:
            dcg += len(new) / math.log2(rank + 1)
            covered |= new
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(units), k) + 1))
    first = relevant_ranks[0] if relevant_ranks else None
    return {
        "hit": 1.0 if first else 0.0,
        "recall": len(covered) / len(units),
        "complete": 1.0 if len(covered) == len(units) else 0.0,
        "mrr": 1.0 / first if first else 0.0,
        "precision": len(relevant_ranks) / k,
        "ndcg": min(1.0, dcg / ideal) if ideal else 0.0,
        "first_rank": first,
        "units": len(units),
        "missed": [kept[i] for i in range(len(units)) if i not in covered],
    }
