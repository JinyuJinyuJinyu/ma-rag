"""Watch the app's retrieval calls, and apply the XLSX ablation's filter.

retrieval_probe() swaps `retrieve` on one retriever object for the length of one question, then puts
the original back, even after an exception. Under `full` it only records what came back. Under
`no_xlsx` it passes NO_XLSX_FILTER, which hides every spreadsheet the A2A Excel agent ingested (.xlsx and
.csv), to the app's own retrieve() as `filter_expr`, so company routing runs as under `full`; a retrieve()
without that argument is replaced by `retriever.search(..., filter_expr=NO_XLSX_FILTER)`. Neither the app's
code nor the index changes.

api_retrieval_probe() does the same for the API route, which builds a new MultiAgentRAG (and so a new
retriever) per request: it patches AzureSearchRetriever.retrieve on the class instead. The API route
always runs under `full`.

The probes also keep any retrieval exception: the supervisor agent swallows them (it answers with no
chunks), so without a probe a failed or rejected search would look like an empty one.
"""

import inspect
from contextlib import contextmanager

from evaluation.evidence import is_spreadsheet

SPREADSHEET_TYPES = ("xlsx", "csv")
NO_XLSX_FILTER = " and ".join(f"file_type ne '{t}'" for t in SPREADSHEET_TYPES)
FILTERS = {"full": None, "no_xlsx": NO_XLSX_FILTER}


class Probe:
    def __init__(self, condition: str, filter_expr):
        self.condition = condition
        self.filter = filter_expr
        self.calls = 0
        self.queries = []  # the query of every retrieval call, in order
        self.docs = None   # the last call's documents, with their parsed metadata
        self.error = None  # the exception retrieval raised, if any


def _takes_filter(retrieve) -> bool:
    """True if a retrieve() takes a filter_expr argument (the app's does)."""
    try:
        return "filter_expr" in inspect.signature(retrieve).parameters
    except (TypeError, ValueError):
        return False


def _probed(probe: Probe, retriever, original, query, *args, **kwargs):
    """One retrieve() call: recorded, and under a filter given the filter, or sent through retriever.search.

    Unfiltered, the caller's arguments go to the original unchanged. Filtered, a retrieve() that takes
    filter_expr gets the filter (with any the caller gave); for any other, top_k and use_hybrid (positional
    or keyword, as retrieve() takes them) choose the search's size and mode.
    """
    probe.calls += 1
    probe.queries.append(query)
    try:
        if probe.filter is None:
            docs = original(query, *args, **kwargs)
        elif _takes_filter(original):
            given = kwargs.get("filter_expr")
            kwargs["filter_expr"] = f"({given}) and ({probe.filter})" if given else probe.filter
            docs = original(query, *args, **kwargs)
        else:
            top_k = kwargs.get("top_k", args[0] if args else None) or retriever.top_k
            use_hybrid = kwargs.get("use_hybrid", args[1] if len(args) > 1 else True)
            if hasattr(retriever, "search"):
                docs = retriever.search(query, top_k=top_k, mode="hybrid" if use_hybrid else "vector",
                                        filter_expr=probe.filter)
            else:  # a retriever without search(): its filtered hybrid search
                docs = retriever.retrieve_with_filter(query, probe.filter, top_k=top_k)
    except Exception as e:
        probe.error = e
        raise
    probe.docs = list(docs or [])
    return docs


@contextmanager
def retrieval_probe(retriever, condition: str = "full"):
    if condition not in FILTERS:
        raise ValueError(f"unknown condition {condition!r}")
    probe = Probe(condition, FILTERS[condition])
    own = vars(retriever)
    had_own, saved = "retrieve" in own, own.get("retrieve")
    original = retriever.retrieve
    retriever.retrieve = lambda query, *args, **kwargs: _probed(probe, retriever, original, query, *args, **kwargs)
    try:
        yield probe
    finally:
        if had_own:
            retriever.retrieve = saved
        else:
            own.pop("retrieve", None)


@contextmanager
def api_retrieval_probe():
    """Record every AzureSearchRetriever.retrieve call made while the API answers (full only)."""
    from src.retrieval.retriever import AzureSearchRetriever

    probe = Probe("full", None)
    original = AzureSearchRetriever.__dict__["retrieve"]

    def retrieve(self, query, *args, **kwargs):
        return _probed(probe, self, lambda *a, **k: original(self, *a, **k), query, *args, **kwargs)

    AzureSearchRetriever.retrieve = retrieve
    try:
        yield probe
    finally:
        AzureSearchRetriever.retrieve = original


def filter_leaked(condition: str, chunks) -> list[str]:
    """Under no_xlsx, the spreadsheet sources that still came back (the filter leaked)."""
    if condition != "no_xlsx":
        return []
    return [c.get("source_file") or "" for c in chunks
            if (c.get("file_type") or "") in SPREADSHEET_TYPES or is_spreadsheet(c.get("source_file"))]


def is_filter_rejection(error: BaseException) -> bool:
    """True if Azure AI Search refused the filter, looking through wrapped exceptions.

    Only a search error counts: the embedding call (openai) can also fail with HTTP 400, and that
    is an ordinary retrieval error, not a rejected filter.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        text = str(error)
        if "InvalidRequestParameter" in text or "Invalid expression" in text:
            return True
        module = type(error).__module__ or ""
        if getattr(error, "status_code", None) == 400 and module.startswith("azure"):
            return True
        error = error.__cause__ or error.__context__
    return False
