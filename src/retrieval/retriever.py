"""
Retrieval Module for Azure AI Search.

This module implements hybrid search (vector + keyword) retrieval
using Azure AI Search.
"""

import logging
import re
from typing import List, Dict, Any, Optional
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential

from config import get_config
from src.data_pipeline.embedder import Embedder

logger = logging.getLogger(__name__)


class RetrievalError(Exception):
    """Raised when retrieval operations fail."""
    pass


# The spreadsheets the A2A Excel agent ingests
WORKBOOK_TYPES = ("xlsx", "csv")
WORKBOOK_FILTER = " or ".join(f"file_type eq '{t}'" for t in WORKBOOK_TYPES)


def _is_workbook(doc: Dict[str, Any]) -> bool:
    return (doc.get("source_file") or "").lower().endswith(tuple(f".{t}" for t in WORKBOOK_TYPES))


# The covered companies, by the ticker their chunks carry (index_selection.csv), and the names, tickers and
# products a question may call them by: whole words, any case, except MU (a common word) in capitals only
COMPANY_PATTERNS = {
    "NVDA": r"(?i:\b(?:nvidia|nvda|geforce|blackwell)\b)",
    "AMD": r"(?i:\b(?:amd|advanced micro devices|epyc|ryzen|radeon)\b)",
    "AVGO": r"(?i:\b(?:broadcom|avgo|vmware)\b)",
    "INTC": r"(?i:\b(?:intel|intc)\b)",
    "MU": r"(?i:\bmicron\b)|\bMU\b",
    "MSFT": r"(?i:\b(?:microsoft|msft|azure|xbox|linkedin)\b)",
    "GOOGL": r"(?i:\b(?:alphabet|google|googl|goog|youtube|waymo)\b)",
    "META": r"(?i:\b(?:meta|facebook|instagram|whatsapp|reality labs)\b)",
}
# Company routing searches each named company on its own up to this many companies; a query naming more
# (all eight, say) is a cross-company question the comparison workbook answers, so it runs one search
MAX_ROUTED_COMPANIES = 3


def companies_in(text: str) -> List[str]:
    """The tickers of the covered companies a text names, in the order it first names them."""
    found = []
    for ticker, pattern in COMPANY_PATTERNS.items():
        match = re.search(pattern, text or "")
        if match:
            found.append((match.start(), ticker))
    return [ticker for _, ticker in sorted(found)]


def company_filter(tickers: List[str]) -> str:
    """The chunks of these companies, and the comparison workbook's (it covers all eight, so has no ticker)."""
    return " or ".join([f"ticker eq '{t}'" for t in tickers] + ["ticker eq null"])


def all_filters(*filters: Optional[str]) -> Optional[str]:
    """The OData filters combined with 'and' (None when there are none)."""
    parts = [f for f in filters if f]
    if len(parts) <= 1:
        return parts[0] if parts else None
    return " and ".join(f"({f})" for f in parts)


