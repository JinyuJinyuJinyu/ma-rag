"""Offline tests for the retrieval eval: AzureSearchRetriever.search(), evaluation/evidence.py,
evaluation/snapshot.py, evaluation/retrieval.py and scripts/eval_retrieval.py.

No network: every test runs with a socket guard that fails on any connection attempt, and the app is
fed placeholder Azure settings. The tests write their own small golden sets; they never need
evaluation/golden_set.yaml. Run from the project root:

    python -m pytest tests/test_eval_retrieval.py -q
"""

import contextlib
import importlib.util
import io
import json
import math
import os
import socket
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLACEHOLDER_ENV = {
    "AZURE_AI_FOUNDRY_EMBEDDING_ENDPOINT": "https://placeholder.invalid/",
    "AZURE_AI_FOUNDRY_EMBEDDING_API_KEY": "placeholder",
    "AZURE_AI_FOUNDRY_EMBEDDING_DEPLOYMENT_NAME": "text-embedding-ada-002",
    "AZURE_AI_FOUNDRY_EMBEDDING_API_VERSION": "2024-08-01-preview",
    "AZURE_AI_FOUNDRY_GPT4_ENDPOINT": "https://placeholder.invalid/",
    "AZURE_AI_FOUNDRY_GPT4_API_KEY": "placeholder",
    "AZURE_AI_FOUNDRY_GPT4_DEPLOYMENT_NAME": "gpt-4.1-mini",
    "AZURE_AI_FOUNDRY_GPT4_API_VERSION": "2024-12-01-preview",
    "AZURE_SEARCH_ENDPOINT": "https://placeholder.invalid/",
    "AZURE_SEARCH_ADMIN_KEY": "placeholder",
    "AZURE_SEARCH_INDEX_NAME": "ai-earnings-desk",
    "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://placeholder.invalid/",
    "AZURE_DOCUMENT_INTELLIGENCE_KEY": "placeholder",
    "AZURE_CONTENT_SAFETY_ENDPOINT": "",
    "AZURE_CONTENT_SAFETY_KEY": "",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
}
# Set before anything imports config (load_dotenv never overrides), so the real .env can't be used
os.environ.update(PLACEHOLDER_ENV)

from evaluation import evidence, retrieval, snapshot  # noqa: E402
from evaluation.gold import load_gold, retrieval_queries  # noqa: E402
from evaluation.probe import NO_XLSX_FILTER  # noqa: E402


