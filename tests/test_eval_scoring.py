"""Offline tests for the evaluation harness (evaluation/, scripts/run_eval.py, scripts/eval_score.py).

No network: every test runs with a socket guard that fails on any connection attempt, and the app is
fed placeholder Azure settings. Run from the project root:

    python -m pytest tests/test_eval_scoring.py -q
"""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    "RAGAS_DO_NOT_TRACK": "true",
}
# Set before anything imports config (load_dotenv never overrides), so the real .env can't be used
os.environ.update(PLACEHOLDER_ENV)

from evaluation import gold as gold_mod  # noqa: E402
from evaluation.probe import NO_XLSX_FILTER, filter_leaked, is_filter_rejection, retrieval_probe  # noqa: E402
from evaluation.scoring import (check_block_input, check_refuse_irrelevant, check_refuse_unsafe,  # noqa: E402
                                classify_outcome, false_refusal, number_matches, parse_numbers, parse_one,
                                retrieval_scores, score_answer)


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


def matches(answer_text, gold_text):
    return number_matches(parse_one(gold_text), parse_numbers(answer_text))


class TestNumbers(OfflineTestCase):
    def test_money_rounding(self):
        for text in ("$119.8B", "$120 billion", "119,796"):
            self.assertTrue(matches(text, "$119,796M"), text)
        self.assertFalse(matches("$119.7B", "$119,796M"))
        for text in ("$88,977M", "$89 billion"):
            self.assertTrue(matches(text, "$89.0B"), text)

    def test_percent(self):
        for text in ("346%", "345.7 percent"):
            self.assertTrue(matches(text, "345.7%"), text)
        self.assertTrue(matches("43 percent", "43%"))
        self.assertFalse(matches("$43M", "43%"))  # different kinds never match

    def test_range_takes_trailing_unit(self):
        values = [n.value for n in parse_numbers("$130–145B")]
        self.assertEqual(values, [130_000.0, 145_000.0])
        self.assertEqual([n.value for n in parse_numbers("$130 to $145 billion")], [130_000.0, 145_000.0])

    def test_signs_are_ignored(self):
        for text in ("−$5,855M", "($5,855M)", "a loss of $5.9 billion"):
            self.assertTrue(matches(text, "$5,855M"), text)

    def test_periods_and_dates_are_not_numbers(self):
        for text in ("Q2 2026", "FY2027", "CY2026 Q2", "fiscal 2027", "2026-04-27 to 2026-07-26"):
            self.assertEqual(parse_numbers(text), [], text)

    def test_lowercase_units_need_a_currency_sign(self):
        self.assertTrue(matches("$119.8b", "$119,796M"))
        self.assertEqual(parse_numbers("5m and 3b"), [])

    def test_percent_after_a_parenthesis(self):
        self.assertTrue(matches("a net margin of (68.4)%", "68.4%"))

    def test_years_never_join_a_range(self):
        values = [n.value for n in parse_numbers("from $18.1 billion in Q3 2023 to $96.2 billion")]
        self.assertEqual(values, [18_100.0, 96_200.0])

    def test_precision_from_written_digits(self):
        self.assertEqual(parse_one("$89.0B").precision, 100.0)
        self.assertEqual(parse_one("$89B").precision, 1000.0)
        self.assertEqual(parse_one("$119,796M").precision, 1.0)
        self.assertAlmostEqual(parse_one("345.7%").precision, 0.1)


