"""Stage 1 helpers: send one gold question through the app and turn what happened into a record.

The graph route builds the same state as MultiAgentRAG.query() and runs `rag.graph.invoke` with two
LangChain callbacks: NodeTimer (time per agent) and get_usage_metadata_callback (tokens). The api route
posts to /api/v1/query through FastAPI's TestClient, so Content Safety and session memory are included.
Nothing here changes how the app behaves.
"""

import logging
import re
import time
from datetime import datetime, timezone

from langchain_core.callbacks import BaseCallbackHandler, get_usage_metadata_callback

from evaluation.gold import turns
from evaluation.probe import filter_leaked, is_filter_rejection, retrieval_probe
from evaluation.scoring import ANSWER_AGENT_FAILED, FAIL_OPEN_PREFIX

NODES = ("guardrail", "supervisor_retrieval", "intent_identifier", "answer_generator")
API_PATH = "/api/v1/query"
# Same texts as MultiAgentRAG.query() (src/agents/rag_agent.py) shows for a rejected question
REJECTED_UNSAFE = ("Your question has been flagged as inappropriate or potentially unsafe. "
                   "Please rephrase your question.")
REJECTED_IRRELEVANT = ("Your question doesn't appear to be related to the indexed documents. "
                       "Please ask questions relevant to the available content.")


class FilterRejected(RuntimeError):
    """Azure AI Search refused the no_xlsx filter; the run must stop."""

    def __init__(self, record: dict):
        super().__init__(record["error"]["message"])
        self.record = record


class NodeTimer(BaseCallbackHandler):
    """Times each LangGraph node (pairing start and end by run_id), counts chat-model calls and notes
    which node's LLM call failed (the agents catch their own errors, so the graph itself never fails)."""

    def __init__(self):
        self._open = {}
        self._llm_node = {}
        self.seconds = {}
        self.llm_calls = 0
        self.llm_errors = []

    def on_chain_start(self, serialized, inputs, *, run_id, metadata=None, **kwargs):
        node = (metadata or {}).get("langgraph_node")
        # Only the node's own run: calls nested inside a node carry the same metadata
        if node in NODES and kwargs.get("name") == node:
            self._open[run_id] = (node, time.perf_counter())

    def _close(self, run_id):
        started = self._open.pop(run_id, None)
        if started:
            node, t0 = started
            self.seconds[node] = self.seconds.get(node, 0.0) + time.perf_counter() - t0

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._close(run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._close(run_id)

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        self.llm_calls += 1
        self._llm_node[run_id] = (metadata or {}).get("langgraph_node")

    def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **kwargs):
        self.llm_calls += 1
        self._llm_node[run_id] = (metadata or {}).get("langgraph_node")

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.llm_errors.append({"node": self._llm_node.get(run_id), "type": type(error).__name__,
                                "message": sanitize(error, 160)})


