"""Watch the supervisor agent's retrieval call, and apply the XLSX ablation's filter.

retrieval_probe() swaps `retrieve` on one retriever object for the length of one question, then puts
the original back, even after an exception. Under `full` it only records what came back. Under
`no_xlsx` it calls `retrieve_with_filter(query, "file_type ne 'xlsx'")` with the same top_k instead,
which hides both A2A workbooks from search. Neither the app's code nor the index changes.

The probe also keeps any retrieval exception: the supervisor agent swallows them (it answers with no
chunks), so without the probe a rejected filter would look like an empty search.
"""

from contextlib import contextmanager

NO_XLSX_FILTER = "file_type ne 'xlsx'"
FILTERS = {"full": None, "no_xlsx": NO_XLSX_FILTER}


class Probe:
    def __init__(self, condition: str, filter_expr):
        self.condition = condition
        self.filter = filter_expr
        self.calls = 0
        self.docs = None   # the retriever's documents, with their parsed metadata
        self.error = None  # the exception retrieval raised, if any


@contextmanager
def retrieval_probe(retriever, condition: str = "full"):
    if condition not in FILTERS:
        raise ValueError(f"unknown condition {condition!r}")
    probe = Probe(condition, FILTERS[condition])
    own = vars(retriever)
    had_own, saved = "retrieve" in own, own.get("retrieve")
    original = retriever.retrieve

    def probed_retrieve(query, top_k=None, **kwargs):
        probe.calls += 1
        try:
            if probe.filter is None:
                docs = original(query, top_k=top_k, **kwargs)
            else:
                docs = retriever.retrieve_with_filter(query, probe.filter, top_k=top_k or retriever.top_k)
        except Exception as e:
            probe.error = e
            raise
        probe.docs = list(docs or [])
        return docs

    retriever.retrieve = probed_retrieve
    try:
        yield probe
    finally:
        if had_own:
            retriever.retrieve = saved
        else:
            own.pop("retrieve", None)


def filter_leaked(condition: str, retrieved_sources) -> list[str]:
    """Under no_xlsx, the .xlsx sources that still came back (the filter leaked)."""
    if condition != "no_xlsx":
        return []
    return [s for s in retrieved_sources if (s or "").lower().endswith(".xlsx")]


def is_filter_rejection(error: BaseException) -> bool:
    """True if Azure AI Search refused the filter (HTTP 400), looking through wrapped exceptions."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "status_code", None) == 400:
            return True
        text = str(error)
        if "InvalidRequestParameter" in text or "Invalid expression" in text or "(400)" in text \
                or "Status code: 400" in text:
            return True
        error = error.__cause__ or error.__context__
    return False
