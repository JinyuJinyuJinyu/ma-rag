"""A local copy of every chunk in the search index, and what the golden set can do with it.

export_snapshot() reads the whole index once (read-only: one facet query, then one query per source
file) and writes logs/eval/index_snapshot.jsonl, one chunk per line, plus index_snapshot.json with the
counts and a checksum. logs/ stays out of git: the chunks quote the companies' IR documents.

With a snapshot, offline:
- check_gold() checks the golden set against what the index really holds: every evidence unit needs a
  chunk that supports it, and no chunk may match a decline item's `absent` patterns.
- evidence_chunks() picks the few chunks that hold an item's evidence, for the answer eval's `gold`
  condition (the answer agent with perfect retrieval).
- fewest_chunks() counts the chunks a vertical question's rows need, which its `chunks` label must match
  (row chunks only: a workbook's column views don't change the label).
"""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from evaluation.evidence import alternatives, normalize, row_chunks, supports, unit_files
from evaluation.gold import is_vertical, turns

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FILE = ROOT / "logs" / "eval" / "index_snapshot.jsonl"
SELECT = ["id", "content", "source_file", "chunk_index", "file_type", "metadata"]
PAGE_SIZE = 1000   # Azure AI Search returns at most 1,000 documents per request
MAX_FILES = 1000   # facet buckets to ask for: one per source file
LOOSE_SUPPORT = 10  # a unit that this many chunks support is probably matching more than its fact


class SnapshotError(ValueError):
    """The file isn't an index snapshot (one chunk per line)."""


def meta_path(path) -> Path:
    """The summary next to a snapshot: snap.jsonl -> snap.json, any other name -> <name>.meta.json, so the
    summary can never overwrite the snapshot itself (snapshot --out snap.json)."""
    path = Path(path)
    return path.with_suffix(".json") if path.suffix == ".jsonl" else path.with_name(path.name + ".meta.json")


def _odata_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _chunk(doc: dict) -> dict:
    """An index document as a snapshot chunk; file_type from the field, else the metadata JSON, else the suffix."""
    file_type = doc.get("file_type")
    if not file_type:
        try:
            file_type = json.loads(doc.get("metadata") or "{}").get("file_type")
        except (TypeError, ValueError):
            file_type = None
    source_file = doc.get("source_file") or ""
    if not file_type and "." in source_file:
        file_type = source_file.rsplit(".", 1)[1].lower()
    return {"chunk_id": doc.get("id"), "source_file": source_file, "file_type": file_type,
            "chunk_index": doc.get("chunk_index"), "content": doc.get("content") or ""}


def snapshot_sha256(chunks: list[dict]) -> str:
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda c: c["chunk_id"] or ""):
        digest.update(json.dumps([chunk["chunk_id"], chunk["source_file"], chunk["content"]]).encode("utf-8"))
    return digest.hexdigest()


def fetch_chunks(search_client, page_size: int = PAGE_SIZE) -> tuple[list[dict], int]:
    """Every chunk in the index, file by file in chunk order, and the count the index reports.

    One facet query lists the source files; each file is then read in chunk_index order, which is
    unique within a file, so paging (if a file ever passes page_size chunks) can't skip or repeat.
    """
    head = search_client.search(search_text="*", facets=[f"source_file,count:{MAX_FILES}"], top=0,
                                include_total_count=True)
    files = sorted(bucket["value"] for bucket in (head.get_facets() or {}).get("source_file") or [])
    total = head.get_count()
    chunks = []
    for source_file in files:
        skip = 0
        while True:
            page = list(search_client.search(search_text="*", filter=f"source_file eq {_odata_string(source_file)}",
                                             order_by=["chunk_index asc"], select=SELECT, top=page_size,
                                             skip=skip))
            chunks += [_chunk(doc) for doc in page]
            if len(page) < page_size:
                break
            skip += page_size
    return chunks, total


def export_snapshot(retriever, path=SNAPSHOT_FILE) -> dict:
    """Write the index's chunks to `path` (jsonl) and its summary next to it; returns the summary."""
    chunks, total = fetch_chunks(retriever.search_client)
    if total is not None and total != len(chunks):
        raise RuntimeError(f"the index reports {total} documents but {len(chunks)} were read; "
                           "is something writing to it? Try again when it's idle.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    meta = {
        "index_name": retriever.index_name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunks": len(chunks),
        "sha256": snapshot_sha256(chunks),
        "files": dict(sorted(Counter(c["source_file"] for c in chunks).items())),
        "file_types": dict(sorted(Counter(c["file_type"] or "unknown" for c in chunks).items())),
    }
    meta_path(path).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def load_snapshot(path=SNAPSHOT_FILE) -> list[dict]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no index snapshot at {path}: run `python scripts/eval_retrieval.py snapshot` "
                                "first (it reads the index once and changes nothing)")
    chunks = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError as e:
            raise SnapshotError(f"{path} is not an index snapshot (line {n} isn't a JSON chunk): pass the .jsonl "
                                "file that `python scripts/eval_retrieval.py snapshot` writes") from e
        if not isinstance(chunk, dict) or not {"chunk_id", "source_file", "content"} <= set(chunk):
            raise SnapshotError(f"{path} is not an index snapshot (line {n} has no chunk_id, source_file and "
                                "content): pass the .jsonl file that `python scripts/eval_retrieval.py snapshot` writes")
        chunks.append(chunk)
    return chunks


