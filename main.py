#!/usr/bin/env python3
"""
Multi-Agent RAG System - CLI Orchestration Script

This script provides a command-line interface for:
- Processing documents through the pipeline
- Indexing the planned file selection of the AI Earnings Desk dataset
- Testing retrieval
- Testing multi-agent query
- Managing the index
"""

import argparse
import logging
import sys
from pathlib import Path

from src.data_pipeline.document_extractor import DocumentExtractor
from src.data_pipeline.a2a_excel_client import A2AExcelClient
from src.data_pipeline.local_extractor import LocalExtractor
from src.data_pipeline.selection import DEFAULT_SELECTION_CSV, find_metadata, load_selection
from src.data_pipeline.text_chunker import TextChunker
from src.data_pipeline.embedder import Embedder
from src.data_pipeline.indexer import AzureSearchIndexer
from src.retrieval.retriever import AzureSearchRetriever
from src.agents.rag_agent import MultiAgentRAG
from src.monitoring.logger import setup_logging
from config import get_config, ConfigurationError


def setup_cli_logging():
    """Setup logging for CLI."""
    setup_logging(logging.INFO)
    return logging.getLogger(__name__)


SPREADSHEET_SUFFIXES = {".xlsx", ".csv"}
LOCAL_SUFFIXES = {".html", ".htm", ".docx"}
SUPPORTED_SUFFIXES = {".pdf"} | SPREADSHEET_SUFFIXES | LOCAL_SUFFIXES


def run_pipeline(file_path: str, logger, extra_metadata: dict = None) -> dict:
    """
    Run one document through the full pipeline.

    Routes by file type: PDF via Azure Document Intelligence, XLSX/CSV via the
    standalone A2A Excel Narrator Agent service, HTML/DOCX via local text
    extraction (no Azure calls).

    Args:
        file_path: Path to a PDF, XLSX, CSV, HTML or DOCX file
        logger: Logger instance
        extra_metadata: Attributes stored with every chunk (company, ticker, ...)

    Returns:
        Dictionary with the chunk counts

    Raises:
        Exception: Any extraction, embedding or indexing failure
    """
    logger.info("=" * 60)
    logger.info("DOCUMENT PROCESSING PIPELINE")
    logger.info("=" * 60)

    source_file = Path(file_path)
    if not source_file.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = source_file.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"Unsupported file type '{suffix}'. Expected one of: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
    is_spreadsheet = suffix in SPREADSHEET_SUFFIXES

    # Step 1: Extract text
    if is_spreadsheet:
        logger.info("\n[1/5] Extracting via A2A Excel Narrator Agent...")
        extractor = A2AExcelClient()
        extraction_result = extractor.extract_text_from_file(str(source_file))
        unit = "sheets"
    elif suffix in LOCAL_SUFFIXES:
        logger.info(f"\n[1/5] Extracting text from {suffix.lstrip('.').upper()} (local)...")
        extractor = LocalExtractor()
        extraction_result = extractor.extract_text_from_file(str(source_file))
        unit = "file"
    else:
        logger.info("\n[1/5] Extracting text from PDF...")
        extractor = DocumentExtractor()
        extraction_result = extractor.extract_text_from_pdf(str(source_file))
        unit = "pages"

    extracted_text = extraction_result["text"]
    metadata = extraction_result["metadata"]
    metadata["file_type"] = suffix.lstrip(".")
    metadata.update(extra_metadata or {})

    logger.info(f"✓ Extracted {len(extracted_text)} characters from {extraction_result['page_count']} {unit}")

    # Step 2: Chunk text
    logger.info("\n[2/5] Chunking text...")
    chunker = TextChunker()
    chunks = chunker.chunk_text(extracted_text, metadata=metadata)

    stats = chunker.get_chunk_statistics(chunks)
    logger.info(f"✓ Created {stats['total_chunks']} chunks")
    logger.info(f"  - Avg size: {stats['avg_chunk_size']:.0f} chars")
    logger.info(f"  - Min/Max: {stats['min_chunk_size']}/{stats['max_chunk_size']}")

    # Step 3: Generate embeddings
    logger.info("\n[3/5] Generating embeddings...")
    embedder = Embedder()
    chunks_with_embeddings = embedder.embed_chunks(chunks)

    logger.info(f"✓ Generated embeddings for {len(chunks_with_embeddings)} chunks")

    # Step 4: Create/verify index
    logger.info("\n[4/5] Setting up search index...")
    indexer = AzureSearchIndexer()
    created = indexer.create_index(recreate=False)

    if created:
        logger.info("✓ Index created successfully")
    else:
        logger.info("✓ Using existing index")

    # Step 5: Index documents
    logger.info("\n[5/5] Indexing documents...")
    result = indexer.index_documents(chunks_with_embeddings)

    logger.info(f"✓ Indexed {result['indexed']} chunks")
    if result['failed'] > 0:
        logger.warning(f"⚠ {result['failed']} chunks failed to index")

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("PROCESSING COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Source file: {source_file.name}")
    logger.info(f"Chunks created: {len(chunks)}")
    logger.info(f"Chunks indexed: {result['indexed']}")
    logger.info("=" * 60)

    return {"chunks_created": len(chunks), "chunks_indexed": result["indexed"], "chunks_failed": result["failed"]}


