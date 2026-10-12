"""
Index Selection Module.

Reads a dataset's index_selection.csv (see datasets/ai-earnings-desk/INDEXING_PLAN.md):
which files to index, in which tier, and the attributes stored with their
chunks (company, ticker, doc_kind, file_type, fiscal_period, calendar_quarter).

Paths in the CSV are relative to the CSV's folder. When a file needs cleaning
before it can be narrated (its note starts with "clean first"), the cleaned copy
lives in the dataset's prepared/ folder under the same file name, and that copy
is ingested instead, so its chunks keep the original file name.
"""

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from src.data_pipeline.indexer import METADATA_FIELDS

logger = logging.getLogger(__name__)

DEFAULT_SELECTION_CSV = (
    Path(__file__).resolve().parents[2] / "datasets" / "ai-earnings-desk" / "index_selection.csv"
)


@dataclass
class SelectedFile:
    """One row of index_selection.csv."""

    tier: int
    listed_path: str              # path as written in the CSV
    path: Path                    # file to ingest (the prepared copy when one exists)
    route: str
    est_chunks: int
    pdf_pages: int
    note: str
    prepared_path: Path           # where a cleaned copy goes: <dataset>/prepared/<file name>
    metadata: Dict[str, str] = field(default_factory=dict)
    prepared: bool = False        # True when path is the prepared copy
    needs_preparing: bool = False  # "clean first", but no prepared copy yet

    @property
    def file_type(self) -> str:
        return self.metadata.get("file_type") or self.path.suffix.lower().lstrip(".")


def load_selection(csv_path: Optional[str] = None) -> List[SelectedFile]:
    """
    Load index_selection.csv.

    Args:
        csv_path: Path to the CSV. If None, uses the ai-earnings-desk selection.

    Returns:
        List of SelectedFile, in CSV order
    """
    csv_file = Path(csv_path) if csv_path else DEFAULT_SELECTION_CSV
    base = csv_file.resolve().parent

    with open(csv_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    selection = []
    for row in rows:
        original = base / row["path"]
        prepared_copy = base / "prepared" / original.name
        note = row.get("note") or ""
        selection.append(SelectedFile(
            tier=int(row["tier"]),
            listed_path=row["path"],
            path=prepared_copy if prepared_copy.exists() else original,
            route=row.get("route") or "",
            est_chunks=int(row.get("est_chunks") or 0),
            pdf_pages=int(row.get("pdf_pages") or 0),
            note=note,
            prepared_path=prepared_copy,
            metadata={name: row[name] for name in METADATA_FIELDS if row.get(name)},
            prepared=prepared_copy.exists(),
            needs_preparing=note.startswith("clean first") and not prepared_copy.exists(),
        ))

    logger.info(f"Loaded {len(selection)} files from {csv_file}")
    return selection


def find_metadata(file_path: str, csv_path: Optional[str] = None) -> Dict[str, str]:
    """
    Look up the attributes listed for a file in index_selection.csv.

    Matches the listed file or its prepared copy.

    Args:
        file_path: Path to the file being processed
        csv_path: Path to the CSV. If None, uses the ai-earnings-desk selection.

    Returns:
        The file's attributes, or an empty dict if the file isn't listed
        (or the CSV doesn't exist)
    """
    csv_file = Path(csv_path) if csv_path else DEFAULT_SELECTION_CSV
    if not csv_file.exists():
        return {}

    target = Path(file_path).resolve()
    base = csv_file.resolve().parent
    for item in load_selection(str(csv_file)):
        if target in ((base / item.listed_path).resolve(), item.prepared_path.resolve()):
            return dict(item.metadata)
    return {}