def load_snapshot_meta(path=SNAPSHOT_FILE) -> dict:
    meta = meta_path(path)
    return json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}


def _supported_units(units: list[dict], chunks: list[dict]) -> list[set]:
    """For each chunk, the indexes of the units it supports."""
    out = []
    for chunk in chunks:
        text = normalize(chunk.get("content") or "")
        out.append({i for i, unit in enumerate(units) if supports(unit, chunk.get("source_file"), text)})
    return out


def evidence_chunks(units: list[dict], chunks: list[dict], k: int) -> list[dict]:
    """A small set of snapshot chunks that together hold the units' facts: at most k, in pick order.

    Greedy set cover: each pick is the chunk that supports the most units not yet covered. Ties go to
    the chunk from the file listed first for the units it would add, then the lower chunk_index. Units
    no chunk supports stay uncovered (check_gold() reports them).
    """
    supported = _supported_units(units, chunks)

    def tie_break(index, covered):
        # Only the units this pick would add count: a file listed first for an already-covered unit
        # says nothing about this pick
        chunk = chunks[index]
        file_rank = min((unit_files(units[u]).index(chunk["source_file"])
                         for u in supported[index] - covered if chunk["source_file"] in unit_files(units[u])),
                        default=len(chunks))
        return file_rank, chunk.get("chunk_index") or 0, chunk.get("chunk_id") or ""

    picks, covered = [], set()
    while len(picks) < k:
        gains = {i: len(units_here - covered) for i, units_here in enumerate(supported) if units_here - covered}
        if not gains:
            break
        best = max(gains.values())
        choice = min((i for i, gain in gains.items() if gain == best), key=lambda i: tie_break(i, covered))
        picks.append(chunks[choice])
        covered |= supported[choice]
    return picks


def fewest_chunks(units: list[dict], chunks: list[dict]) -> Optional[int]:
    """The fewest snapshot chunks that together hold every unit (an exact set cover), or None when some
    unit has no chunk. A vertical question's `chunks` label is this number for its evidence."""
    universe = frozenset(range(len(units)))
    sets = {frozenset(s) for s in _supported_units(units, chunks) if s}
    sets = [s for s in sets if not any(s < other for other in sets)]  # a chunk another one covers adds nothing
    if not units or frozenset().union(*sets) != universe:
        return None if units else 0
    best = [len(universe)]

    def search(covered: frozenset, used: int) -> None:
        if covered == universe:
            best[0] = min(best[0], used)
            return
        remaining = universe - covered
        widest = max(len(s & remaining) for s in sets)
        if used + -(-len(remaining) // widest) >= best[0]:
            return
        # Branch on the uncovered unit with the fewest chunks: one of them must be picked
        unit = min(remaining, key=lambda u: sum(1 for s in sets if u in s))
        for s in sorted((s for s in sets if unit in s), key=lambda s: -len(s & remaining)):
            search(covered | s, used + 1)

    search(frozenset(), 0)
    return best[0]


def check_gold(items: list[dict], chunks: list[dict]) -> dict:
    """The golden set against the snapshot: support per evidence unit, and any `absent` pattern found.

    Returns {"units": [...], "absent": [...], "missing_files": [...], "vertical": [...]} where each unit
    row has the item id, turn, unit index, fact, support (chunks in the unit's files that hold the fact),
    elsewhere (chunks in other files matching its patterns: a loose pattern shows up here) and status:
    ok, broken (nothing supports it: fix the patterns or the files) or loose. Each vertical row has the
    item id, its `chunks` label and the fewest chunks that hold its evidence (fewest_chunks()): they must
    agree, or re-chunking has changed what the question costs.
    """
    normalized = [(chunk, normalize(chunk.get("content") or "")) for chunk in chunks]
    in_snapshot = {chunk["source_file"] for chunk in chunks}
    rows, absent, named, vertical = [], [], set(), []
    for item in items:
        for turn_no, turn in enumerate(turns(item), 1):
            for n, unit in enumerate(turn.get("evidence") or []):
                files = unit_files(unit)
                named |= set(files)
                support = [c["chunk_id"] for c, text in normalized if supports(unit, c["source_file"], text)]
                anywhere = {"any_of": [{"all": form["all"]} for form in alternatives(unit)]}
                elsewhere = [c["chunk_id"] for c, text in normalized
                             if c["source_file"] not in files and supports(anywhere, c["source_file"], text)]
                status = "broken" if not support else ("loose" if len(support) >= LOOSE_SUPPORT else "ok")
                rows.append({"id": item["id"], "turn": turn_no, "unit": n, "fact": unit.get("fact", ""),
                             "files": files, "support": len(support), "chunks": support[:5],
                             "elsewhere": len(elsewhere), "status": status})
        if is_vertical(item):
            label, fewest = item["vertical"]["chunks"], fewest_chunks(item.get("evidence") or [], row_chunks(chunks))
            vertical.append({"id": item["id"], "label": label, "fewest": fewest, "ok": fewest == label})
        for pattern in item.get("absent") or []:
            compiled = re.compile(pattern, re.IGNORECASE)
            found = [c["chunk_id"] for c, text in normalized if compiled.search(text)]
            if found:
                absent.append({"id": item["id"], "pattern": pattern, "chunks": found[:5], "count": len(found)})
    return {"units": rows, "absent": absent, "missing_files": sorted(named - in_snapshot), "vertical": vertical}