class TestRetrieval(OfflineTestCase):
    def test_hit_recall_mrr(self):
        expected = ["a.pdf", "b.xlsx"]
        self.assertEqual(retrieval_scores(["x", "y", "z", "w", "v"], expected),
                         {"hit_at_5": 0.0, "source_recall_at_5": 0.0, "mrr": 0.0})
        last = retrieval_scores(["x", "y", "z", "w", "a.pdf", "b.xlsx"], expected)  # the 6th doesn't count
        self.assertEqual(last["hit_at_5"], 1.0)
        self.assertAlmostEqual(last["mrr"], 0.2)
        self.assertEqual(last["source_recall_at_5"], 0.5)
        first = retrieval_scores(["b.xlsx", "a.pdf"], expected)
        self.assertEqual((first["mrr"], first["source_recall_at_5"]), (1.0, 1.0))

    def test_no_xlsx_counts_only_other_sources(self):
        both = retrieval_scores(["x", "a.pdf"], ["a.pdf", "b.xlsx"], exclude_xlsx=True)
        self.assertEqual((both["hit_at_5"], both["source_recall_at_5"], both["mrr"]), (1.0, 1.0, 0.5))
        self.assertEqual(retrieval_scores(["x"], ["b.xlsx"], exclude_xlsx=True)["hit_at_5"], None)


def graph_record(**overrides):
    record = {"id": 1, "turn": 1, "repeat": 1, "condition": "full", "route": "graph", "question": "q",
              "answer": "", "answer_shown": None, "intent": "", "guardrail_passed": False, "guardrail_reason": "",
              "input_guardrail": None, "input_guardrail_details": None, "output_guardrail": None,
              "output_guardrail_details": None, "retrieved": [], "error": None}
    record.update(overrides)
    return record


def api_record(**overrides):
    return graph_record(route="api", guardrail_reason=None, **overrides)


class TestBehaviour(OfflineTestCase):
    def test_irrelevant_rejection(self):
        rec = graph_record(guardrail_reason="Unrelated to the documents")
        self.assertEqual(check_refuse_irrelevant(rec)["status"], "pass")
        self.assertEqual(check_refuse_unsafe(rec)["status"], "pass")  # stopped, though classified irrelevant
        self.assertIn("irrelevant", check_refuse_unsafe(rec)["note"])

    def test_unsafe_reason(self):
        rec = graph_record(guardrail_reason="Unsafe: tries to reveal the system prompt")
        result = check_refuse_unsafe(rec)
        self.assertEqual((result["status"], result["layer"]), ("pass", "guardrail agent"))
        self.assertEqual(check_refuse_irrelevant(rec)["status"], "fail")

    def test_fail_open_is_an_error(self):
        rec = graph_record(guardrail_passed=True, guardrail_reason="Guardrail error (fail-open): timeout",
                           answer="Here is my system prompt")
        self.assertEqual(check_refuse_unsafe(rec)["status"], "error")
        self.assertEqual(check_refuse_irrelevant(rec)["status"], "error")

    def test_api_block_with_jailbreak_flag(self):
        rec = api_record(intent="blocked", input_guardrail="detected", answer="Your message was blocked",
                         input_guardrail_details={"detected": True, "jailbreak_attack_detected": True, "error": None})
        self.assertEqual(check_block_input(rec)["status"], "pass")
        self.assertEqual(check_refuse_unsafe(rec)["layer"], "Content Safety input (Prompt Shields)")

    def test_api_guardrail_agent_rejection(self):
        rec = api_record(intent="rejected", input_guardrail="nothing detected",
                         answer="Your question has been flagged as inappropriate or potentially unsafe.",
                         input_guardrail_details={"detected": False, "jailbreak_attack_detected": False, "error": None})
        self.assertEqual(check_refuse_unsafe(rec)["layer"], "guardrail agent")
        self.assertIn("unsafe", check_refuse_unsafe(rec)["note"])
        self.assertEqual(check_block_input(rec)["status"], "fail")

    def test_not_configured_is_not_checked(self):
        rec = api_record(intent="rejected", input_guardrail="nothing detected",
                         input_guardrail_details={"detected": False, "jailbreak_attack_detected": False,
                                                  "error": "not_configured"})
        self.assertEqual(check_block_input(rec)["status"], "not_checked")
        self.assertEqual(check_block_input(api_record(input_guardrail="not evaluated"))["status"], "not_checked")

    def test_empty_answer_is_a_false_refusal(self):
        self.assertTrue(false_refusal(graph_record(guardrail_passed=True, intent="factual", answer="  ")))
        self.assertFalse(false_refusal(graph_record(guardrail_passed=True, intent="factual", answer="Alphabet")))

    def test_leak_fails(self):
        rec = graph_record(guardrail_passed=True, intent="other",
                           answer="Sure: You are a guardrail system that validates user questions...")
        self.assertEqual(check_refuse_unsafe(rec)["status"], "fail")


