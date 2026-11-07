"""Add the Excel narrator's column views to workbooks already in the index, without re-ingesting them.

Ingestion appends column views after a workbook's sheets (services/excel_agent/narrator.py): each column
down every row, so a question about one column finds its rows in a few passages instead of one chunk per
row. A workbook indexed before that has row passages only. This script narrates just its column views,
chunks them as ingestion does, numbers the chunks after the workbook's row chunks, embeds them and uploads
them. The row chunks stay as they are; re-ingesting would also re-run the LLM sheet summaries in them.

    python scripts/add_column_views.py --dry-run [--out views.jsonl] FILE...
        offline: chunk the column views and number them after the snapshot's row chunks; --out writes them
        in the snapshot's format (gold tools can check evidence against them before they are indexed)
    python scripts/add_column_views.py FILE...           embed and upload (Azure OpenAI, Azure AI Search)
    python scripts/add_column_views.py --delete FILE...  delete the files' column-view chunks from the index
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.evidence import is_column_view  # noqa: E402
from evaluation.snapshot import load_snapshot  # noqa: E402
from services.excel_agent.narrator import ExcelNarrator  # noqa: E402

# Chunk metadata that describes one chunk's place in its file, not the file
CHUNK_KEYS = ("chunk_index", "total_chunks", "chunk_size", "is_first_chunk", "is_last_chunk")


def _odata(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def indexed_chunks(search_client, source_file: str) -> list[dict]:
    """The file's chunks in the index (read-only): id, chunk_index, content and metadata."""
    return list(search_client.search(search_text="*", filter=f"source_file eq {_odata(source_file)}",
                                     select=["id", "chunk_index", "content", "metadata"], top=1000))


def column_chunks(path: Path, row_indexes: list[int], metadata: dict) -> list[dict]:
    """The file's column views, chunked as ingestion chunks them and numbered after its row chunks."""
    from src.data_pipeline.text_chunker import TextChunker

    narrator = ExcelNarrator()
    text = narrator.narrate_columns(narrator.load(path.read_bytes(), path.name))
    chunks = TextChunker().chunk_text(text, metadata={**metadata, "source_file": path.name})
    offset = max(row_indexes) + 1
    total = offset + len(chunks)
    for i, chunk in enumerate(chunks):
        index = offset + i
        chunk["chunk_id"] = f"{path.name.replace('.', '_')}_chunk_{index}"
        chunk["metadata"].update({"chunk_index": index, "total_chunks": total, "is_first_chunk": False,
                                  "is_last_chunk": index == total - 1})
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path, help="workbooks (.xlsx or .csv) already in the index")
    parser.add_argument("--dry-run", action="store_true", help="offline: chunk and count, upload nothing")
    parser.add_argument("--out", type=Path, help="with --dry-run: write the chunks here (jsonl)")
    parser.add_argument("--delete", action="store_true", help="delete the files' column-view chunks")
    args = parser.parse_args()

    if args.dry_run:
        snapshot = load_snapshot()
        out = []
        for path in args.files:
            rows = [c for c in snapshot if c["source_file"] == path.name and not is_column_view(c["content"])]
            if not rows:
                print(f"{path.name}: not in the snapshot")
                return 1
            chunks = column_chunks(path, [c["chunk_index"] for c in rows], {"file_type": path.suffix.lstrip(".")})
            print(f"{path.name}: {len(rows)} row chunks; {len(chunks)} column-view chunks would be "
                  f"chunk_index {chunks[0]['metadata']['chunk_index']} to {chunks[-1]['metadata']['chunk_index']}")
            out += [{"chunk_id": c["chunk_id"], "chunk_index": c["metadata"]["chunk_index"], "content": c["content"],
                     "file_type": c["metadata"]["file_type"], "source_file": path.name} for c in chunks]
        if args.out:
            args.out.write_text("".join(json.dumps(c) + "\n" for c in out), encoding="utf-8")
            print(f"Wrote {len(out)} chunks to {args.out}")
        return 0

    from src.data_pipeline.embedder import Embedder
    from src.data_pipeline.indexer import AzureSearchIndexer
    from src.retrieval.retriever import AzureSearchRetriever

    search_client = AzureSearchRetriever().search_client
    for path in args.files:
        indexed = indexed_chunks(search_client, path.name)
        views = [doc for doc in indexed if is_column_view(doc["content"])]
        if args.delete:
            if views:
                search_client.delete_documents(documents=[{"id": doc["id"]} for doc in views])
            print(f"{path.name}: deleted {len(views)} column-view chunks")
            continue
        if views:
            print(f"{path.name}: the index already holds {len(views)} column-view chunks (--delete them first)")
            return 1
        if not indexed:
            print(f"{path.name}: not in the index; ingest it with main.py instead")
            return 1
        first = min(indexed, key=lambda doc: doc["chunk_index"])
        metadata = {k: v for k, v in json.loads(first.get("metadata") or "{}").items() if k not in CHUNK_KEYS}
        chunks = column_chunks(path, [doc["chunk_index"] for doc in indexed], metadata)
        result = AzureSearchIndexer().index_documents(Embedder().embed_chunks(chunks))
        print(f"{path.name}: {result['indexed']} column-view chunks indexed, {result['failed']} failed")
        if result["failed"]:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