class RetryCounter(logging.Handler):
    """Counts the openai SDK's "Retrying request" log records (it retries 429s and timeouts itself)."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.count = 0

    def emit(self, record):
        if "Retrying request" in record.getMessage():
            self.count += 1

    def take(self) -> int:
        count, self.count = self.count, 0
        return count

    def install(self):
        openai_logger = logging.getLogger("openai")
        openai_logger.setLevel(logging.INFO)
        openai_logger.propagate = False  # count, don't print
        openai_logger.addHandler(self)
        return self


_URL = re.compile(r"https?://\S+")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def sanitize(text, limit: int = 300) -> str:
    """Error text for the records: no URLs (they name the endpoints) and no long key-like tokens."""
    text = _URL.sub("<url>", str(text))
    text = _LONG_TOKEN.sub("<redacted>", text)
    return " ".join(text.split())[:limit]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rejection_text(reason: str) -> str:
    lowered = (reason or "").lower()
    return REJECTED_UNSAFE if ("unsafe" in lowered or "jailbreak" in lowered) else REJECTED_IRRELEVANT


def _base(entry: dict, turn_no: int, question: str) -> dict:
    return {
        "id": entry["item"]["id"], "turn": turn_no, "repeat": entry["repeat"],
        "condition": entry["condition"], "retrieval_filter": None, "route": entry["route"],
        "question": question, "answer": "", "answer_shown": None, "intent": "",
        "guardrail_passed": None, "guardrail_reason": None,
        "input_guardrail": None, "input_guardrail_details": None,
        "output_guardrail": None, "output_guardrail_details": None,
        "retrieved": [], "timings_s": None, "tokens": None, "llm_calls": None,
        "openai_retries": 0, "error": None, "notes": [], "started_at": now_iso(),
    }


def _flag_app_failures(record: dict, llm_errors=()) -> None:
    """Mark failures inside the app so they count as errors, not as wrong, declined or refused answers.

    The agents catch their own exceptions: a failed answer call becomes a fixed apology, a failed intent
    call falls back to "general", and a failed guardrail call lets the question through (fails open).
    """
    by_node = {}
    for error in llm_errors:
        by_node.setdefault(error.get("node"), error)
    if (record.get("answer") or "").startswith(ANSWER_AGENT_FAILED) and not record.get("error"):
        detail = by_node.get("answer_generator")
        record["error"] = {"type": "agent_error", "message": "the answer agent's LLM call failed"
                           + (f": {detail['type']}: {detail['message']}" if detail else "")}
    if "intent_identifier" in by_node:
        record["notes"].append("the intent agent's LLM call failed, so intent fell back to 'general'")
    if (record.get("guardrail_reason") or "").startswith(FAIL_OPEN_PREFIX):
        record["notes"].append("the guardrail agent failed open: the question went through unchecked")


def _sum_usage(usage_metadata: dict):
    if not usage_metadata:
        return None
    totals = {"input": 0, "output": 0, "total": 0, "by_model": {}}
    for model, usage in usage_metadata.items():
        totals["input"] += usage.get("input_tokens", 0)
        totals["output"] += usage.get("output_tokens", 0)
        totals["total"] += usage.get("total_tokens", 0)
        totals["by_model"][model] = {k: usage.get(k, 0) for k in ("input_tokens", "output_tokens", "total_tokens")}
    return totals


def run_graph_entry(rag, entry: dict, session_id: str, retries: RetryCounter) -> dict:
    """Run one question through `rag.graph` in-process and return its record."""
    question = entry["item"]["question"]
    record = _base(entry, 1, question)
    state = {  # the same fields MultiAgentRAG.query() fills (rag_agent.py)
        "session_id": session_id, "user_id": "", "current_question": question,
        "previous_question": "", "previous_answer": "", "retrieved_chunks": [], "retrieved_metadata": [],
        "intent": "", "answer": "", "conversation_turn": 1, "guardrail_passed": False, "guardrail_reason": "",
    }
    timer = NodeTimer()
    retries.take()
    t0 = time.perf_counter()
    try:
        with retrieval_probe(rag.supervisor_agent.retriever, entry["condition"]) as probe, \
                get_usage_metadata_callback() as usage:
            record["retrieval_filter"] = probe.filter
            out = rag.graph.invoke(state, config={"callbacks": [timer]})
    except Exception as e:  # the agents catch their own errors, so this is rare
        record["error"] = {"type": type(e).__name__, "message": sanitize(e)}
        record["timings_s"] = {"total": round(time.perf_counter() - t0, 3)}
        record["openai_retries"] = retries.take()
        return record
    total = time.perf_counter() - t0

    record["answer"] = out.get("answer") or ""
    record["intent"] = out.get("intent") or ""
    record["guardrail_passed"] = bool(out.get("guardrail_passed"))
    record["guardrail_reason"] = out.get("guardrail_reason") or ""
    if rag.guardrail_enabled and not record["guardrail_passed"]:
        record["answer_shown"] = rejection_text(record["guardrail_reason"])

    file_types = {doc.get("id"): (doc.get("metadata") or {}).get("file_type") for doc in (probe.docs or [])}
    record["retrieved"] = [
        {"rank": rank, "chunk_id": meta.get("chunk_id"), "source_file": meta.get("source_file"),
         "file_type": file_types.get(meta.get("chunk_id")), "chunk_index": meta.get("chunk_index"),
         "score": meta.get("score"), "content": meta.get("content")}
        for rank, meta in enumerate(out.get("retrieved_metadata") or [], 1)
    ]
    record["timings_s"] = {node: round(timer.seconds[node], 3) for node in NODES if node in timer.seconds}
    record["timings_s"]["total"] = round(total, 3)
    record["llm_calls"] = timer.llm_calls
    record["tokens"] = _sum_usage(usage.usage_metadata)
    if record["tokens"] is None and timer.llm_calls:
        record["notes"].append("the usage callback saw no tokens, although the agents made LLM calls")
    record["openai_retries"] = retries.take()
    if timer.llm_errors:
        record["llm_errors"] = timer.llm_errors
    _flag_app_failures(record, timer.llm_errors)

    if probe.error is not None:
        record["error"] = {"type": type(probe.error).__name__, "message": sanitize(probe.error)}
        if entry["condition"] == "no_xlsx" and is_filter_rejection(probe.error):
            record["error"]["type"] = "filter_rejected"
            raise FilterRejected(record)
    leaks = filter_leaked(entry["condition"], [chunk["source_file"] for chunk in record["retrieved"]])
    if leaks:
        record["error"] = {"type": "filter_leak",
                           "message": f"no_xlsx still retrieved workbook chunks from {', '.join(sorted(set(leaks)))}"}
    return record


def run_api_entry(client, entry: dict, session_id: str, top_k: int, retries: RetryCounter,
                  delay: float = 0.0) -> list:
    """Post each turn of a question to /api/v1/query with one session_id; one record per turn."""
    records = []
    for turn_no, turn in enumerate(turns(entry["item"]), 1):
        if turn_no > 1 and delay:
            time.sleep(delay)
        record = _base(entry, turn_no, turn["question"])
        retries.take()
        t0 = time.perf_counter()
        try:
            with get_usage_metadata_callback() as usage:
                response = client.post(API_PATH, json={"question": turn["question"], "session_id": session_id,
                                                       "top_k": top_k})
        except Exception as e:
            record["error"] = {"type": type(e).__name__, "message": sanitize(e)}
            record["openai_retries"] = retries.take()
            records.append(record)
            continue
        wall = time.perf_counter() - t0
        record["openai_retries"] = retries.take()
        # The API runs in TestClient's own thread, so the callbacks may not see its LLM calls
        record["tokens"] = _sum_usage(usage.usage_metadata)
        if response.status_code != 200:
            record["error"] = {"type": f"HTTP {response.status_code}", "message": sanitize(response.text)}
            record["timings_s"] = {"total": round(wall, 3)}
            records.append(record)
            continue
        data = response.json()
        record.update({
            "answer": data.get("answer") or "",
            "intent": data.get("intent") or "",
            "guardrail_passed": data.get("guardrail_passed"),
            "input_guardrail": data.get("input_guardrail"),
            "input_guardrail_details": data.get("input_guardrail_details"),
            "output_guardrail": data.get("output_guardrail"),
            "output_guardrail_details": data.get("output_guardrail_details"),
            "conversation_turn": data.get("conversation_turn"),
            "timings_s": {"total": round(data.get("processing_time_seconds") or wall, 3), "wall": round(wall, 3)},
        })
        record["retrieved"] = [
            {"rank": rank, "chunk_id": chunk.get("chunk_id"), "source_file": chunk.get("source_file"),
             "file_type": None, "chunk_index": chunk.get("chunk_index"), "score": chunk.get("score"),
             "content": chunk.get("full_content")}
            for rank, chunk in enumerate(data.get("retrieved_chunks") or [], 1)
        ]
        _flag_app_failures(record)
        records.append(record)
    return records