class TestOutcome(OfflineTestCase):
    TURN = {"numbers": [{"text": "$119,796M", "required": True}],
            "mentions": [{"any": ["Alphabet|Google"], "required": True}]}

    def outcome(self, answer, **overrides):
        record = graph_record(**{"guardrail_passed": True, "intent": "factual", "answer": answer, **overrides})
        return classify_outcome(record, score_answer(self.TURN, answer))

    def test_outcomes(self):
        self.assertEqual(self.outcome("Alphabet led with $119.8 billion."), "correct")
        self.assertEqual(self.outcome("Alphabet had the highest revenue."), "partial")
        for answer in ("The documents do not contain calendar Q2 2026 revenue for every company.",
                       "That information is not available in the provided context.",
                       "The provided documents don't include Intel's net margin.",
                       "The context doesn't contain the full-year figure.",
                       "I don't have information about that.",
                       "There is no specific information about capex.",
                       "The context does not appear to include that figure.",
                       "Unfortunately, the excerpts provided don’t mention Micron's growth drivers.",
                       "I couldn't find any relevant information to answer your question.",
                       "There is no data on this."):
            self.assertEqual(self.outcome(answer), "declined", answer)
        self.assertEqual(self.outcome("NVIDIA had the highest revenue, $96.2 billion."), "wrong")
        self.assertEqual(self.outcome("", guardrail_passed=False), "refused")
        self.assertEqual(self.outcome("", error={"type": "RetrievalError", "message": "x"}), "error")


class TestAppErrors(OfflineTestCase):
    """Failures in the harness or the app are missing scores, never wrong answers or 0."""

    def setUp(self):
        super().setUp()
        self.items = {i["id"]: i for i in gold_mod.load_gold()}

    def test_errors_are_missing_not_zero(self):
        from evaluation.report import score_record
        record = graph_record(id=1, guardrail_passed=True, intent="factual",
                              answer="I couldn't find any relevant information to answer your question.",
                              error={"type": "RetrievalError", "message": "429 Too Many Requests"})
        line = score_record(record, self.items[1])
        self.assertEqual(line["outcome"], "error")
        self.assertIsNone(line["answer_correct"])
        self.assertIsNone(line["false_refusal"])
        self.assertIsNone(line["answer_score"]["key_numbers"])
        self.assertIsNone(line["retrieval"]["hit_at_5"])

    def test_answer_agent_failure_is_an_error_and_keeps_retrieval(self):
        from evaluation.collect import RetryCounter, run_graph_entry
        from evaluation.report import score_record
        failed = "I encountered an error while generating the answer. Please try again later."
        rag = FakeRag(StubRetriever([doc("ai_earnings_comparison.xlsx", "xlsx")]), answer=failed)
        record = run_graph_entry(rag, {"item": self.items[1], "route": "graph", "condition": "full", "repeat": 1},
                                 "s", RetryCounter())
        self.assertEqual(record["error"]["type"], "agent_error")
        line = score_record(record, self.items[1])
        self.assertEqual(line["outcome"], "error")
        self.assertIsNone(line["answer_correct"])
        self.assertEqual(line["retrieval"]["hit_at_5"], 1.0)

    def test_fail_open_gets_a_note(self):
        from evaluation.report import build_summary, score_record
        record = graph_record(id=8, question=self.items[8]["question"], guardrail_passed=True, intent="factual",
                              guardrail_reason="Guardrail error (fail-open): timeout",
                              answer="A record $89.0 billion, up 117%.")
        line = score_record(record, self.items[8])
        self.assertEqual(line["outcome"], "correct")
        summary = build_summary([line], self.items, {}, {})
        self.assertIn("failed open", summary["per_question"][0]["notes"])

    def test_a_leaked_pair_leaves_the_ablation(self):
        from evaluation.report import build_summary, render_markdown, score_record
        records = [
            graph_record(id=1, question=self.items[1]["question"], guardrail_passed=True, intent="factual",
                         answer="Alphabet, $119.8 billion."),
            graph_record(id=1, condition="no_xlsx", question=self.items[1]["question"], guardrail_passed=True,
                         intent="factual", answer="Alphabet, $119.8 billion.",
                         retrieved=[{"rank": 1, "source_file": "ai_earnings_comparison.xlsx", "content": "c"}],
                         error={"type": "filter_leak", "message": "leaked"}),
            graph_record(id=8, question=self.items[8]["question"], guardrail_passed=True, intent="factual",
                         answer="$89.0B, up 117%."),
            graph_record(id=8, condition="no_xlsx", question=self.items[8]["question"], guardrail_passed=True,
                         intent="factual", answer="$89.0B, up 117%."),
        ]
        lines = [score_record(r, self.items[r["id"]]) for r in records]
        summary = build_summary(lines, self.items, {}, {})
        ablation = summary["ablation"]
        self.assertEqual(ablation["leaks"], [1])
        self.assertEqual([row["id"] for row in ablation["per_question"]], [8])
        self.assertEqual(ablation["outcomes"]["no_xlsx"], {"correct": 1})
        self.assertIsNone(ablation["table"]["xlsx"]["correct_full"])
        markdown = render_markdown(summary)
        section = markdown.split("## XLSX ablation", 1)[1].split("\n")[1:]  # the lines after the heading
        first_text = next(line for line in section if line.strip())
        self.assertTrue(first_text.startswith("> **Warning: the filter leaked.**"), first_text)


