"""Answer eval, stage 1 helpers: send one golden-set question through the app and record what happened.

Three ways in:
- graph: builds the same state as MultiAgentRAG.query() and runs `rag.graph.invoke` in-process.
- api: posts to /api/v1/query through FastAPI's TestClient, so Content Safety and session memory are
  included (follow-up items and the guardrail checks use it).
- gold: skips the guardrail and retrieval and gives the intent and answer agents the item's evidence
  chunks from an index snapshot: how well the agent answers when retrieval is perfect.

Every route is watched the same way: a LangChain callback (NodeTimer, attached through a context hook,
so it also sees the API's per-request graph) times each agent and notes failed LLM calls, and
get_usage_metadata_callback counts tokens. Nothing here changes how the app behaves.
"""

import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from langchain_core.callbacks import BaseCallbackHandler, get_usage_metadata_callback
from langchain_core.tracers.context import register_configure_hook

from evaluation.gold import turns
from evaluation.probe import api_retrieval_probe, filter_leaked, is_filter_rejection, retrieval_probe
from evaluation.scoring import ANSWER_AGENT_FAILED, FAIL_OPEN_PREFIX

NODES = ("guardrail", "supervisor_retrieval", "intent_identifier", "answer_generator")
API_PATH = "/api/v1/query"
# Same texts as MultiAgentRAG.query() (src/agents/rag_agent.py) shows for a rejected question
REJECTED_UNSAFE = ("Your question has been flagged as inappropriate or potentially unsafe. "
                   "Please rephrase your question.")
REJECTED_IRRELEVANT = ("Your question doesn't appear to be related to the indexed documents. "
                       "Please ask questions relevant to the available content.")
GOLD_GUARDRAIL_REASON = "not run: gold contexts"


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
        self.default_node = None  # the node to blame for a failed call outside the graph (the gold route)

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
        self.llm_errors.append({"node": self._llm_node.get(run_id) or self.default_node,
                                "type": type(error).__name__, "message": sanitize(error, 160)})


# The timer reaches every LangChain run started while it is set, including the graph the API builds per
# request inside TestClient (the same mechanism as get_usage_metadata_callback)
_TIMER: ContextVar[Optional[NodeTimer]] = ContextVar("eval_node_timer", default=None)
register_configure_hook(_TIMER, inheritable=True)


@contextmanager
def watch_llm_calls():
    timer = NodeTimer()
    token = _TIMER.set(timer)
    try:
        yield timer
    finally:
        _TIMER.reset(token)


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
_HOST_ARG = re.compile(r"host=['\"][^'\"]*['\"]")
_HOST = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:windows\.net|azure\.com|azure\.net|microsoft\.com)\b", re.IGNORECASE)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def sanitize(text, limit: int = 300) -> str:
    """Error text for the records: no URLs or host names (they name the endpoints) and no long key-like
    tokens, so a record can be shared without revealing the Azure resources."""
    text = _URL.sub("<url>", str(text))
    text = _HOST_ARG.sub("host=<host>", text)
    text = _HOST.sub("<host>", text)
    text = _LONG_TOKEN.sub("<redacted>", text)
    return " ".join(text.split())[:limit]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rejection_text(reason: str) -> str:
    lowered = (reason or "").lower()
    return REJECTED_UNSAFE if ("unsafe" in lowered or "jailbreak" in lowered) else REJECTED_IRRELEVANT