class AzureSearchRetriever:
    """
    Retrieves relevant documents using Azure AI Search hybrid search.

    Combines:
    - Vector search (semantic similarity using embeddings)
    - Keyword search (BM25 text matching)
    """

    # The fields every search returns
    SELECT_FIELDS = ["id", "content", "source_file", "chunk_index", "metadata"]
    # hybrid: vector + keyword (the app's default); vector: embeddings only; keyword: BM25 only
    MODES = ("hybrid", "vector", "keyword")

    def __init__(self, index_name: str = None, top_k: int = None):
        """
        Initialize the retriever.

        Args:
            index_name: Name of the search index. If None, uses config default.
            top_k: Number of results to retrieve. If None, uses config default.
        """
        config = get_config()

        self.index_name = index_name or config.search_index_name
        self.top_k = top_k or config.retrieval_top_k
        # Hybrid search fuses the keyword results with this many vector neighbours, never fewer than top_k.
        # With only top_k of them, each neighbour is also a keyword match (a long question shares a word
        # with nearly every chunk), so the fusion ranks them first and no keyword-only chunk gets in.
        self.hybrid_knn = config.retrieval_hybrid_knn
        # retrieve() adds up to workbook_expand workbook chunks (20 by default) when workbook_trigger of the
        # top results are workbook chunks (0: off)
        self.workbook_expand = config.retrieval_workbook_expand
        self.workbook_trigger = config.retrieval_workbook_trigger
        # retrieve() searches each company a query names on its own (off unless RETRIEVAL_COMPANY_ROUTING=true)
        self.company_routing = config.retrieval_company_routing
        # ...and gives a query naming just one company at least single_company_top_k of its chunks (0: top_k)
        self.single_company_top_k = config.retrieval_single_company_top_k

        # Initialize search client
        self.search_client = SearchClient(
            endpoint=config.search_endpoint,
            index_name=self.index_name,
            credential=AzureKeyCredential(config.search_admin_key)
        )

        # Initialize embedder for query vectorization
        self.embedder = Embedder()

        logger.info(
            f"AzureSearchRetriever initialized for index: {self.index_name}, "
            f"top_k: {self.top_k}"
        )

    def embed_query(self, query: str) -> List[float]:
        """The query's embedding, for callers that run several searches with one question."""
        return self.embedder.embed_text(query)

    def search(self, query: str, top_k: int = None, mode: str = "hybrid",
               filter_expr: Optional[str] = None,
               vector: Optional[List[float]] = None) -> List[Dict[str, Any]]:
        """
        Search the index in one of three modes, optionally filtered.

        Args:
            query: Search query
            top_k: Number of results to retrieve (overrides default)
            mode: "hybrid" (vector + keyword), "vector" or "keyword"
            filter_expr: OData filter expression, applied in every mode
                        Example: "file_type ne 'xlsx'"
            vector: A precomputed query embedding (vector and hybrid modes); embedded here if None

        Returns:
            List of retrieved documents with metadata and scores

        Raises:
            ValueError: If the query is empty or the mode unknown
            RetrievalError: If retrieval fails
        """
        if not query or not query.strip():
            raise ValueError("Query cannot be empty")
        if mode not in self.MODES:
            raise ValueError(f"Unknown search mode {mode!r}; expected one of {', '.join(self.MODES)}")

        k = top_k or self.top_k

        logger.info(
            f"Retrieving documents for query: '{query[:50]}...' "
            f"(top_k={k}, mode={mode}" + (f", filter={filter_expr}" if filter_expr else "") + ")"
        )

        try:
            request = {
                # Keyword search (BM25) in hybrid and keyword modes
                "search_text": query if mode in ("hybrid", "keyword") else None,
                "top": k,
                "select": self.SELECT_FIELDS,
            }
            if mode != "keyword":
                # Generate query embedding
                if vector is None:
                    logger.info("Generating query embedding...")
                    vector = self.embedder.embed_text(query)
                    self._last_embedding = (query, vector)  # retrieve()'s workbook expansion reuses it
                request["vector_queries"] = [VectorizedQuery(
                    vector=vector,
                    k_nearest_neighbors=max(k, self.hybrid_knn) if mode == "hybrid" else k,
                    fields="content_vector"
                )]
            if filter_expr:
                request["filter"] = filter_expr

            results = self.search_client.search(**request)

            # Process results
            documents = self._process_results(results)

            logger.info(f"Retrieved {len(documents)} documents")

            return documents

        except Exception as e:
            logger.error(f"Error retrieving documents: {e}")
            raise RetrievalError(f"Retrieval failed: {e}") from e

    def retrieve(self, query: str, top_k: int = None,
                 use_hybrid: bool = True, filter_expr: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieve relevant documents for a query.

        With company routing on (RETRIEVAL_COMPANY_ROUTING=true), a query that names one to three of the
        covered companies searches each one's chunks, plus the comparison workbook's, on its own and merges
        the results rank by rank: every company it names gets top_k results. Other queries run one search.
        A query naming just one company gets at least RETRIEVAL_SINGLE_COMPANY_TOP_K results (when set).

        With workbook expansion on (RETRIEVAL_WORKBOOK_EXPAND above 0), a question whose top results hold
        at least RETRIEVAL_WORKBOOK_TRIGGER workbook chunks also gets up to RETRIEVAL_WORKBOOK_EXPAND more
        workbook chunks, from a second search over the workbooks, after its top results.

        Args:
            query: Search query
            top_k: Number of results to retrieve (overrides default)
            use_hybrid: If True, use hybrid search (vector + keyword).
                       If False, use vector search only.
            filter_expr: OData filter applied to every search, as well as routing's and expansion's own
                        Example: "file_type ne 'xlsx'"

        Returns:
            List of retrieved documents with metadata and scores

        Raises:
            RetrievalError: If retrieval fails
        """
        mode = "hybrid" if use_hybrid else "vector"
        k = top_k or self.top_k
        tickers = companies_in(query) if self.company_routing else []
        if len(tickers) > MAX_ROUTED_COMPANIES:
            tickers = []
        if not tickers:
            documents = self.search(query, top_k=top_k, mode=mode, filter_expr=filter_expr)
        else:
            # Company routing: a chunk that never names its company (a table, a later paragraph) loses to
            # chunks that do, from other companies' documents and the workbook rows; searching only the
            # named company's chunks takes those away, and a question about two companies gets both
            # A question about one company may also get more of its chunks: they are all that company's,
            # and its right passage may sit just below the top_k (a file's other passages share its words)
            routed_k = max(k, self.single_company_top_k) if len(tickers) == 1 else k
            vector = self.embed_query(query)
            self._last_embedding = (query, vector)
            results = [self.search(query, top_k=routed_k, mode=mode,
                                   filter_expr=all_filters(company_filter([t]), filter_expr), vector=vector)
                       for t in tickers]
            documents, seen = [], set()
            for rank in range(routed_k):
                for docs in results:
                    if rank < len(docs) and docs[rank].get("id") not in seen:
                        seen.add(docs[rank].get("id"))
                        documents.append(docs[rank])
            logger.info(f"Company routing: {', '.join(tickers)} ({routed_k} results each)")
        if self.workbook_expand <= 0:
            return documents

        # Workbook expansion: a question whose top results are mostly workbook chunks reads a workbook.
        # The narrator writes one sentence per row, so one column of a sheet (eight companies, twelve
        # quarters) spreads over many chunks; a second search over the workbooks adds more of them.
        if sum(1 for doc in documents[:k] if _is_workbook(doc)) < self.workbook_trigger:
            return documents
        cached = getattr(self, "_last_embedding", None)
        vector = cached[1] if cached and cached[0] == query else self.embed_query(query)
        extra = self.search(query, top_k=k + self.workbook_expand, mode=mode,
                            filter_expr=all_filters(WORKBOOK_FILTER, company_filter(tickers) if tickers else None,
                                                    filter_expr),
                            vector=vector)
        seen = {doc.get("id") for doc in documents}
        added = [doc for doc in extra if doc.get("id") not in seen][:self.workbook_expand]
        logger.info(f"Workbook expansion: {len(added)} more workbook chunks")
        return documents + added

    def retrieve_with_filter(self, query: str, filter_expr: str,
                           top_k: int = None) -> List[Dict[str, Any]]:
        """
        Retrieve documents with filtering (hybrid search).

        Args:
            query: Search query
            filter_expr: OData filter expression
                        Example: "company eq 'NVIDIA'" or
                        "source_file eq 'NVDA_FY2027-Q2_transcript.pdf'"
            top_k: Number of results to retrieve

        Returns:
            List of retrieved documents

        Raises:
            RetrievalError: If retrieval fails
        """
        logger.info(f"Retrieving with filter: {filter_expr}")
        return self.search(query, top_k=top_k, mode="hybrid", filter_expr=filter_expr)

    def _process_results(self, results) -> List[Dict[str, Any]]:
        """
        Process search results into a structured format.

        Args:
            results: Search results from Azure AI Search

        Returns:
            List of document dictionaries
        """
        import json

        documents = []

        for result in results:
            # Extract score from Azure Search results
            # Try different possible score field names
            score = None
            if hasattr(result, '__getitem__'):  # Dictionary-like object
                # Azure Search returns score with @ prefix
                score = result.get('@search.score') or result.get('search_score') or result.get('score')
            else:  # Object with attributes
                score = getattr(result, 'score', None)

            # Log if score is still None for debugging
            if score is None:
                logger.debug(f"No score found for result: {result.get('id', 'unknown')}")

            # Parse metadata JSON
            metadata_str = result.get('metadata', '{}')
            try:
                metadata = json.loads(metadata_str)
            except:
                metadata = {}

            # Create document dictionary
            doc = {
                "id": result.get('id'),
                "content": result.get('content'),
                "source_file": result.get('source_file'),
                "chunk_index": result.get('chunk_index'),
                "score": score if score is not None else 0.0,  # Default to 0.0 if no score
                "metadata": metadata
            }

            documents.append(doc)

        return documents

    def get_context_for_query(self, query: str, top_k: int = None) -> str:
        """
        Retrieve documents and format them as context for LLM.

        Args:
            query: Search query
            top_k: Number of results to retrieve

        Returns:
            Formatted context string ready for LLM consumption

        Example:
            >>> context = retriever.get_context_for_query("NVIDIA Data Center revenue")
            >>> print(context)
        """
        documents = self.retrieve(query, top_k=top_k)

        if not documents:
            return "No relevant documents found."

        # Format documents as context
        context_parts = []
        for i, doc in enumerate(documents, 1):
            source = doc.get('source_file', 'Unknown')
            content = doc.get('content', '')

            context_parts.append(
                f"[Document {i}] (Source: {source})\n{content}"
            )

        context = "\n\n".join(context_parts)
        return context


def retrieve_documents(query: str, top_k: int = None) -> List[Dict[str, Any]]:
    """
    Convenience function to retrieve documents.

    Args:
        query: Search query
        top_k: Number of results to retrieve

    Returns:
        List of retrieved documents

    Example:
        >>> docs = retrieve_documents("Which company had the highest revenue in calendar Q2 2026?", top_k=3)
        >>> for doc in docs:
        ...     print(doc['content'][:100])
    """
    retriever = AzureSearchRetriever(top_k=top_k)
    return retriever.retrieve(query)


def get_context(query: str, top_k: int = None) -> str:
    """
    Convenience function to get formatted context for a query.

    Args:
        query: Search query
        top_k: Number of results to retrieve

    Returns:
        Formatted context string

    Example:
        >>> context = get_context("NVIDIA Data Center revenue")
        >>> print(context)
    """
    retriever = AzureSearchRetriever(top_k=top_k)
    return retriever.get_context_for_query(query)


if __name__ == "__main__":
    # Test the retriever
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])

        try:
            retriever = AzureSearchRetriever()

            print("\n" + "=" * 60)
            print("RETRIEVAL TEST")
            print("=" * 60)
            print(f"Query: {query}")

            # Retrieve documents
            documents = retriever.retrieve(query, top_k=5)

            print(f"\nRetrieved {len(documents)} documents:")
            print("=" * 60)

            for i, doc in enumerate(documents, 1):
                print(f"\n[{i}] Score: {doc.get('score', 'N/A'):.4f}" if doc.get('score') else f"\n[{i}]")
                print(f"Source: {doc.get('source_file', 'Unknown')}")
                print(f"Content: {doc.get('content', '')[:200]}...")
                print("-" * 60)

            # Get formatted context
            print("\n" + "=" * 60)
            print("FORMATTED CONTEXT")
            print("=" * 60)
            context = retriever.get_context_for_query(query, top_k=3)
            print(context[:500] + "..." if len(context) > 500 else context)

        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        print("Usage: python retriever.py <query>")
        print("Example: python retriever.py NVIDIA Data Center revenue")
        sys.exit(1)