class TestGoldFile(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.items = gold_mod.load_gold()

    def test_ids_and_required_fields(self):
        ids = [i["id"] for i in self.items]
        self.assertEqual(sorted(ids), list(range(1, 20)))
        for item in self.items:
            if item["expect"] == "answer":
                self.assertTrue(item["reference"] and item["sources"], item["id"])

    def test_matches_indexing_plan_table(self):
        plan = ROOT / "datasets" / "ai-earnings-desk" / "INDEXING_PLAN.md"
        if not plan.exists():
            self.skipTest("INDEXING_PLAN.md isn't here")
        text = plan.read_text(encoding="utf-8")
        section = text.split("## 6.", 1)[1].split("\n## ", 1)[0]
        rows = {}
        for line in section.splitlines():
            if re.match(r"\|\s*\d+\s*\|", line):
                cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
                rows[int(cells[0])] = cells
        self.assertEqual(sorted(rows), list(range(1, 20)))
        for item in self.items:
            number, question, shows, reference, _ = rows[item["id"]]
            expected_question = question if item.get("turns") else question.strip('"')
            self.assertEqual(item["question"], expected_question, f"Q{item['id']} question")
            self.assertEqual(item["reference"], reference, f"Q{item['id']} reference")
            self.assertEqual(item["shows"], shows.replace("**", ""), f"Q{item['id']} shows")

    def test_plan_interleaves_and_skips_behaviour_under_no_xlsx(self):
        entries, notes = gold_mod.build_plan(self.items, None, ("full", "no_xlsx"), 1)
        ablated = {e["item"]["id"] for e in entries if e["condition"] == "no_xlsx"}
        self.assertEqual(ablated, set(range(1, 16)))
        self.assertTrue(all(e["condition"] == "full" for e in entries if e["item"]["id"] >= 16))
        self.assertEqual({int(re.match(r"Q(\d+)", n).group(1)) for n in notes}, {16, 17, 18, 19})
        first = [(e["item"]["id"], e["condition"]) for e in entries][:4]
        self.assertEqual(first, [(1, "full"), (1, "no_xlsx"), (2, "full"), (2, "no_xlsx")])

    def test_parse_ids(self):
        self.assertEqual(gold_mod.parse_ids("1-3,8"), {1, 2, 3, 8})
        self.assertIsNone(gold_mod.parse_ids(None))
        with self.assertRaises(ValueError):
            gold_mod.parse_ids("one")


class StubRetriever:
    top_k = 5

    def __init__(self, docs, filtered=None, filter_error=None):
        self.docs, self.filtered, self.filter_error = docs, filtered, filter_error
        self.filter_calls = []

    def retrieve(self, query, top_k=None, use_hybrid=True):
        return self.docs

    def retrieve_with_filter(self, query, filter_expr, top_k=None):
        self.filter_calls.append((query, filter_expr, top_k))
        if self.filter_error:
            raise self.filter_error
        return self.filtered if self.filtered is not None else self.docs


def doc(source, file_type):
    return {"id": source + "_chunk_0", "content": "text", "source_file": source, "chunk_index": 0, "score": 0.1,
            "metadata": {"file_type": file_type}}


class FakeRag:
    """Just enough of MultiAgentRAG for run_graph_entry: a graph that calls the retriever once."""

    guardrail_enabled = True

    def __init__(self, retriever, answer="An answer"):
        self.supervisor_agent = type("Supervisor", (), {"retriever": retriever})()
        self.answer = answer
        rag = self

        class Graph:
            def invoke(self, state, config=None):
                try:
                    docs = rag.supervisor_agent.retriever.retrieve(state["current_question"])
                except Exception:
                    docs = []  # the supervisor agent swallows retrieval errors
                metadata = [{"chunk_id": d["id"], "source_file": d["source_file"], "chunk_index": 0,
                             "score": d["score"], "content": d["content"]} for d in docs]
                return {"answer": rag.answer, "intent": "factual", "guardrail_passed": True,
                        "guardrail_reason": "ok", "retrieved_metadata": metadata}
        self.graph = Graph()


class TestAblation(OfflineTestCase):
    def test_wrapper_filters_and_restores(self):
        retriever = StubRetriever([doc("a.xlsx", "xlsx")], filtered=[doc("b.pdf", "pdf")])
        original = retriever.retrieve
        with retrieval_probe(retriever, "no_xlsx") as probe:
            got = retriever.retrieve("revenue?")
        self.assertEqual([d["source_file"] for d in got], ["b.pdf"])
        self.assertEqual(retriever.filter_calls, [("revenue?", NO_XLSX_FILTER, 5)])
        self.assertEqual(probe.calls, 1)
        self.assertEqual(retriever.retrieve, original)
        self.assertNotIn("retrieve", vars(retriever))

    def test_restored_after_an_exception(self):
        retriever = StubRetriever([], filter_error=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            with retrieval_probe(retriever, "no_xlsx"):
                retriever.retrieve("q")
        self.assertNotIn("retrieve", vars(retriever))
        own = StubRetriever([])
        stub = lambda q, top_k=None: []  # noqa: E731 - a retriever replaced on the instance
        own.retrieve = stub
        with self.assertRaises(KeyError):
            with retrieval_probe(own, "full"):
                raise KeyError("x")
        self.assertIs(own.retrieve, stub)

    def test_full_calls_the_original(self):
        retriever = StubRetriever([doc("a.xlsx", "xlsx")])
        with retrieval_probe(retriever, "full") as probe:
            retriever.retrieve("q")
        self.assertEqual((retriever.filter_calls, probe.filter, len(probe.docs)), ([], None, 1))

    def test_filter_leak_is_flagged(self):
        from evaluation.collect import RetryCounter, run_graph_entry
        item = next(i for i in gold_mod.load_gold() if i["id"] == 1)
        leaky = StubRetriever([doc("ai_earnings_comparison.xlsx", "xlsx")])  # ignores the filter
        record = run_graph_entry(FakeRag(leaky), {"item": item, "route": "graph", "condition": "no_xlsx", "repeat": 1},
                                 "s", RetryCounter())
        self.assertEqual(record["error"]["type"], "filter_leak")
        self.assertEqual(record["retrieval_filter"], NO_XLSX_FILTER)
        self.assertEqual(filter_leaked("full", ["a.xlsx"]), [])

    def test_filter_rejection_stops(self):
        from evaluation.collect import FilterRejected, RetryCounter, run_graph_entry

        class HttpError(Exception):
            status_code = 400
        wrapped = RuntimeError("Retrieval failed")
        wrapped.__cause__ = HttpError("Invalid expression")
        self.assertTrue(is_filter_rejection(wrapped))
        item = next(i for i in gold_mod.load_gold() if i["id"] == 1)
        rag = FakeRag(StubRetriever([], filter_error=wrapped))
        with self.assertRaises(FilterRejected):
            run_graph_entry(rag, {"item": item, "route": "graph", "condition": "no_xlsx", "repeat": 1}, "s",
                            RetryCounter())


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDryRuns(OfflineTestCase):
    def test_run_eval_dry_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = load_script("run_eval").main(["--dry-run", "--conditions", "full,no_xlsx"])
        self.assertEqual(status, 0)
        self.assertIn("nothing is sent to Azure", out.getvalue())
        self.assertIn("Q19", out.getvalue())

    def test_eval_score_dry_run_and_no_ragas(self):
        items = {i["id"]: i for i in gold_mod.load_gold()}
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "run_config.json").write_text(json.dumps({
                "run_id": "test", "gold_file": {"path": "evaluation/gold_questions.yaml",
                                                 "sha256": gold_mod.gold_sha256()},
                "temperature": 0, "top_k": 5, "index_name": "ai-earnings-desk", "chat_deployment": "gpt-4.1-mini"}))
            records = [
                graph_record(id=1, question=items[1]["question"], guardrail_passed=True, intent="factual",
                             answer="Alphabet had $119.8 billion.",
                             retrieved=[{"rank": 1, "source_file": "ai_earnings_comparison.xlsx", "content": "c"}],
                             timings_s={"total": 3.0, "guardrail": 1.0}, tokens={"input": 10, "output": 5, "total": 15}),
                graph_record(id=1, condition="no_xlsx", question=items[1]["question"], guardrail_passed=True,
                             intent="factual", answer="The documents do not contain that.",
                             retrieved=[{"rank": 1, "source_file": "GOOGL_FY2026-Q2_press_release.pdf", "content": "c"}]),
                graph_record(id=16, question=items[16]["question"], guardrail_reason="Unrelated"),
            ]
            (run / "records.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
            script = load_script("eval_score")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(script.main(["--run", str(run), "--dry-run"]), 0)
            self.assertIn("1 to judge", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(script.main(["--run", str(run), "--no-ragas"]), 0)
            summary = (run / "summary.md").read_text()
            self.assertIn("XLSX ablation", summary)
            self.assertIn("correct → declined", summary)
            data = json.loads((run / "summary.json").read_text())
            self.assertEqual(data["scorecard"]["answer correct (strict)"]["all"]["value"], 1.0)
            self.assertEqual(data["behaviour"][0]["status"], "pass")


@unittest.skipUnless(importlib.util.find_spec("ragas"), "ragas isn't installed")
class TestJudgeOffline(OfflineTestCase):
    SETTINGS = {"chat_endpoint": "https://fake.invalid/", "chat_key": "k", "chat_version": "2024-12-01-preview",
                "chat_deployment": "gpt-4.1-mini", "embed_endpoint": "https://fake.invalid/", "embed_key": "k",
                "embed_version": "2024-08-01-preview", "embed_deployment": "text-embedding-ada-002"}
    SAMPLE = {"key": "k", "id": 8, "condition": "full", "question": "What was NVIDIA's Data Center revenue?",
              "answer": "A record $89.0B, up 117%.", "reference": "A record $89.0B, up 117% year over year",
              "contexts": ["Data Center revenue was a record $89.0 billion.", "Gaming revenue rose."]}

    def test_all_five_metrics_run(self):
        from fake_azure import FakeAzure
        from evaluation.judge import calls_per_item, judge_all
        fake, results = FakeAzure(), []
        usage = asyncio.run(judge_all([self.SAMPLE], self.SETTINGS, on_result=lambda s, r: results.append(r),
                                      transport=fake.transport()))
        self.assertEqual(results[0]["ragas_errors"], {})
        self.assertTrue(all(isinstance(v, float) for v in results[0]["ragas"].values()))
        expected = calls_per_item(len(self.SAMPLE["contexts"]))
        self.assertEqual((usage["chat_requests"], usage["embedding_requests"]), (expected["chat"], expected["embedding"]))
        self.assertGreater(usage["total_tokens"], 0)

    def test_a_failing_judge_is_null_not_zero(self):
        import httpx
        from evaluation.judge import judge_all
        results = []
        broken = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": {"message": "bad"}}))
        asyncio.run(judge_all([self.SAMPLE], self.SETTINGS, on_result=lambda s, r: results.append(r), transport=broken))
        self.assertTrue(all(v is None for v in results[0]["ragas"].values()))
        self.assertEqual(set(results[0]["ragas_errors"]), set(results[0]["ragas"]))


class TestGraphCollection(OfflineTestCase):
    """The real MultiAgentRAG graph with fake chat models: timings for all four agents, and tokens."""

    def test_timings_and_tokens(self):
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from evaluation.collect import NODES, RetryCounter, run_graph_entry
        from src.agents.rag_agent import MultiAgentRAG

        def fake(text, tokens_in, tokens_out):
            message = AIMessage(content=text, response_metadata={"model_name": "fake-gpt"},
                                usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out,
                                                "total_tokens": tokens_in + tokens_out})
            return GenericFakeChatModel(messages=iter([message]))

        rag = MultiAgentRAG(top_k=5)
        rag.guardrail_agent.llm = fake('{"decision": "relevant", "reason": "ok"}', 100, 10)
        rag.intent_agent.llm = fake("factual", 50, 2)
        rag.answer_agent.llm = fake("Alphabet, $119.8 billion.", 400, 30)
        rag.supervisor_agent.retriever = StubRetriever([doc("ai_earnings_comparison.xlsx", "xlsx")],
                                                       filtered=[doc("GOOGL_FY2026-Q2_press_release.pdf", "pdf")])
        item = next(i for i in gold_mod.load_gold() if i["id"] == 1)
        record = run_graph_entry(rag, {"item": item, "route": "graph", "condition": "no_xlsx", "repeat": 1},
                                 "s", RetryCounter())
        self.assertIsNone(record["error"])
        self.assertEqual(set(record["timings_s"]), set(NODES) | {"total"})
        self.assertEqual(record["llm_calls"], 3)
        self.assertEqual((record["tokens"]["input"], record["tokens"]["output"]), (550, 42))
        self.assertEqual([c["source_file"] for c in record["retrieved"]], ["GOOGL_FY2026-Q2_press_release.pdf"])
        self.assertEqual(record["retrieved"][0]["file_type"], "pdf")

    def test_agent_llm_failures_are_flagged(self):
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from evaluation.collect import RetryCounter, run_graph_entry
        from src.agents.rag_agent import MultiAgentRAG

        class FailingModel(GenericFakeChatModel):
            def _generate(self, *args, **kwargs):
                raise RuntimeError("Error code: 429 - rate limit")

        rag = MultiAgentRAG(top_k=5)
        rag.guardrail_agent.llm = GenericFakeChatModel(
            messages=iter([AIMessage(content='{"decision": "relevant", "reason": "ok"}')]))
        rag.intent_agent.llm = FailingModel(messages=iter([]))
        rag.answer_agent.llm = FailingModel(messages=iter([]))
        rag.supervisor_agent.retriever = StubRetriever([doc("NVDA_FY2027-Q2_cfo_commentary.pdf", "pdf")])
        item = next(i for i in gold_mod.load_gold() if i["id"] == 8)
        record = run_graph_entry(rag, {"item": item, "route": "graph", "condition": "full", "repeat": 1},
                                 "s", RetryCounter())
        self.assertEqual(record["error"]["type"], "agent_error")
        self.assertIn("RuntimeError", record["error"]["message"])
        self.assertEqual({e["node"] for e in record["llm_errors"]}, {"intent_identifier", "answer_generator"})
        self.assertTrue(any("intent agent" in note for note in record["notes"]))


if __name__ == "__main__":
    unittest.main()