def process_document(file_path: str, logger):
    """
    Process one document through the full pipeline (the `process` command).

    If the file is listed in the dataset's index_selection.csv, its company,
    ticker, period and document kind are stored with its chunks.

    Args:
        file_path: Path to a PDF, XLSX, CSV, HTML or DOCX file
        logger: Logger instance
    """
    try:
        extra_metadata = find_metadata(file_path)
        if extra_metadata:
            logger.info(f"Listed in {DEFAULT_SELECTION_CSV.name}: {extra_metadata}")
        run_pipeline(file_path, logger, extra_metadata)
    except Exception as e:
        logger.error(f"Error processing document: {e}")
        sys.exit(1)


def ingest_selection(tier: str, types: str, csv_path: str, dry_run: bool, logger):
    """
    Index the files planned in a dataset's index_selection.csv (the `ingest` command).

    Each file runs through run_pipeline() with its company, ticker, period and
    document kind attached. A failed file is reported and the rest continue;
    re-running is safe because chunk IDs are stable, so chunks are replaced,
    not duplicated.

    Args:
        tier: "1", "2" or "all"
        types: Comma-separated file types to include (e.g. "pdf,html,docx"), or None for all
        csv_path: Path to index_selection.csv, or None for the ai-earnings-desk selection
        dry_run: If True, only list the files and estimates; nothing is sent to Azure
        logger: Logger instance
    """
    tiers = {1, 2} if tier == "all" else {int(tier)}
    wanted = {t.strip().lower().lstrip(".") for t in types.split(",")} if types else None
    files = [
        item for item in load_selection(csv_path)
        if item.tier in tiers and (wanted is None or item.file_type in wanted)
    ]

    logger.info("=" * 60)
    logger.info("INDEX SELECTION" + (" (dry run: nothing is sent to Azure)" if dry_run else ""))
    logger.info("=" * 60)
    for i, item in enumerate(files, 1):
        if not item.path.exists():
            status = "MISSING"
        elif item.needs_preparing:
            status = "needs cleaning"
        elif item.prepared:
            status = "prepared copy"
        else:
            status = "ok"
        logger.info(
            f"[{i:>2}] tier {item.tier}  {item.file_type:<4}  ~{item.est_chunks:>3} chunks  "
            f"{item.pdf_pages:>2} PDF pages  {status:<14}  {item.listed_path}"
        )
    logger.info("-" * 60)
    logger.info(
        f"Files: {len(files)}; estimated chunks: {sum(f.est_chunks for f in files)}; "
        f"Document Intelligence pages: {sum(f.pdf_pages for f in files)}"
    )
    if any(f.file_type in ("xlsx", "csv") for f in files):
        logger.info("XLSX/CSV files go through the Excel Narrator Agent: start it first with "
                    "`python -m services.excel_agent`")

    if dry_run or not files:
        return

    failures = []
    indexed = 0
    for i, item in enumerate(files, 1):
        logger.info(f"\n### File {i}/{len(files)}: {item.listed_path}")
        if item.needs_preparing:
            message = f"needs cleaning first ({item.note}); save the cleaned copy as {item.prepared_path}"
            logger.error(message)
            failures.append((item.listed_path, message))
            continue
        try:
            if item.prepared:
                logger.info(f"Using the prepared copy: {item.path}")
            result = run_pipeline(str(item.path), logger, item.metadata)
            indexed += result["chunks_indexed"]
        except Exception as e:
            logger.error(f"Error processing {item.listed_path}: {e}")
            failures.append((item.listed_path, str(e)))

    logger.info("\n" + "=" * 60)
    logger.info("INGEST COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Files indexed: {len(files) - len(failures)} of {len(files)}")
    logger.info(f"Chunks indexed: {indexed}")
    for listed_path, message in failures:
        logger.error(f"✗ {listed_path}: {message}")
    logger.info("=" * 60)
    if failures:
        sys.exit(1)