class NoNetwork:
    """Fails any socket connection, so a test that reaches for the network errors loudly."""

    def __enter__(self):
        def refuse(*args, **kwargs):
            raise AssertionError("a test tried to open a network connection")
        self._patches = [mock.patch.object(socket.socket, "connect", refuse),
                         mock.patch.object(socket.socket, "connect_ex", refuse),
                         mock.patch.object(socket, "create_connection", refuse)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self._guard = NoNetwork().__enter__()

    def tearDown(self):
        self._guard.__exit__()


# ---- A small golden set and a fake index ----------------------------------------------------------

GOLD_YAML = textwrap.dedent("""\
    - id: 1
      question: "What was NVIDIA's Data Center revenue in its fiscal Q2 2027?"
      kind: lookup
      expect: answer
      difficulty: easy
      route: [graph]
      companies: [NVIDIA]
      reference: "NVIDIA's Data Center revenue in fiscal Q2 2027 was a record $89.0 billion. It was up 117% year over year."
      numbers:
        - {text: "$89.0B"}
        - {text: "117%"}
      evidence:
        - fact: "Data Center revenue $89.0B"
          files: [NVDA_FY2027-Q2_cfo_commentary.pdf, NVDA_FY2027-Q2_transcript.pdf]
          all: ['data center', '89(\\.0)? ?(billion|B)\\b|89,0\\d\\d']
        - fact: "Data Center up 117%"
          files: [NVDA_FY2027-Q2_cfo_commentary.pdf]
          all: ['data center', '117 ?%']
    - id: 2
      question: "Which company had the highest revenue in calendar Q2 2026?"
      kind: superlative
      expect: answer
      difficulty: medium
      route: [graph]
      companies: [Alphabet, NVIDIA]
      reference: "Alphabet had the highest revenue in calendar Q2 2026, $119,796M. NVIDIA was second with $96,221M."
      numbers:
        - {text: "$119,796M"}
        - {text: "$96,221M"}
      evidence:
        - fact: "Rankings: highest revenue CY2026 Q2"
          files: [ai_earnings_comparison.xlsx]
          all: ['Highest revenue, CY2026 Q2', '119,?796']
    - id: 3
      question: "Why was Alphabet's net income in calendar Q2 2026 so far above its operating income?"
      kind: explanation
      expect: answer
      difficulty: medium
      route: [graph]
      companies: [Alphabet]
      reference: "Alphabet's other income in Q2 2026 included a net gain of $98.0 billion, mostly unrealized gains on equity securities."
      numbers:
        - {any_of: ["$98.0B", "$99.0B"]}
      mentions:
        - {any: ['unrealized|equity securities']}
      evidence:
        - fact: "workbook: Alphabet net income"
          files: [ai_earnings_comparison.xlsx]
          all: ['Alphabet', '112,?193']
        - fact: "other income gain $98.0B"
          any_of:
            - {files: [GOOGL_FY2026-Q2_press_release.pdf], all: ['98[.,]0 billion']}
            - {files: [GOOGL_FY2026-Q2_transcript.pdf], all: ['other income', '98 billion']}
    - id: 4
      question: "What was NVIDIA's revenue in its fiscal Q3 2027?"
      kind: unanswerable
      expect: decline
      difficulty: easy
      route: [graph]
      companies: [NVIDIA]
      reference: "The indexed documents don't include NVIDIA's fiscal Q3 2027 results."
      absent: ['third quarter of fiscal 2027 revenue was']
    - id: 5
      question: "Meta's Family of Apps revenue in Q2 2026, then Reality Labs"
      kind: follow_up
      expect: answer
      difficulty: medium
      route: [api]
      companies: [Meta]
      turns:
        - question: "What was Meta's Family of Apps revenue in Q2 2026?"
          reference: "Meta's Family of Apps revenue in Q2 2026 was $59.9 billion."
          numbers:
            - {text: "$59.9B"}
          evidence:
            - fact: "Family of Apps revenue"
              files: [META_FY2026-Q2_press_release.pdf]
              all: ['Family of Apps', '59[.,]9']
        - question: "And Reality Labs?"
          reference: "Meta's Reality Labs revenue in Q2 2026 was $0.4 billion."
          numbers:
            - {text: "$0.4B"}
          evidence:
            - fact: "Reality Labs revenue"
              files: [META_FY2026-Q2_press_release.pdf]
              all: ['Reality Labs', '0[.,]4 billion|\\b37\\d\\b']
    - id: 6
      question: "What's the weather in Sydney tomorrow?"
      kind: refuse_irrelevant
      expect: refuse_irrelevant
      difficulty: easy
      route: [graph]
""")


# A vertical question: it reads one column (CY2024 Q3) down the Gross margin sheet's rows, one unit per row
VERTICAL_YAML = textwrap.dedent(r"""
    - id: 7
      question: "Which company had the highest gross margin in calendar Q3 2024?"
      kind: superlative
      expect: answer
      difficulty: medium
      route: [graph]
      companies: [Meta]
      vertical: {op: max_min, sheet: Gross margin, chunks: 2}
      reference: "Meta had the highest gross margin of the eight companies in calendar Q3 2024, at 81.8%."
      numbers:
        - {text: "81.8%"}
      evidence:
        - {fact: NVIDIA 74.6%, files: [ai_earnings_comparison.xlsx], all: ['Gross margin - Company ''NVIDIA'': [^'']*?CY2024 Q3 is 74\.6;']}
        - {fact: Meta 81.8%, files: [ai_earnings_comparison.xlsx], all: ['Gross margin - Company ''Meta'': [^'']*?CY2024 Q3 is 81\.8;']}
""")


def write_gold(folder: Path, text: str = GOLD_YAML) -> Path:
    path = Path(folder) / "golden_set.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def chunk(chunk_id, source_file, content, chunk_index=0, file_type=None):
    return {"chunk_id": chunk_id, "source_file": source_file, "content": content, "chunk_index": chunk_index,
            "file_type": file_type or source_file.rsplit(".", 1)[1]}


# What the index holds in these tests: the evidence for items 1-3 and 5, plus distractors
INDEX = [
    chunk("nvda_cfo_0", "NVDA_FY2027-Q2_cfo_commentary.pdf", "Revenue $96,221 $81,615 $46,743 18 % 106 %", 0),
    chunk("nvda_cfo_2", "NVDA_FY2027-Q2_cfo_commentary.pdf",
          "Data Center revenue was a record $89,023 million, up 117 % from a year ago.", 2),
    chunk("nvda_tr_4", "NVDA_FY2027-Q2_transcript.pdf", "Data center revenue increased 18% to $89 billion.", 4),
    chunk("wb_148", "ai_earnings_comparison.xlsx",
          "Rankings — Ranking 'Highest revenue, CY2026 Q2': Company is Alphabet; Value is 119796; "
          "Runner-up is NVIDIA; Runner-up value is 96221; Units is USD millions.", 148),
    chunk("wb_30", "ai_earnings_comparison.xlsx",
          "Quarterly results — Company 'Alphabet': Net income (USD millions) is 112193.", 30),
    chunk("googl_pr_3", "GOOGL_FY2026-Q2_press_release.pdf",
          "Other income (expense), net, included a net gain of $98.0 billion, primarily unrealized gains.", 3),
    chunk("meta_pr_1", "META_FY2026-Q2_press_release.pdf", "Family of Apps revenue $59.9 billion.", 1),
    chunk("amd_pr_0", "AMD_FY2026-Q2_press_release.html", "AMD reported revenue of $11.5 billion.", 0),
]


def as_doc(c):
    """A snapshot chunk as AzureSearchRetriever.search() returns documents."""
    return {"id": c["chunk_id"], "content": c["content"], "source_file": c["source_file"],
            "chunk_index": c["chunk_index"], "score": 0.5, "metadata": {"file_type": c["file_type"]}}


class FakeRetriever:
    """search() over INDEX: questions mapped to ranked chunk ids; filters hide spreadsheets."""

    index_name = "test-index"
    top_k = 5

    def __init__(self, ranking=None, fail=None, leak=False):
        self.ranking = ranking or {}
        self.fail = fail or {}
        self.leak = leak
        self.searches, self.embeddings = [], []

    def embed_query(self, query):
        self.embeddings.append(query)
        return [0.1, 0.2, 0.3]

    def search(self, query, top_k=None, mode="hybrid", filter_expr=None, vector=None):
        self.searches.append({"query": query, "top_k": top_k, "mode": mode, "filter": filter_expr, "vector": vector})
        error = self.fail.get((query, mode, filter_expr))
        if error:
            raise error
        by_id = {c["chunk_id"]: c for c in INDEX}
        ids = self.ranking.get(query, [c["chunk_id"] for c in INDEX])
        if mode == "keyword":
            ids = list(reversed(ids))
        docs = [as_doc(by_id[i]) for i in ids]
        if filter_expr and not self.leak:
            docs = [d for d in docs if d["metadata"]["file_type"] not in ("xlsx", "csv")]
        return docs[:top_k or self.top_k]


# ---- AzureSearchRetriever.search() ----------------------------------------------------------------

class FakeSearchClient:
    def __init__(self, results=None, error=None):
        self.calls, self.results, self.error = [], results or [], error

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return list(self.results)


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_text(self, text):
        self.calls.append(text)
        return [0.5] * 3


class TestRetrieverSearch(OfflineTestCase):
    def make(self, results=None, error=None):
        from src.retrieval.retriever import AzureSearchRetriever
        retriever = AzureSearchRetriever(top_k=5)
        retriever.search_client = FakeSearchClient(results, error)
        retriever.embedder = FakeEmbedder()
        return retriever

    def test_modes_shape_the_request(self):
        r = self.make()
        r.search("q", mode="hybrid")
        r.search("q", top_k=3, mode="vector")
        r.search("q", mode="keyword")
        hybrid, vector, keyword = r.search_client.calls
        self.assertEqual(set(hybrid), {"search_text", "vector_queries", "top", "select"})
        self.assertEqual((hybrid["search_text"], hybrid["top"]), ("q", 5))
        # Hybrid fuses the keyword results with 50 vector neighbours, not just top: with 5 of them, every
        # neighbour also matched a keyword and no keyword-only chunk could enter the top 5
        self.assertEqual(hybrid["vector_queries"][0].k_nearest_neighbors, 50)
        self.assertEqual(hybrid["vector_queries"][0].fields, "content_vector")
        self.assertIsNone(vector["search_text"])
        self.assertEqual((vector["top"], vector["vector_queries"][0].k_nearest_neighbors), (3, 3))
        self.assertEqual(set(keyword), {"search_text", "top", "select"})
        self.assertEqual(r.embedder.calls, ["q", "q"])  # keyword search needs no embedding
        self.assertEqual(hybrid["select"], ["id", "content", "source_file", "chunk_index", "metadata"])

    def test_filter_in_every_mode_and_a_given_vector(self):
        r = self.make()
        for mode in ("hybrid", "vector", "keyword"):
            r.search("q", mode=mode, filter_expr="file_type ne 'xlsx'", vector=[1.0, 2.0])
        self.assertTrue(all(call["filter"] == "file_type ne 'xlsx'" for call in r.search_client.calls))
        self.assertEqual(r.embedder.calls, [])
        self.assertEqual(r.search_client.calls[0]["vector_queries"][0].vector, [1.0, 2.0])

    def test_retrieve_and_retrieve_with_filter_keep_their_requests(self):
        r = self.make()
        r.retrieve("q")
        r.retrieve("q", top_k=2, use_hybrid=False)
        r.retrieve_with_filter("q", "company eq 'NVIDIA'")
        plain, vector_only, filtered = r.search_client.calls
        self.assertEqual(set(plain), {"search_text", "vector_queries", "top", "select"})
        self.assertEqual(plain["search_text"], "q")
        self.assertEqual((vector_only["search_text"], vector_only["top"]), (None, 2))
        self.assertEqual(set(filtered), {"search_text", "vector_queries", "top", "select", "filter"})
        self.assertEqual((filtered["search_text"], filtered["filter"]), ("q", "company eq 'NVIDIA'"))

    def test_workbook_expansion(self):
        def doc(i, source):
            return {"id": i, "content": i, "source_file": source, "chunk_index": 0, "metadata": "{}"}

        mostly_workbook = [doc("w1", "a.xlsx"), doc("w2", "a.xlsx"), doc("w3", "b.csv"), doc("p1", "x.pdf"),
                           doc("p2", "x.pdf")]
        r = self.make(mostly_workbook)
        self.assertEqual((r.workbook_expand, r.workbook_trigger), (20, 3))  # on by default
        r.workbook_expand = 0
        self.assertEqual([d["id"] for d in r.retrieve("q")], ["w1", "w2", "w3", "p1", "p2"])
        self.assertEqual(len(r.search_client.calls), 1)  # off

        r = self.make()
        r.workbook_expand, r.workbook_trigger = 3, 3
        more = [doc("w2", "a.xlsx"), doc("w4", "a.xlsx"), doc("w5", "a.xlsx"), doc("w6", "a.xlsx"),
                doc("w7", "a.xlsx")]
        r.search_client.search = lambda **kw: r.search_client.calls.append(kw) or list(
            mostly_workbook if len(r.search_client.calls) == 1 else more)
        ids = [d["id"] for d in r.retrieve("q")]
        self.assertEqual(ids, ["w1", "w2", "w3", "p1", "p2", "w4", "w5", "w6"])  # new ones only, at most 3
        first, second = r.search_client.calls
        self.assertNotIn("filter", first)
        self.assertEqual((second["filter"], second["top"]), ("file_type eq 'xlsx' or file_type eq 'csv'", 8))
        self.assertEqual(r.embedder.calls, ["q"])  # one embedding for both searches

        r = self.make([doc("w1", "a.xlsx"), doc("w2", "a.xlsx"), doc("p1", "x.pdf")])
        r.workbook_expand, r.workbook_trigger = 3, 3
        self.assertEqual(len(r.retrieve("q")), 3)
        self.assertEqual(len(r.search_client.calls), 1)  # two workbook chunks: no expansion

    def test_companies_in(self):
        from src.retrieval.retriever import companies_in
        self.assertEqual(companies_in("How did AMD's Data Center and Intel's DCAI compare?"), ["AMD", "INTC"])
        self.assertEqual(companies_in("Xbox growth, then NVIDIA's Blackwell"), ["MSFT", "NVDA"])  # in order named
        self.assertEqual(companies_in("Google Cloud, GOOGL and Alphabet"), ["GOOGL"])  # each company once
        self.assertEqual(companies_in("Micron's HBM and MU's guidance"), ["MU"])
        # Words that contain a name, and 'mu' in lower case, name no company
        self.assertEqual(companies_in("Artificial intelligence metadata in a mu-law codec"), [])
        self.assertEqual(companies_in("Which company had the highest revenue in calendar Q2 2026?"), [])
        self.assertEqual(companies_in(""), [])

    def test_company_routing(self):
        def doc(i, source):
            return {"id": i, "content": i, "source_file": source, "chunk_index": 0, "metadata": "{}"}

        by_filter = {
            "ticker eq 'AMD' or ticker eq null": [doc("amd1", "AMD.html"), doc("w1", "c.xlsx"), doc("amd2", "AMD.pdf")],
            "ticker eq 'INTC' or ticker eq null": [doc("intc1", "INTC.html"), doc("w1", "c.xlsx"),
                                                   doc("intc2", "INTC.html")],
        }
        r = self.make()
        r.search_client.search = lambda **kw: r.search_client.calls.append(kw) or list(
            by_filter.get(kw.get("filter"), [doc("other", "x.pdf")]))
        self.assertFalse(r.company_routing)  # off by default
        r.retrieve("AMD revenue?")
        self.assertNotIn("filter", r.search_client.calls[0])

        r.company_routing = True
        r.search_client.calls.clear()
        self.assertEqual([d["id"] for d in r.retrieve("AMD revenue?")], ["amd1", "w1", "amd2"])
        (call,) = r.search_client.calls
        self.assertEqual((call["filter"], call["top"]), ("ticker eq 'AMD' or ticker eq null", 5))

        # Two companies: a search each, merged rank by rank with each chunk once, and one embedding for both
        r.search_client.calls.clear()
        r.embedder.calls.clear()
        ids = [d["id"] for d in r.retrieve("How did AMD and Intel compare?")]
        self.assertEqual(ids, ["amd1", "intc1", "w1", "amd2", "intc2"])
        self.assertEqual([c["filter"] for c in r.search_client.calls],
                         ["ticker eq 'AMD' or ticker eq null", "ticker eq 'INTC' or ticker eq null"])
        self.assertEqual(r.embedder.calls, ["How did AMD and Intel compare?"])

        # Four companies: one unfiltered search, as without routing
        r.search_client.calls.clear()
        r.retrieve("Revenue of NVIDIA, AMD, Intel and Micron?")
        self.assertEqual(len(r.search_client.calls), 1)
        self.assertNotIn("filter", r.search_client.calls[0])

        # A caller's filter applies to every search, routed or not
        r.search_client.calls.clear()
        r.retrieve("AMD revenue?", filter_expr="file_type ne 'xlsx'")
        r.retrieve("Which company led?", filter_expr="file_type ne 'xlsx'")
        self.assertEqual([c["filter"] for c in r.search_client.calls],
                         ["(ticker eq 'AMD' or ticker eq null) and (file_type ne 'xlsx')", "file_type ne 'xlsx'"])

    def test_one_company_gets_more_results(self):
        def doc(i, source):
            return {"id": i, "content": i, "source_file": source, "chunk_index": 0, "metadata": "{}"}

        # The company's five best chunks, then three workbook rows
        ranked = [doc(f"p{i}", "AMD.pdf") for i in range(5)] + [doc(f"w{i}", "c.xlsx") for i in range(3)]
        r = self.make()
        r.company_routing = True
        r.search_client.search = lambda **kw: r.search_client.calls.append(kw) or ranked[:kw["top"]]
        self.assertEqual(r.single_company_top_k, 0)  # off by default
        self.assertEqual(len(r.retrieve("AMD revenue?")), 5)

        r.single_company_top_k = 10
        r.search_client.calls.clear()
        self.assertEqual(len(r.retrieve("AMD revenue?")), 8)
        r.retrieve("How did AMD and Intel compare?")
        # One company: 10 results; two: top_k each. The workbook rows sit below the top 5, so no expansion
        self.assertEqual([c["top"] for c in r.search_client.calls], [10, 5, 5])

    def test_company_routing_with_workbook_expansion(self):
        def doc(i, source):
            return {"id": i, "content": i, "source_file": source, "chunk_index": 0, "metadata": "{}"}

        routed = [doc("w1", "c.xlsx"), doc("w2", "c.xlsx"), doc("w3", "c.xlsx"), doc("p1", "AMD.pdf")]
        more = [doc("w2", "c.xlsx"), doc("w4", "c.xlsx")]
        r = self.make()
        r.company_routing = True
        r.workbook_expand, r.workbook_trigger = 3, 3
        r.search_client.search = lambda **kw: r.search_client.calls.append(kw) or list(
            routed if len(r.search_client.calls) == 1 else more)
        self.assertEqual([d["id"] for d in r.retrieve("AMD gross margin by quarter?")], ["w1", "w2", "w3", "p1", "w4"])
        _, expansion = r.search_client.calls
        # The extra workbook chunks come from the named company's workbooks and the comparison workbook
        self.assertEqual((expansion["filter"], expansion["top"]),
                         ("(file_type eq 'xlsx' or file_type eq 'csv') and (ticker eq 'AMD' or ticker eq null)", 8))
        self.assertEqual(r.embedder.calls, ["AMD gross margin by quarter?"])  # one embedding for both searches

    def test_results_are_processed_as_before(self):
        result = {"id": "c1", "content": "text", "source_file": "a.pdf", "chunk_index": 4, "@search.score": 0.03,
                  "metadata": json.dumps({"file_type": "pdf"})}
        docs = self.make([result]).search("q")
        self.assertEqual(docs, [{"id": "c1", "content": "text", "source_file": "a.pdf", "chunk_index": 4,
                                 "score": 0.03, "metadata": {"file_type": "pdf"}}])

    def test_bad_input_and_failures(self):
        from src.retrieval.retriever import RetrievalError
        r = self.make(error=RuntimeError("boom"))
        with self.assertRaises(ValueError):
            r.search("  ")
        with self.assertRaises(ValueError):
            r.search("q", mode="semantic")
        with self.assertRaises(RetrievalError) as caught:
            r.search("q")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        with self.assertRaises(RetrievalError):
            r.retrieve_with_filter("q", "x eq 1")


# ---- evaluation/evidence.py -----------------------------------------------------------------------

def unit(files, *patterns, fact=None):
    out = {"files": list(files), "all": list(patterns)}
    if fact:
        out["fact"] = fact
    return out


class TestEvidenceScores(OfflineTestCase):
    UNITS = [unit(["a.pdf"], "alpha"), unit(["b.pdf"], "beta")]
    CHUNKS = [chunk("c1", "a.pdf", "nothing here"), chunk("c2", "a.pdf", "alpha here"),
              chunk("c3", "b.pdf", "alpha and beta"), chunk("c4", "a.pdf", "ALPHA again"),
              chunk("c5", "c.pdf", "beta from another file")]

    def test_scores_at_k(self):
        s = evidence.evidence_scores(self.UNITS, self.CHUNKS, 5)
        self.assertEqual((s["hit"], s["recall"], s["complete"], s["first_rank"]), (1.0, 1.0, 1.0, 2))
        self.assertAlmostEqual(s["mrr"], 0.5)
        self.assertAlmostEqual(s["precision"], 3 / 5)
        ideal = 1 + 1 / math.log2(3)
        self.assertAlmostEqual(s["ndcg"], (1 / math.log2(3) + 1 / math.log2(4)) / ideal)
        self.assertEqual(s["missed"], [])

    def test_scores_at_1_and_3(self):
        s1 = evidence.evidence_scores(self.UNITS, self.CHUNKS, 1)
        self.assertEqual((s1["hit"], s1["recall"], s1["mrr"], s1["precision"], s1["first_rank"]), (0.0, 0.0, 0.0, 0.0, None))
        self.assertEqual(s1["missed"], [0, 1])
        s3 = evidence.evidence_scores(self.UNITS, self.CHUNKS, 3)
        self.assertEqual((s3["hit"], s3["complete"]), (1.0, 1.0))
        self.assertAlmostEqual(s3["precision"], 2 / 3)

    def test_files_restrict_and_text_is_normalized(self):
        u = unit(["NVDA_FY2027-Q2_cfo_commentary.pdf"], "data center", "117 ?%")
        text = evidence.normalize("Data\nCenter revenue up 117 %")
        self.assertTrue(evidence.supports(u, "NVDA_FY2027-Q2_cfo_commentary.pdf", text))
        self.assertFalse(evidence.supports(u, "NVDA_FY2027-Q2_transcript.pdf", text))
        self.assertEqual(evidence.normalize("$130–145 “B”"), '$130-145 "B"')

    def test_spreadsheet_only_units_leave_under_no_xlsx(self):
        units = [unit(["w.xlsx"], "gamma"), unit(["b.pdf"], "beta")]
        chunks = [chunk("x", "w.xlsx", "gamma"), chunk("y", "b.pdf", "nothing")]
        s = evidence.evidence_scores(units, chunks, 5, exclude_spreadsheets=True)
        self.assertEqual((s["units"], s["hit"], s["missed"]), (1, 0.0, [1]))  # missed indexes the item's own list
        self.assertIsNone(evidence.evidence_scores(units[:1], chunks, 5, exclude_spreadsheets=True))
        self.assertEqual(evidence.evidence_scores(units[:1], chunks, 5)["hit"], 1.0)

    def test_any_of_forms_and_needs(self):
        either = {"fact": "f", "any_of": [unit(["w.xlsx"], "Value is 98"), unit(["p.pdf"], r"98\.0 billion")]}
        self.assertTrue(evidence.supports(either, "p.pdf", "a gain of 98.0 billion"))
        self.assertTrue(evidence.supports(either, "w.xlsx", "Value is 98"))
        self.assertFalse(evidence.supports(either, "w.xlsx", "a gain of 98.0 billion"))
        self.assertEqual(evidence.unit_files(either), ["w.xlsx", "p.pdf"])
        self.assertFalse(evidence.spreadsheet_only(either))
        xlsx, docs = unit(["w.xlsx"], "a"), unit(["p.pdf"], "b")
        self.assertEqual([evidence.needs(u) for u in ([xlsx], [docs], [xlsx, docs], [either], [])],
                         ["xlsx", "docs", "both", "docs", "none"])
        self.assertEqual([evidence.names_spreadsheet(u) for u in (xlsx, docs, either)], [True, False, True])

    def test_chunks_labels_against_the_top_k(self):
        self.assertEqual([evidence.chunks_label(n, 5) for n in (1, 2, 5, 6, 24)],
                         ["1 chunk", "2–5 chunks", "2–5 chunks", "more than 5 chunks", "more than 5 chunks"])
        self.assertEqual(evidence.chunks_order(5), ["1 chunk", "2–5 chunks", "more than 5 chunks"])
        self.assertEqual(evidence.chunks_order(2), ["1 chunk", "2 chunks", "more than 2 chunks"])
        self.assertEqual(evidence.chunks_order(1), ["1 chunk", "more than 1 chunk"])


# ---- evaluation/snapshot.py -----------------------------------------------------------------------

class FakePaged(list):
    def __init__(self, items, facets=None, count=None):
        super().__init__(items)
        self._facets, self._count = facets, count

    def get_facets(self):
        return self._facets

    def get_count(self):
        return self._count


class FakeIndexClient:
    """Answers the snapshot's facet query and its per-file, chunk-ordered queries."""

    def __init__(self, docs, report_count=None):
        self.docs, self.calls = docs, []
        self.report_count = len(docs) if report_count is None else report_count

    def search(self, search_text=None, facets=None, top=None, include_total_count=False, filter=None,
               order_by=None, select=None, skip=0):
        self.calls.append({"facets": facets, "top": top, "filter": filter, "order_by": order_by, "skip": skip})
        if facets:
            counts = {}
            for doc in self.docs:
                counts[doc["source_file"]] = counts.get(doc["source_file"], 0) + 1
            return FakePaged([], {"source_file": [{"value": f, "count": n} for f, n in counts.items()]},
                             self.report_count)
        name = filter.split("eq ", 1)[1].strip("'").replace("''", "'")
        rows = sorted((d for d in self.docs if d["source_file"] == name), key=lambda d: d["chunk_index"])
        return FakePaged(rows[skip:skip + top])


def index_doc(chunk_id, source_file, chunk_index, content, file_type=None, metadata=None):
    return {"id": chunk_id, "source_file": source_file, "chunk_index": chunk_index, "content": content,
            "file_type": file_type, "metadata": json.dumps(metadata or {})}


class TestSnapshot(OfflineTestCase):
    DOCS = [index_doc("b_1", "b.html", 1, "two"), index_doc("b_0", "b.html", 0, "one", file_type="html"),
            index_doc("a_0", "a.xlsx", 0, "row", metadata={"file_type": "xlsx"}), index_doc("c_0", "c.pdf", 0, "pdf")]

    def test_export_and_load(self):
        retriever = type("R", (), {"search_client": FakeIndexClient(self.DOCS), "index_name": "idx"})()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.jsonl"
            meta = snapshot.export_snapshot(retriever, path)
            chunks = snapshot.load_snapshot(path)
            self.assertEqual(snapshot.load_snapshot_meta(path)["chunks"], 4)
        self.assertEqual([c["chunk_id"] for c in chunks], ["a_0", "b_0", "b_1", "c_0"])  # file, then chunk order
        self.assertEqual([c["file_type"] for c in chunks], ["xlsx", "html", "html", "pdf"])  # field, JSON, suffix
        self.assertEqual((meta["chunks"], meta["files"]), (4, {"a.xlsx": 1, "b.html": 2, "c.pdf": 1}))
        self.assertEqual(meta["sha256"], snapshot.snapshot_sha256(chunks))
        self.assertEqual(meta["index_name"], "idx")

    def test_paging_within_a_file(self):
        docs = [index_doc(f"p_{i}", "p.pdf", i, f"chunk {i}") for i in range(5)]
        client = FakeIndexClient(docs)
        chunks, total = snapshot.fetch_chunks(client, page_size=2)
        self.assertEqual(([c["chunk_index"] for c in chunks], total), ([0, 1, 2, 3, 4], 5))
        self.assertEqual([call["skip"] for call in client.calls[1:]], [0, 2, 4])

    def test_a_count_mismatch_stops(self):
        retriever = type("R", (), {"search_client": FakeIndexClient(self.DOCS, report_count=9), "index_name": "i"})()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            snapshot.export_snapshot(retriever, Path(tmp) / "s.jsonl")

    def test_missing_snapshot_says_how_to_make_one(self):
        with self.assertRaises(FileNotFoundError) as caught:
            snapshot.load_snapshot("/nonexistent/snap.jsonl")
        self.assertIn("eval_retrieval.py snapshot", str(caught.exception))

    def test_evidence_chunks_greedy_cover(self):
        units = [unit(["a.pdf", "b.pdf"], "alpha"), unit(["a.pdf", "b.pdf"], "beta"), unit(["c.pdf"], "gamma")]
        chunks = [chunk("b1", "b.pdf", "alpha", 1), chunk("a5", "a.pdf", "alpha", 5), chunk("a3", "a.pdf", "alpha", 3),
                  chunk("b2", "b.pdf", "alpha beta", 2), chunk("c0", "c.pdf", "gamma", 0)]
        picks = snapshot.evidence_chunks(units, chunks, 5)
        self.assertEqual([c["chunk_id"] for c in picks], ["b2", "c0"])  # the two-fact chunk first
        self.assertEqual([c["chunk_id"] for c in snapshot.evidence_chunks(units[:1], chunks, 5)], ["a3"])  # file order, index
        self.assertEqual(len(snapshot.evidence_chunks(units, chunks, 1)), 1)
        self.assertEqual(snapshot.evidence_chunks([unit(["z.pdf"], "zeta")], chunks, 5), [])

    def test_fewest_chunks_is_an_exact_cover(self):
        units = [unit(["w.xlsx"], rf"\bu{n}\b") for n in range(6)]
        chunks = [chunk("wide", "w.xlsx", "u0 u1 u2 u3", 0), chunk("left", "w.xlsx", "u0 u1 u4", 1),
                  chunk("right", "w.xlsx", "u2 u3 u5", 2), chunk("other", "p.pdf", "u4 u5", 3)]
        # Greedy takes the four-row chunk first and then needs two more; two chunks hold every row
        self.assertEqual([c["chunk_id"] for c in snapshot.evidence_chunks(units, chunks, 30)], ["wide", "left", "right"])
        self.assertEqual(snapshot.fewest_chunks(units, chunks), 2)
        self.assertEqual(snapshot.fewest_chunks(units[:4], chunks), 1)
        self.assertIsNone(snapshot.fewest_chunks(units + [unit(["w.xlsx"], r"\bu9\b")], chunks))  # u9 is nowhere
        self.assertEqual(snapshot.fewest_chunks([], chunks), 0)

    def test_check_gold_compares_vertical_labels_with_the_index(self):
        def item(qid, label, *patterns):
            return {"id": qid, "question": "q", "kind": "superlative", "expect": "answer", "route": ["graph"],
                    "vertical": {"op": "max_min", "sheet": "s", "chunks": label},
                    "evidence": [unit(["w.xlsx"], p) for p in patterns]}
        chunks = [chunk("r1", "w.xlsx", "row one", 1), chunk("r2", "w.xlsx", "row two", 2)]
        items = [item(70, 2, "one", "two"), item(71, 1, "one", "two"), item(72, 2, "one", "three"),
                 {**item(73, 1, "one"), "vertical": None}]
        report = snapshot.check_gold(items, chunks)
        self.assertEqual(report["vertical"], [{"id": 70, "label": 2, "fewest": 2, "ok": True},
                                              {"id": 71, "label": 1, "fewest": 2, "ok": False},
                                              {"id": 72, "label": 2, "fewest": None, "ok": False}])

    def test_check_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = load_gold(write_gold(Path(tmp)))
        index = INDEX + [chunk("nvda_tr_9", "NVDA_FY2027-Q2_transcript.pdf",
                               "Revenue in the third quarter of fiscal 2027 revenue was not given.", 9)]
        report = snapshot.check_gold(items, index)
        status = {(row["id"], row["turn"], row["unit"]): row["status"] for row in report["units"]}
        self.assertEqual(status[(1, 1, 0)], "ok")
        self.assertEqual(status[(5, 2, 0)], "broken")  # no Reality Labs chunk in this index
        row = next(r for r in report["units"] if (r["id"], r["unit"]) == (1, 0))
        self.assertEqual((row["support"], row["chunks"]), (2, ["nvda_cfo_2", "nvda_tr_4"]))
        self.assertEqual([hit["id"] for hit in report["absent"]], [4])
        self.assertEqual(report["missing_files"], ["GOOGL_FY2026-Q2_transcript.pdf"])


# ---- evaluation/retrieval.py ----------------------------------------------------------------------

class TestRetrievalRun(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.items = load_gold(write_gold(Path(self.tmp.name)))
        self.queries = retrieval_queries(self.items)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_queries_are_answer_items_first_turns(self):
        self.assertEqual([(q["id"], q["turn"]) for q in self.queries], [(1, 1), (2, 1), (3, 1), (5, 1)])
        self.assertEqual(self.queries[3]["question"], "What was Meta's Family of Apps revenue in Q2 2026?")

    def test_every_mode_and_condition_one_embedding_per_question(self):
        fake, seen, sleeps = FakeRetriever(), [], []
        records = retrieval.run_queries(fake, self.queries, ("hybrid", "keyword"), ("full", "no_xlsx"), k=5,
                                        delay=0.1, on_record=seen.append, sleep=sleeps.append)
        self.assertEqual(len(records), 4 * 2 * 2)
        self.assertEqual(seen, records)
        self.assertEqual(len(fake.embeddings), 4)
        self.assertEqual(len(sleeps), len(records) - 1)
        first = [(s["mode"], s["filter"], s["vector"] is not None) for s in fake.searches[:4]]
        self.assertEqual(first, [("hybrid", None, True), ("hybrid", NO_XLSX_FILTER, True),
                                 ("keyword", None, False), ("keyword", NO_XLSX_FILTER, False)])
        self.assertEqual(records[0]["results"][0]["file_type"], "pdf")
        self.assertTrue(all(r["error"] is None for r in records))

    def test_a_failed_search_is_recorded_and_the_run_goes_on(self):
        q = self.queries[0]["question"]
        fake = FakeRetriever(fail={(q, "hybrid", None): RuntimeError("timeout")})
        records = retrieval.run_queries(fake, self.queries, ("hybrid",), ("full",), k=5)
        self.assertEqual(records[0]["error"]["type"], "RuntimeError")
        self.assertEqual(len(records), 4)

    def test_a_rejected_filter_stops_the_run(self):
        class HttpResponseError(Exception):
            status_code = 400
        HttpResponseError.__module__ = "azure.core.exceptions"
        wrapped = RuntimeError("Retrieval failed")
        wrapped.__cause__ = HttpResponseError("Invalid expression: file_type")
        q = self.queries[0]["question"]
        fake, seen = FakeRetriever(fail={(q, "hybrid", NO_XLSX_FILTER): wrapped}), []
        with self.assertRaises(retrieval.FilterRejected) as caught:
            retrieval.run_queries(fake, self.queries, ("hybrid",), ("full", "no_xlsx"), k=5, on_record=seen.append)
        self.assertEqual(caught.exception.record["error"]["type"], "filter_rejected")
        self.assertEqual([r["condition"] for r in seen], ["full", "no_xlsx"])

    def test_a_leaking_filter_is_flagged_and_not_scored(self):
        records = retrieval.run_queries(FakeRetriever(leak=True), self.queries[1:2], ("hybrid",), ("no_xlsx",), k=5)
        self.assertEqual(records[0]["error"]["type"], "filter_leak")
        lines, _ = retrieval.score_records(records, {i["id"]: i for i in self.items})
        self.assertIsNone(lines[0]["scores"])
        summary = retrieval.build_summary(lines, {"k": 5, "modes": ["hybrid"], "conditions": ["no_xlsx"]})
        self.assertIn("filter leaked", retrieval.render_markdown(summary))

    def test_scores_and_summary(self):
        ranking = {
            self.queries[0]["question"]: ["amd_pr_0", "nvda_tr_4", "nvda_cfo_2", "wb_148", "nvda_cfo_0"],
            self.queries[1]["question"]: ["wb_148", "amd_pr_0", "nvda_cfo_0", "googl_pr_3", "wb_30"],
            self.queries[2]["question"]: ["googl_pr_3", "amd_pr_0", "nvda_cfo_0", "nvda_tr_4", "meta_pr_1", "wb_30"],
            self.queries[3]["question"]: ["meta_pr_1"],
        }
        records = retrieval.run_queries(FakeRetriever(ranking), self.queries, ("hybrid",), ("full", "no_xlsx"), k=5)
        lines, notes = retrieval.score_records(records, {i["id"]: i for i in self.items})
        self.assertEqual(notes, [])
        by = {(line["id"], line["condition"]): line for line in lines}
        q1 = by[(1, "full")]
        self.assertEqual((q1["scores"]["hit_at_1"], q1["scores"]["hit_at_3"], q1["first_rank"]), (0.0, 1.0, 2))
        self.assertEqual((q1["scores"]["recall"], q1["scores"]["complete"], q1["needs"]), (1.0, 1.0, "docs"))
        self.assertAlmostEqual(q1["scores"]["precision"], 2 / 5)
        q3 = by[(3, "full")]  # the workbook row is at rank 6: outside the top 5
        self.assertEqual((q3["scores"]["recall"], q3["missed"], q3["needs"]), (0.5, ["workbook: Alphabet net income"], "both"))
        self.assertTrue(by[(2, "no_xlsx")]["na"])  # only a workbook holds it
        q3n = by[(3, "no_xlsx")]
        self.assertEqual((q3n["units"], q3n["scores"]["complete"], q3n["scores"]["right_file"]), (1, 1.0, 1.0))

        run_config = {"run_id": "t", "k": 5, "modes": ["hybrid"], "conditions": ["full", "no_xlsx"],
                      "index_name": "test-index"}
        summary = retrieval.build_summary(lines, run_config)
        card = summary["scorecards"]["hybrid · full"]
        self.assertEqual(card["hit"]["all"], {"value": 1.0, "n": 4, "of": 4, "failed": 0, "leaked": 0})
        self.assertEqual(card["complete"]["all"]["value"], 0.75)
        self.assertEqual(card["hit"]["xlsx"], {"value": 1.0, "n": 1, "of": 1, "failed": 0, "leaked": 0})
        self.assertEqual(summary["scorecards"]["hybrid · no_xlsx"]["hit"]["xlsx"],
                         {"value": None, "n": 0, "of": 1, "failed": 0, "leaked": 0})
        self.assertEqual([row["id"] for row in summary["worst"]], [3])
        q2 = next(row for row in summary["per_question"] if row["id"] == 2)
        self.assertEqual(q2["ranks"], {"hybrid · full": "1", "hybrid · no_xlsx": "n/a"})
        # Q3 needs two files (the workbook's net income and the press release's gain) and missed the workbook's;
        # under no_xlsx the workbook's fact is out, so it needs one file and requires none
        self.assertEqual((q3["files_needed"], q3["per_file"]), (2, {"ai_earnings_comparison.xlsx": {"found": 0.0, "seen": 0.0}}))
        self.assertEqual((q3n["files_needed"], q3n["required_files"], q3n["per_file"]), (1, [], {}))
        # The breakdowns cover single-turn questions: Q5, a follow-up, is left out
        self.assertEqual(list(summary["by_difficulty"]), ["easy", "medium"])
        self.assertEqual({key: row["n"] for key, row in summary["by_files"].items()}, {"1 file": 2, "2 files": 1})
        self.assertEqual(summary["by_files"]["2 files"]["complete"]["value"], 0.0)
        self.assertEqual(sorted(summary["per_file"]), ["NVDA_FY2027-Q2_cfo_commentary.pdf", "ai_earnings_comparison.xlsx"])
        self.assertEqual(summary["per_file"]["ai_earnings_comparison.xlsx"],
                         {"n": 2, "found": {"value": 0.5, "n": 2, "of": 2, "failed": 0, "leaked": 0},
                          "seen": {"value": 0.5, "n": 2, "of": 2, "failed": 0, "leaked": 0}})
        markdown = retrieval.render_markdown(summary)
        for text in ("# Retrieval scorecard", "## Overview (all questions)", "## hybrid · no_xlsx", "complete@5",
                     "| 2 | Which company had the highest revenue in calendar Q2 2026? | superlative | medium | 1 | xlsx "
                     "| – | 1 | n/a |",
                     "workbook: Alphabet net income", "## By difficulty (hybrid · full)", "## By files needed",
                     "| ai_earnings_comparison.xlsx | 2 | 0.50 | 0.50 |", "## By kind (hybrid · full)", "## Caveats"):
            self.assertIn(text, markdown)

    def test_per_file_found_and_seen_and_failures(self):
        item = {"id": 7, "kind": "compare", "expect": "answer", "difficulty": "hard", "route": ["graph"],
                "companies": ["Broadcom", "NVIDIA"], "question": "q",
                "evidence": [{"fact": "AI revenue", "files": ["AVGO_FY2026-Q3_press_release.pdf"], "all": ["AI semi"]},
                             {"fact": "networking", "files": ["NVDA_FY2027-Q2_transcript.pdf"], "all": ["networking"]},
                             {"fact": "either", "files": ["NVDA_FY2027-Q2_cfo_commentary.pdf", "AMD_FY2026-Q2_press_release.html"],
                              "all": ["inventory"]}]}
        record = {"id": 7, "turn": 1, "mode": "hybrid", "condition": "full", "question": "q", "k": 5, "results": [
            chunk("a", "AVGO_FY2026-Q3_press_release.pdf", "AI semi revenue"),
            chunk("b", "NVDA_FY2027-Q2_transcript.pdf", "gaming revenue"),  # the file, but not its fact
            chunk("c", "NVDA_FY2027-Q2_cfo_commentary.pdf", "inventory rose")]}
        line = retrieval.score_record(record, item)
        self.assertEqual(line["files_needed"], 3)
        self.assertEqual(line["per_file"], {"AVGO_FY2026-Q3_press_release.pdf": {"found": 1.0, "seen": 1.0},
                                            "NVDA_FY2027-Q2_transcript.pdf": {"found": 0.0, "seen": 1.0}})
        failed = retrieval.score_record({**record, "id": 10, "results": [], "error": {"type": "RuntimeError", "message": "x"}},
                                        {**item, "id": 10})
        hard = {**item, "id": 8, "difficulty": "medium"}
        easy = {**item, "id": 9, "difficulty": "easy", "evidence": item["evidence"][:1]}
        lines = [line, failed, retrieval.score_record({**record, "id": 8}, hard),
                 retrieval.score_record({**record, "id": 9}, easy)]
        summary = retrieval.build_summary(lines, {"k": 5, "modes": ["hybrid"], "conditions": ["full"]})
        self.assertEqual(list(summary["by_difficulty"]), ["easy", "medium", "hard"])  # rubric order, not alphabetical
        self.assertEqual(list(summary["by_files"]), ["1 file", "3+ files"])
        nvda = summary["per_file"]["NVDA_FY2027-Q2_transcript.pdf"]
        # Q7 and Q8 searched (the file seen, its fact not found); Q10's search failed and still counts
        self.assertEqual((nvda["n"], nvda["found"]["value"], nvda["found"]["failed"], nvda["seen"]["value"]), (3, 0.0, 1, 1.0))
        self.assertIn("| NVDA_FY2027-Q2_transcript.pdf | 3 | 0.00 (n=2; 1 failed) | 1.00 (n=2; 1 failed) |",
                      retrieval.render_markdown(summary))

    def test_vertical_breakdowns_and_column(self):
        def item(qid, op, chunks, rows, kind="superlative", difficulty="medium"):
            return {"id": qid, "question": f"vertical {qid}", "kind": kind, "expect": "answer", "difficulty": difficulty,
                    "route": ["graph"], "companies": ["Meta"], "vertical": {"op": op, "sheet": "s", "chunks": chunks},
                    "evidence": [unit(["ai_earnings_comparison.xlsx"], rf"\bC{n} is {n}\b", fact=f"row {n}")
                                 for n in range(1, rows + 1)]}

        def row(n):
            return chunk(f"row_{n}", "ai_earnings_comparison.xlsx", f"Sheet - Company 'C{n}': C{n} is {n}.", n)
        items = {60: item(60, "max_min", 2, 2), 61: item(61, "count", 8, 8, "aggregate", "hard"),
                 62: item(62, "list", 1, 2, "lookup", "easy"), **{i["id"]: i for i in self.items}}
        searches = {60: [row(1), row(2)], 61: [row(n) for n in range(1, 6)],  # five of its eight rows
                    62: [chunk("both", "ai_earnings_comparison.xlsx", "C1 is 1. C2 is 2.")],
                    2: [INDEX[3]], 1: [INDEX[1]]}
        lines = [retrieval.score_record({"id": qid, "turn": 1, "mode": "hybrid", "condition": "full",
                                         "question": items[qid]["question"], "k": 5, "results": results},
                                        items[qid]) for qid, results in searches.items()]
        self.assertEqual((lines[0]["vertical"], lines[3]["vertical"]), ({"op": "max_min", "chunks": 2}, None))
        config = {"k": 5, "modes": ["hybrid"], "conditions": ["full"]}
        summary = retrieval.build_summary(lines, config)
        card = summary["scorecards"]["hybrid · full"]
        self.assertEqual(card["complete"]["vertical"], {"value": 0.667, "n": 3, "of": 3, "failed": 0, "leaked": 0})
        self.assertEqual(card["recall"]["vertical"]["value"], round((1 + 5 / 8 + 1) / 3, 3))
        self.assertEqual(card["hit"]["xlsx"]["of"], 4)  # the vertical questions also count where their facts live
        self.assertEqual({key: r["n"] for key, r in summary["by_shape"].items()}, {"vertical": 3, "row": 1})
        self.assertEqual(list(summary["by_shape"]), ["vertical", "row"])  # Q1 needs no workbook: in neither
        chunks = summary["by_vertical_chunks"]
        self.assertEqual(list(chunks), ["1 chunk", "2–5 chunks", "more than 5 chunks"])
        self.assertEqual((chunks["more than 5 chunks"]["recall"]["value"], chunks["more than 5 chunks"]["complete"]["value"]),
                         (0.625, 0.0))
        self.assertEqual(list(summary["by_vertical_op"]), ["list", "max_min", "count"])
        rows = {r["id"]: r for r in summary["per_question"]}
        self.assertEqual((rows[61]["vertical"], rows[2]["vertical"]), ("count · 8 chunks", None))
        # The run's k sets the groups: a top 10 holds eight chunks
        wide = retrieval.build_summary(lines, {**config, "k": 10})
        self.assertEqual({key: r["n"] for key, r in wide["by_vertical_chunks"].items()}, {"1 chunk": 1, "2–10 chunks": 2})
        markdown = retrieval.render_markdown(summary)
        for text in ("## Vertical questions (hybrid · full)", "### Vertical vs row", "### By chunks needed",
                     "### By operation", "| more than 5 chunks | 1 | 1.00 | 0.62 | 0.00 |", "| Cross-source | Vertical |",
                     "| xlsx | count · 8 chunks |", "can be complete at 5 only through column views"):
            self.assertIn(text, markdown)
        self.assertNotIn("## Vertical questions", retrieval.render_markdown(retrieval.build_summary(lines[3:], config)))
        # Q61 needs 8 chunks: it can't be complete at 5, so it caps the columns and leaves Incomplete evidence
        self.assertEqual(summary["beyond_k"], 1)
        self.assertEqual((summary["complete_ceiling"]["vertical"], summary["complete_ceiling"]["all"]), (0.667, 0.8))
        self.assertNotIn(61, [row["id"] for row in summary["worst"]])
        self.assertIn("complete@5 can then reach at most 0.80 for All, 0.75 for Needs a workbook, 0.67 for Vertical", markdown)
        self.assertEqual(wide["beyond_k"], 0)
        # Under no_xlsx a vertical question with workbook-only rows is n/a, not scored on its few document rows
        ablated = retrieval.score_record({"id": 61, "turn": 1, "mode": "hybrid", "condition": "no_xlsx",
                                          "question": items[61]["question"], "k": 5, "results": []}, items[61])
        self.assertEqual((ablated["na"], ablated["scores"]), (True, None))

    def test_rescoring_skips_questions_no_longer_in_the_gold(self):
        records = retrieval.run_queries(FakeRetriever(), self.queries[:1], ("hybrid",), ("full",), k=5)
        lines, notes = retrieval.score_records(records, {})
        self.assertEqual((lines, len(notes)), ([], 1))


# ---- scripts/eval_retrieval.py --------------------------------------------------------------------

def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCli(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.gold = write_gold(self.dir)
        self.script = load_script("eval_retrieval")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.script.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_dry_run_sends_nothing(self):
        fake = FakeRetriever()
        with mock.patch.object(self.script, "make_retriever", lambda k=None: fake):
            status, out, _ = self.run_cli("run", "--dry-run", "--set", str(self.gold), "--modes", "hybrid,keyword",
                                          "--conditions", "full,no_xlsx")
        self.assertEqual(status, 0)
        self.assertIn("nothing is sent to Azure", out)
        self.assertIn("4 embedding calls and 16 searches", out)
        self.assertEqual(fake.searches, [])

    def test_run_then_score_again(self):
        out_dir = self.dir / "run"
        with mock.patch.object(self.script, "make_retriever", lambda k=None: FakeRetriever()):
            status, out, _ = self.run_cli("run", "--set", str(self.gold), "--out", str(out_dir), "--delay", "0",
                                          "--conditions", "full,no_xlsx")
        self.assertEqual(status, 0, out)
        for name in ("run_config.json", "results.jsonl", "scores.jsonl", "summary.json", "summary.md"):
            self.assertTrue((out_dir / name).exists(), name)
        config = json.loads((out_dir / "run_config.json").read_text())
        self.assertEqual((config["records"], config["stopped"], config["k"]), (8, None, 5))
        self.assertIn("Retrieval scorecard", (out_dir / "summary.md").read_text())
        (out_dir / "summary.md").unlink()
        status, out, _ = self.run_cli("score", "--run", str(out_dir))
        self.assertEqual(status, 0)
        self.assertTrue((out_dir / "summary.md").exists())
        self.assertIn("hybrid · full: hit@5", out)

    def test_bad_modes_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("run", "--set", str(self.gold), "--modes", "semantic", "--dry-run")

    def test_snapshot_and_check_gold(self):
        docs = [index_doc(c["chunk_id"], c["source_file"], c["chunk_index"], c["content"], c["file_type"])
                for c in INDEX]
        fake = type("R", (), {"search_client": FakeIndexClient(docs), "index_name": "test-index"})()
        snap = self.dir / "snap.jsonl"
        with mock.patch.object(self.script, "make_retriever", lambda k=None: fake):
            status, out, _ = self.run_cli("snapshot", "--out", str(snap))
        self.assertEqual(status, 0)
        self.assertIn(f"Saved {len(INDEX)} chunks", out)
        status, out, _ = self.run_cli("check-gold", "--snapshot", str(snap), "--set", str(self.gold))
        self.assertEqual(status, 1)  # Reality Labs has no chunk, and the GOOGL transcript isn't indexed
        self.assertIn("Q5 t2 evidence[0] broken", out)
        self.assertIn("GOOGL_FY2026-Q2_transcript.pdf is named by the golden set", out)
        with self.assertRaises(SystemExit):
            self.run_cli("check-gold", "--snapshot", str(self.dir / "missing.jsonl"), "--set", str(self.gold))

    def test_check_gold_flags_a_vertical_chunks_label_the_index_doesnt_match(self):
        snap = self.dir / "snap.jsonl"
        rows = [chunk("gm_1", "ai_earnings_comparison.xlsx", "Gross margin — Company 'NVIDIA': CY2024 Q3 is 74.6; Units is %.", 1),
                chunk("gm_2", "ai_earnings_comparison.xlsx", "Gross margin — Company 'Meta': CY2024 Q3 is 81.8; Units is %.", 2)]
        snap.write_text("".join(json.dumps(r) + "\n" for r in rows))
        for label, expected in ((1, 1), (2, 0)):
            gold = write_gold(self.dir, VERTICAL_YAML.replace("chunks: 2", f"chunks: {label}"))
            status, out, _ = self.run_cli("check-gold", "--snapshot", str(snap), "--set", str(gold), "--verbose")
            self.assertEqual(status, expected, out)
            if label == 1:
                self.assertIn("! Q7 vertical chunks: labelled 1, but the fewest chunks holding its evidence is 2", out)
                self.assertIn("1 of 1 vertical question(s) with a chunks label the index doesn't match", out)
            else:
                self.assertIn("Q7 vertical chunks: 2, as labelled", out)
        gold = write_gold(self.dir, VERTICAL_YAML.replace("81\\.8;", "81\\.9;"))  # a row no chunk holds
        status, out, _ = self.run_cli("check-gold", "--snapshot", str(snap), "--set", str(gold))
        self.assertEqual(status, 1)
        self.assertIn("! Q7 vertical chunks: labelled 2, but no chunk holds some of its evidence", out)

    def test_never_writes_into_datasets(self):
        with self.assertRaises(SystemExit):
            self.run_cli("run", "--set", str(self.gold), "--out", str(ROOT / "datasets" / "x"))

    def test_output_must_stay_out_of_git(self):
        """The snapshot and the runs quote the indexed documents: inside the repo only git-ignored paths."""
        fake = FakeRetriever()
        with mock.patch.object(self.script, "make_retriever", lambda k=None: fake):
            for argv in (("run", "--set", str(self.gold), "--out", str(ROOT / "evaluation" / "run-x")),
                         ("snapshot", "--out", str(ROOT / "docs" / "snap.jsonl"))):
                with self.assertRaises(SystemExit, msg=argv[0]):
                    self.run_cli(*argv)
        self.assertEqual(fake.searches, [])
        self.assertFalse((ROOT / "evaluation" / "run-x").exists())
        import argparse
        logs_path = ROOT / "logs" / "eval" / "retrieval" / "not-created"
        self.assertEqual(self.script.safe_output(argparse.ArgumentParser(), logs_path), logs_path)  # nothing written
        self.assertFalse(logs_path.exists())

    def test_k_zero_and_a_reused_out_folder_are_usage_errors(self):
        with mock.patch.object(self.script, "make_retriever", lambda k=None: FakeRetriever()):
            with self.assertRaises(SystemExit):
                self.run_cli("run", "--set", str(self.gold), "--k", "0", "--dry-run")
            out_dir = self.dir / "existing"
            out_dir.mkdir()
            with self.assertRaises(SystemExit):
                self.run_cli("run", "--set", str(self.gold), "--out", str(out_dir), "--delay", "0")

    def test_failed_searches_exit_1_and_show_as_failed(self):
        fail = {(q["question"], "hybrid", None): RuntimeError("timeout")
                for q in retrieval_queries(load_gold(self.gold))[:1]}
        out_dir = self.dir / "run"
        with mock.patch.object(self.script, "make_retriever", lambda k=None: FakeRetriever(fail=fail)):
            status, out, err = self.run_cli("run", "--set", str(self.gold), "--out", str(out_dir), "--delay", "0")
        self.assertEqual(status, 1, out + err)
        summary = json.loads((out_dir / "summary.json").read_text())
        self.assertEqual(summary["scorecards"]["hybrid · full"]["hit"]["all"]["failed"], 1)
        markdown = (out_dir / "summary.md").read_text()
        self.assertIn("(n=3; 1 failed)", markdown)
        self.assertIn("1 search(es) failed", markdown)

    def test_host_names_never_reach_the_records(self):
        message = ("HTTPSConnectionPool(host='mysvc.search.windows.net', port=443): Max retries exceeded "
                   "(Caused by NameResolutionError(\"<urllib3.connection.HTTPSConnection object>: Failed to "
                   "resolve 'mysvc.search.windows.net' ([Errno -2] Name or service not known)\"))")
        fail = {(q["question"], "hybrid", None): RuntimeError(message)
                for q in retrieval_queries(load_gold(self.gold))}
        out_dir = self.dir / "run"
        with mock.patch.object(self.script, "make_retriever", lambda k=None: FakeRetriever(fail=fail)):
            self.run_cli("run", "--set", str(self.gold), "--out", str(out_dir), "--delay", "0")
        for name in ("results.jsonl", "summary.md", "summary.json", "scores.jsonl"):
            self.assertNotIn(".search.windows.net", (out_dir / name).read_text(), name)

    def test_rescore_with_a_changed_golden_set(self):
        out_dir = self.dir / "run"
        with mock.patch.object(self.script, "make_retriever", lambda k=None: FakeRetriever()):
            self.run_cli("run", "--set", str(self.gold), "--out", str(out_dir), "--delay", "0")
        reworded = self.gold.read_text().replace("What was NVIDIA's Data Center revenue in its fiscal Q2 2027?",
                                                 "How much Data Center revenue did NVIDIA report in fiscal Q2 2027?")
        self.gold.write_text(reworded)
        status, _, err = self.run_cli("score", "--run", str(out_dir), "--set", str(self.gold))
        self.assertEqual(status, 0)
        self.assertIn("differs from the one this run searched with", err)
        summary = json.loads((out_dir / "summary.json").read_text())
        self.assertTrue(summary["gold_changed"])
        self.assertNotEqual(summary["scored_with"]["sha256"], summary["run"]["gold_file"]["sha256"])
        self.assertNotIn(1, [row["id"] for row in summary["per_question"]])  # its old search isn't scored
        markdown = (out_dir / "summary.md").read_text()
        self.assertIn("The golden set changed since this run", markdown)
        self.assertIn("Q1 skipped (1 search): its question changed since this run", markdown)

    def test_snapshot_named_json_and_a_summary_passed_as_snapshot(self):
        docs = [index_doc(c["chunk_id"], c["source_file"], c["chunk_index"], c["content"], c["file_type"])
                for c in INDEX]
        fake = type("R", (), {"search_client": FakeIndexClient(docs), "index_name": "test-index"})()
        snap = self.dir / "snap.json"
        with mock.patch.object(self.script, "make_retriever", lambda k=None: fake):
            status, _, _ = self.run_cli("snapshot", "--out", str(snap))
        self.assertEqual(status, 0)
        self.assertEqual(len(snapshot.load_snapshot(snap)), len(INDEX))  # the summary didn't overwrite it
        self.assertEqual(snapshot.meta_path(snap), self.dir / "snap.json.meta.json")
        self.assertEqual(snapshot.load_snapshot_meta(snap)["chunks"], len(INDEX))
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.load_snapshot(snapshot.meta_path(snap))
        with self.assertRaises(SystemExit):
            self.run_cli("check-gold", "--snapshot", str(snapshot.meta_path(snap)), "--set", str(self.gold))


class TestReviewFixes(OfflineTestCase):
    """Filter-rejection detection, 'not run' cells, the capped worst list and evidence_chunks' tie-break."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.items = load_gold(write_gold(Path(self.tmp.name)))
        self.queries = retrieval_queries(self.items)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    @staticmethod
    def azure_400(text="Invalid expression: file_type"):
        class HttpResponseError(Exception):
            status_code = 400
        HttpResponseError.__module__ = "azure.core.exceptions"
        wrapped = RuntimeError("Retrieval failed")
        wrapped.__cause__ = HttpResponseError(text)
        return wrapped

    def test_keyword_search_still_detects_a_rejected_filter_when_the_embedding_failed(self):
        q = self.queries[0]["question"]

        class NoEmbedding(FakeRetriever):
            def embed_query(self, query):
                raise RuntimeError("embedding deployment not found")
        fake = NoEmbedding(fail={(q, "keyword", NO_XLSX_FILTER): self.azure_400()})
        with self.assertRaises(retrieval.FilterRejected):
            retrieval.run_queries(fake, self.queries[:1], ("hybrid", "keyword"), ("full", "no_xlsx"), k=5)

    def test_a_400_the_unfiltered_search_also_got_is_not_a_rejected_filter(self):
        q = self.queries[0]["question"]
        error = self.azure_400("The vector field 'content_vector' expects 1536 dimensions")
        fake = FakeRetriever(fail={(q, "hybrid", None): error, (q, "hybrid", NO_XLSX_FILTER): error})
        records = retrieval.run_queries(fake, self.queries[:2], ("hybrid",), ("full", "no_xlsx"), k=5)
        self.assertEqual([r["error"]["type"] if r["error"] else None for r in records],
                         ["RuntimeError", "RuntimeError", None, None])  # recorded, and the run went on

    def test_searches_a_stopped_run_never_made_read_not_run(self):
        q2 = self.queries[1]["question"]
        fake, seen = FakeRetriever(fail={(q2, "hybrid", NO_XLSX_FILTER): self.azure_400()}), []
        with self.assertRaises(retrieval.FilterRejected):
            retrieval.run_queries(fake, self.queries, ("hybrid", "keyword"), ("full", "no_xlsx"), k=5,
                                  on_record=seen.append)
        lines, _ = retrieval.score_records(seen, {i["id"]: i for i in self.items})
        summary = retrieval.build_summary(lines, {"k": 5, "modes": ["hybrid", "keyword"],
                                                  "conditions": ["full", "no_xlsx"]})
        q2_row = next(row for row in summary["per_question"] if row["id"] == 2)
        self.assertEqual(q2_row["ranks"]["keyword · full"], "not run")
        self.assertEqual(q2_row["ranks"]["hybrid · no_xlsx"], "error")
        self.assertIn("not run = the run stopped before this search", retrieval.render_markdown(summary))

    def test_the_worst_list_is_capped_and_says_so(self):
        line = {"turn": 1, "mode": "hybrid", "condition": "full", "kind": "lookup", "companies": ["AMD"],
                "needs": "docs", "k": 5, "error": None, "na": False, "first_rank": 2, "missed": ["a fact"], "units": 2,
                "scores": {"hit_at_1": 0.0, "hit_at_3": 1.0, "hit": 1.0, "recall": 0.5, "complete": 0.0, "mrr": 0.5,
                           "precision": 0.2, "ndcg": 0.4, "right_file": 1.0}}
        lines = [dict(line, id=i, question=f"question {i}") for i in range(1, 21)]
        summary = retrieval.build_summary(lines, {"k": 5, "modes": ["hybrid"], "conditions": ["full"]})
        self.assertEqual((len(summary["worst"]), summary["worst_total"]), (retrieval.WORST_SHOWN, 20))
        self.assertIn(f"the {retrieval.WORST_SHOWN} worst of 20", retrieval.render_markdown(summary))

    def test_evidence_chunks_break_ties_on_the_units_a_pick_adds(self):
        units = [unit(["a.pdf"], "alpha"), unit(["c.pdf", "a.pdf"], "beta"), unit(["a.pdf"], "gamma")]
        chunks = [chunk("Z", "a.pdf", "alpha gamma", 0), chunk("Y", "a.pdf", "alpha beta", 9),
                  chunk("X", "c.pdf", "beta", 3)]
        # Z and Y tie first (two new units each; Z has the lower chunk_index). Then Y and X each add only
        # beta, whose first-listed file is c.pdf: X wins, although Y's file comes first for alpha
        self.assertEqual([c["chunk_id"] for c in snapshot.evidence_chunks(units, chunks, 5)], ["Z", "X"])


if __name__ == "__main__":
    unittest.main()