def _base(entry: dict, turn_no: int, question: str, guardrail_enabled: bool) -> dict:
    return {
        "id": entry["item"]["id"], "turn": turn_no, "repeat": entry["repeat"],
        "condition": entry["condition"], "retrieval_filter": None, "route": entry["route"],
        "question": question, "answer": "", "answer_shown": None, "intent": "",
        "guardrail_enabled": guardrail_enabled, "guardrail_passed": None, "guardrail_reason": None,
        "input_guardrail": None, "input_guardrail_details": None,
        "output_guardrail": None, "output_guardrail_details": None,
        "conversation_turn": None, "retrieval_query": None, "retrieval_calls": None,
        "retrieved": [], "timings_s": None, "tokens": None, "llm_calls": None, "llm_errors": [],
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
    record["llm_errors"] = list(llm_errors)
    if (record.get("answer") or "").startswith(ANSWER_AGENT_FAILED) and not record.get("error"):
        detail = by_node.get("answer_generator")
        record["error"] = {"type": "agent_error", "message": "the answer agent's LLM call failed"
                           + (f": {detail['type']}: {detail['message']}" if detail else "")}
    if "intent_identifier" in by_node:
        record["notes"].append("the intent agent's LLM call failed, so intent fell back to 'general'")
    if "supervisor_retrieval" in by_node:
        record["notes"].append("the follow-up rewrite failed, so the search used the two questions joined")
    reason = record.get("guardrail_reason") or ""
    if "guardrail" in by_node and record.get("guardrail_passed") and not reason.startswith(FAIL_OPEN_PREFIX):
        # The API reports no fail-open of its own when it's older than this harness: the callback saw it
        record["guardrail_reason"] = f"{FAIL_OPEN_PREFIX}: {by_node['guardrail']['type']} (seen by the harness)"
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


def _chunks(metadata: list, file_types: dict) -> list:
    return [{"rank": rank, "chunk_id": meta.get("chunk_id"), "source_file": meta.get("source_file"),
             "file_type": file_types.get(meta.get("chunk_id")), "chunk_index": meta.get("chunk_index"),
             "score": meta.get("score"), "content": meta.get("content") if "content" in meta else meta.get("full_content")}
            for rank, meta in enumerate(metadata, 1)]


def _file_types(probe) -> dict:
    return {doc.get("id"): (doc.get("metadata") or {}).get("file_type") for doc in (probe.docs or [])}


def _finish(record: dict, timer: NodeTimer, usage, total: float, retries) -> None:
    record["timings_s"] = {node: round(timer.seconds[node], 3) for node in NODES if node in timer.seconds}
    record["timings_s"]["total"] = round(total, 3)
    record["llm_calls"] = timer.llm_calls
    record["tokens"] = _sum_usage(usage.usage_metadata)
    if record["tokens"] is None and timer.llm_calls:
        record["notes"].append("the usage callback saw no tokens, although the agents made LLM calls")
    record["openai_retries"] = retries.take()


def run_graph_entry(rag, entry: dict, session_id: str, retries) -> dict:
    """Run one question through `rag.graph` in-process and return its record."""
    question = entry["item"]["question"]
    record = _base(entry, 1, question, rag.guardrail_enabled)
    state = {  # the same fields MultiAgentRAG.query() fills (rag_agent.py)
        "session_id": session_id, "user_id": "", "current_question": question,
        "previous_question": "", "previous_answer": "", "retrieved_chunks": [], "retrieved_metadata": [],
        "intent": "", "answer": "", "conversation_turn": 1, "guardrail_passed": False, "guardrail_reason": "",
        "retrieval_query": "",
    }
    retries.take()
    t0 = time.perf_counter()
    try:
        with retrieval_probe(rag.supervisor_agent.retriever, entry["condition"]) as probe, \
                watch_llm_calls() as timer, get_usage_metadata_callback() as usage:
            record["retrieval_filter"] = probe.filter
            out = rag.graph.invoke(state)
    except Exception as e:  # the agents catch their own errors, so this is rare
        record["error"] = {"type": type(e).__name__, "message": sanitize(e)}
        record["timings_s"] = {"total": round(time.perf_counter() - t0, 3)}
        record["openai_retries"] = retries.take()
        return record
    _finish(record, timer, usage, time.perf_counter() - t0, retries)

    record["answer"] = out.get("answer") or ""
    record["intent"] = out.get("intent") or ""
    record["guardrail_passed"] = bool(out.get("guardrail_passed"))
    record["guardrail_reason"] = sanitize(out.get("guardrail_reason") or "")
    record["conversation_turn"] = out.get("conversation_turn")
    record["retrieval_query"] = out.get("retrieval_query") or (probe.queries[0] if probe.queries else None)
    record["retrieval_calls"] = probe.calls
    if rag.guardrail_enabled and not record["guardrail_passed"]:
        record["answer_shown"] = rejection_text(record["guardrail_reason"])
    record["retrieved"] = _chunks(out.get("retrieved_metadata") or [], _file_types(probe))
    _flag_app_failures(record, timer.llm_errors)

    if probe.error is not None:
        record["error"] = {"type": "retrieval_error", "message": f"{type(probe.error).__name__}: {sanitize(probe.error)}"}
        if entry["condition"] == "no_xlsx" and is_filter_rejection(probe.error):
            record["error"]["type"] = "filter_rejected"
            raise FilterRejected(record)
    leaks = filter_leaked(entry["condition"], record["retrieved"])
    if leaks:
        record["error"] = {"type": "filter_leak",
                           "message": f"no_xlsx still retrieved spreadsheet chunks from {', '.join(sorted(set(leaks)))}"}
    return record


def run_gold_entry(rag, entry: dict, contexts: list, session_id: str, retries) -> dict:
    """Answer one question from its evidence chunks (an index snapshot) with the intent and answer agents.

    No guardrail, no search: the answer agent gets exactly the chunks that hold the evidence, so its
    score shows how well it answers when retrieval is perfect.
    """
    question = entry["item"]["question"]
    record = _base(entry, 1, question, rag.guardrail_enabled)
    metadata = [{"chunk_id": c["chunk_id"], "source_file": c["source_file"], "chunk_index": c.get("chunk_index"),
                 "score": None, "content": c["content"]} for c in contexts]
    state = {"session_id": session_id, "user_id": "", "current_question": question,
             "previous_question": "", "previous_answer": "",
             "retrieved_chunks": [c["content"] for c in contexts], "retrieved_metadata": metadata,
             "intent": "", "answer": "", "conversation_turn": 1, "guardrail_passed": True,
             "guardrail_reason": GOLD_GUARDRAIL_REASON, "retrieval_query": ""}
    retries.take()
    t0 = time.perf_counter()
    try:
        with watch_llm_calls() as timer, get_usage_metadata_callback() as usage:
            for node, agent in (("intent_identifier", rag.intent_agent), ("answer_generator", rag.answer_agent)):
                timer.default_node = node
                started = time.perf_counter()
                state = agent.execute(state)
                timer.seconds[node] = time.perf_counter() - started
    except Exception as e:
        record["error"] = {"type": type(e).__name__, "message": sanitize(e)}
        record["timings_s"] = {"total": round(time.perf_counter() - t0, 3)}
        record["openai_retries"] = retries.take()
        return record
    _finish(record, timer, usage, time.perf_counter() - t0, retries)
    record.update({"answer": state.get("answer") or "", "intent": state.get("intent") or "",
                   "guardrail_passed": True, "guardrail_reason": GOLD_GUARDRAIL_REASON,
                   "conversation_turn": 1, "retrieval_calls": 0})
    record["retrieved"] = _chunks(metadata, {c["chunk_id"]: c.get("file_type") for c in contexts})
    if not contexts:
        record["error"] = {"type": "no_gold_contexts", "message": "no snapshot chunk holds this item's evidence"}
    _flag_app_failures(record, timer.llm_errors)
    return record


def run_api_entry(client, entry: dict, session_id: str, top_k: int, retries, delay: float = 0.0,
                  guardrail_enabled: bool = True) -> list:
    """Post each turn of a question to /api/v1/query with one session_id; one record per turn."""
    records = []
    for turn_no, turn in enumerate(turns(entry["item"]), 1):
        if turn_no > 1 and delay:
            time.sleep(delay)
        record = _base(entry, turn_no, turn["question"], guardrail_enabled)
        retries.take()
        t0 = time.perf_counter()
        try:
            with api_retrieval_probe() as probe, watch_llm_calls() as timer, get_usage_metadata_callback() as usage:
                response = client.post(API_PATH, json={"question": turn["question"], "session_id": session_id,
                                                       "top_k": top_k})
        except Exception as e:
            record["error"] = {"type": type(e).__name__, "message": sanitize(e)}
            record["openai_retries"] = retries.take()
            records.append(record)
            continue
        wall = time.perf_counter() - t0
        _finish(record, timer, usage, wall, retries)
        if response.status_code != 200:
            record["error"] = {"type": f"HTTP {response.status_code}", "message": sanitize(response.text)}
            records.append(record)
            continue
        data = response.json()
        record.update({
            "answer": data.get("answer") or "",
            "intent": data.get("intent") or "",
            "guardrail_passed": data.get("guardrail_passed"),
            "guardrail_reason": sanitize(data.get("guardrail_reason") or "") or None,
            "input_guardrail": data.get("input_guardrail"),
            "input_guardrail_details": data.get("input_guardrail_details"),
            "output_guardrail": data.get("output_guardrail"),
            "output_guardrail_details": data.get("output_guardrail_details"),
            "conversation_turn": data.get("conversation_turn"),
            "retrieval_query": data.get("retrieval_query") or (probe.queries[0] if probe.queries else None),
            "retrieval_calls": probe.calls,
        })
        record["timings_s"]["app"] = round(data.get("processing_time_seconds") or wall, 3)
        record["retrieved"] = _chunks(data.get("retrieved_chunks") or [], _file_types(probe))
        _flag_app_failures(record, timer.llm_errors)
        if probe.error is not None and not record.get("error"):
            # The supervisor agent swallowed it and the API answered from no chunks: not a model failure
            record["error"] = {"type": "retrieval_error",
                               "message": f"{type(probe.error).__name__}: {sanitize(probe.error)}"}
        records.append(record)
    return records