def test_retrieval(query: str, top_k: int, logger):
    """
    Test retrieval functionality.

    Args:
        query: Search query
        top_k: Number of results
        logger: Logger instance
    """
    logger.info("=" * 60)
    logger.info("TESTING RETRIEVAL")
    logger.info("=" * 60)
    logger.info(f"Query: {query}")
    logger.info(f"Top-K: {top_k}")

    try:
        retriever = AzureSearchRetriever(top_k=top_k)
        documents = retriever.retrieve(query)

        logger.info(f"\nRetrieved {len(documents)} documents:")
        logger.info("-" * 60)

        for i, doc in enumerate(documents, 1):
            meta = doc.get('metadata') or {}
            about = ", ".join(meta[k] for k in ("company", "fiscal_period", "doc_kind") if meta.get(k))
            logger.info(f"\n[{i}] Score: {doc.get('score', 'N/A')}")
            logger.info(f"Source: {doc.get('source_file', 'Unknown')}" + (f" ({about})" if about else ""))
            logger.info(f"Content: {doc.get('content', '')[:200]}...")

        logger.info("\n" + "=" * 60)

    except Exception as e:
        logger.error(f"Error testing retrieval: {e}")
        sys.exit(1)


def test_query(question: str, top_k: int, logger):
    """
    Test multi-agent query.

    Args:
        question: User question
        top_k: Number of chunks to retrieve
        logger: Logger instance
    """
    logger.info("=" * 60)
    logger.info("TESTING MULTI-AGENT RAG QUERY")
    logger.info("=" * 60)
    logger.info(f"Question: {question}")

    try:
        config = get_config()
        rag_system = MultiAgentRAG(
            top_k=top_k,
            guardrail_enabled=config.guardrail_enabled,
            guardrail_strictness=config.guardrail_strictness
        )
        response = rag_system.query(question)

        logger.info("\n" + "=" * 60)
        logger.info("RESPONSE")
        logger.info("=" * 60)
        logger.info(f"Guardrail: {'PASSED' if response['guardrail_passed'] else 'REJECTED'} - {response['guardrail_reason']}")
        logger.info(f"Intent: {response['intent']}")
        logger.info(f"Chunks Retrieved: {response['chunks_retrieved']}")
        logger.info(f"Sources: {', '.join(response['sources'])}")
        logger.info("\n" + "-" * 60)
        logger.info("Answer:")
        logger.info("-" * 60)
        logger.info(response['answer'])
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Error testing query: {e}")
        sys.exit(1)


def manage_index(action: str, logger):
    """
    Manage the search index.

    Args:
        action: Action to perform (create, status, delete)
        logger: Logger instance
    """
    logger.info("=" * 60)
    logger.info(f"INDEX MANAGEMENT: {action.upper()}")
    logger.info("=" * 60)

    try:
        indexer = AzureSearchIndexer()

        if action == "create":
            created = indexer.create_index(recreate=False)
            if created:
                logger.info("✓ Index created successfully")
            else:
                logger.info("ℹ Index already exists")

        elif action == "recreate":
            logger.warning("This will delete all existing data!")
            confirm = input("Are you sure? (yes/no): ")
            if confirm.lower() == "yes":
                indexer.create_index(recreate=True)
                logger.info("✓ Index recreated successfully")
            else:
                logger.info("Operation cancelled")

        elif action == "status":
            stats = indexer.get_index_statistics()
            logger.info(f"Index Name: {stats.get('index_name')}")
            logger.info(f"Exists: {stats.get('exists', True)}")
            logger.info(f"Document Count: {stats.get('document_count')}")
            logger.info(f"Vector Search: {stats.get('vector_search_enabled')}")
            logger.info(f"Semantic Search: {stats.get('semantic_search_enabled')}")
            if 'fields' in stats:
                logger.info(f"Fields: {', '.join(stats['fields'])}")

        elif action == "delete":
            logger.warning("This will delete all indexed data!")
            confirm = input("Are you sure? (yes/no): ")
            if confirm.lower() == "yes":
                indexer.delete_index()
                logger.info("✓ Index deleted successfully")
            else:
                logger.info("Operation cancelled")

        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Error managing index: {e}")
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Multi-Agent RAG System CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process one document (PDF, HTML, DOCX, XLSX or CSV)
  python main.py process datasets/ai-earnings-desk/NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_cfo_commentary.pdf
  python main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx

  # Index the planned selection (datasets/ai-earnings-desk/index_selection.csv)
  python main.py ingest --tier 1 --dry-run
  python main.py ingest --tier 1 --types pdf,html,docx

  # Test retrieval
  python main.py retrieve "NVIDIA Data Center revenue" --top-k 5

  # Test multi-agent query
  python main.py query "Which company had the highest revenue in calendar Q2 2026?"

  # Manage index
  python main.py index status
  python main.py index create
  python main.py index delete
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Process command
    process_parser = subparsers.add_parser("process", help="Process a document")
    process_parser.add_argument("file_path", help="Path to a PDF, HTML, DOCX, XLSX or CSV file")

    # Ingest command
    ingest_parser = subparsers.add_parser(
        "ingest", help="Index the files planned in a dataset's index_selection.csv"
    )
    ingest_parser.add_argument(
        "--tier", choices=["1", "2", "all"], default="1",
        help="Which tier to index: 1 = core, 2 = history, all = both (default: 1)"
    )
    ingest_parser.add_argument(
        "--types", default=None,
        help="Comma-separated file types to include, e.g. pdf,html,docx (default: all)"
    )
    ingest_parser.add_argument(
        "--selection", default=None,
        help=f"Path to index_selection.csv (default: {DEFAULT_SELECTION_CSV.relative_to(Path(__file__).resolve().parent)})"
    )
    ingest_parser.add_argument(
        "--dry-run", action="store_true",
        help="Only list the files and estimates; nothing is sent to Azure and no .env is needed"
    )

    # Retrieve command
    retrieve_parser = subparsers.add_parser("retrieve", help="Test retrieval")
    retrieve_parser.add_argument("query", help="Search query")
    retrieve_parser.add_argument("--top-k", type=int, default=5, help="Number of results (default: 5)")

    # Query command
    query_parser = subparsers.add_parser("query", help="Test multi-agent query")
    query_parser.add_argument("question", help="User question")
    query_parser.add_argument("--top-k", type=int, default=5, help="Number of chunks (default: 5)")

    # Index command
    index_parser = subparsers.add_parser("index", help="Manage search index")
    index_parser.add_argument(
        "action",
        choices=["create", "recreate", "status", "delete"],
        help="Index action"
    )

    args = parser.parse_args()

    # A dry run reads only local files, so it needs no Azure configuration
    # (setup_cli_logging() would connect Application Insights)
    if args.command == "ingest" and args.dry_run:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ingest_selection(args.tier, args.types, args.selection, True, logging.getLogger(__name__))
        return

    # Setup logging
    logger = setup_cli_logging()

    # Load configuration
    try:
        config = get_config()
        logger.info("Configuration loaded successfully")
    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Please check your .env file")
        sys.exit(1)

    # Execute command
    if args.command == "process":
        process_document(args.file_path, logger)

    elif args.command == "ingest":
        ingest_selection(args.tier, args.types, args.selection, False, logger)

    elif args.command == "retrieve":
        test_retrieval(args.query, args.top_k, logger)

    elif args.command == "query":
        test_query(args.question, args.top_k, logger)

    elif args.command == "index":
        manage_index(args.action, logger)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
