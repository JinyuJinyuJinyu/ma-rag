"""Answer eval, stage 2 helpers: LLM-judged scores (RAGAS 0.4.3), judged by gpt-4.1-mini at temperature 0.

Judged by default (answer quality):
- faithfulness: are the answer's statements supported by the chunks it was given? Each chunk goes to the
  judge headed "[Source i: file]", as the answer agent saw it, so a citation is checkable, and the
  question is one more passage, so restating its premises doesn't count as made up.
- factual_correctness: RAGAS's F1 between the answer's claims and the reference's claims; and
  reference_coverage, from the same judge calls: the share of the reference's claims the answer states.
Optional (--metrics): response_relevancy, context_precision and context_recall. The last two judge
retrieval, which the retrieval eval measures exactly against the golden set's evidence.

The judge runs at temperature 0 with a fixed seed (best effort: Azure doesn't promise identical outputs)
and up to 4,096 output tokens: ragas's default of 1,024 cut long answers' verdict lists short. It talks to
the same Azure deployments as the app through the openai SDK's AsyncAzureOpenAI client, and a response
hook on that client counts requests, 429s and tokens.

ragas sends usage analytics to its own server unless RAGAS_DO_NOT_TRACK is "true", so this module sets
it before ragas is imported.
"""

import asyncio
import hashlib
import json
import math
import os
import time

from evaluation.collect import sanitize

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

ENV_NAMES = {
    "chat_endpoint": "AZURE_AI_FOUNDRY_GPT4_ENDPOINT",
    "chat_key": "AZURE_AI_FOUNDRY_GPT4_API_KEY",
    "chat_version": "AZURE_AI_FOUNDRY_GPT4_API_VERSION",
    "chat_deployment": "AZURE_AI_FOUNDRY_GPT4_DEPLOYMENT_NAME",
    "embed_endpoint": "AZURE_AI_FOUNDRY_EMBEDDING_ENDPOINT",
    "embed_key": "AZURE_AI_FOUNDRY_EMBEDDING_API_KEY",
    "embed_version": "AZURE_AI_FOUNDRY_EMBEDDING_API_VERSION",
    "embed_deployment": "AZURE_AI_FOUNDRY_EMBEDDING_DEPLOYMENT_NAME",
}
DEFAULT_METRICS = ("faithfulness", "factual_correctness")
ALL_METRICS = ("faithfulness", "factual_correctness", "response_relevancy", "context_precision", "context_recall")
# What each metric writes into a result (factual_correctness also yields reference_coverage)
METRIC_OUTPUTS = {"faithfulness": ("faithfulness",), "factual_correctness": ("factual_correctness", "reference_coverage"),
                  "response_relevancy": ("response_relevancy",), "context_precision": ("context_precision",),
                  "context_recall": ("context_recall",)}
JUDGE_SEED = 42
JUDGE_MAX_TOKENS = 4096
JUDGE_TIMEOUT_S = 120.0
QUESTION_PASSAGE = "[The question the user asked]"


def outputs(metrics) -> list[str]:
    return [name for metric in metrics for name in METRIC_OUTPUTS[metric]]


def calls_per_item(n_contexts: int, metrics=DEFAULT_METRICS) -> dict:
    """Judge calls for one answer: faithfulness 2, factual correctness 4 (claims both ways), response
    relevancy 3 (+2 embeddings), context precision one per retrieved chunk, context recall 1."""
    chat = {"faithfulness": 2, "factual_correctness": 4, "response_relevancy": 3,
            "context_precision": n_contexts, "context_recall": 1}
    return {"chat": sum(chat[m] for m in metrics), "embedding": 2 if "response_relevancy" in metrics else 0}


def read_settings(root) -> dict:
    """The judge's settings from the environment, then .env (like load_dotenv). Values are never printed."""
    from dotenv import dotenv_values

    values = {**dotenv_values(root / ".env"), **os.environ}
    settings = {key: (values.get(name) or "").strip() for key, name in ENV_NAMES.items()}
    missing = [ENV_NAMES[k] for k, v in settings.items() if not v]
    if missing:
        raise RuntimeError(f"set {', '.join(missing)} in .env (the same settings the app uses)")
    return settings


def judge_config(settings: dict) -> dict:
    """Everything that changes a judgment besides the sample itself (stored with each result). The metric
    list isn't part of it: each metric's result is kept on its own, so judging another metric later adds
    to an answer's results instead of replacing them."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        ragas_version = version("ragas")
    except PackageNotFoundError:  # scoring with --no-judge doesn't need ragas installed
        ragas_version = None
    return {"deployment": settings.get("chat_deployment"), "temperature": 0, "seed": JUDGE_SEED,
            "max_tokens": JUDGE_MAX_TOKENS, "ragas": ragas_version}


def fingerprint(sample: dict, config: dict) -> str:
    """A hash of what was judged: the question, answer, chunks, reference and judge settings.

    A cached result is reused only while its fingerprint matches, so editing a reference or the judge
    settings re-judges the answers it affects."""
    payload = {k: sample.get(k) for k in ("question", "answer", "contexts", "reference")}
    payload["config"] = config
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class JudgeUsage:
    """Counts the judge's HTTP requests, 429s and tokens from each response's `usage`."""

    def __init__(self):
        self.chat_requests = self.embedding_requests = self.throttled = self.truncated = 0
        self.prompt_tokens = self.completion_tokens = self.total_tokens = 0

    async def on_response(self, response):
        path = response.request.url.path
        if path.endswith("/chat/completions"):
            self.chat_requests += 1
        elif path.endswith("/embeddings"):
            self.embedding_requests += 1
        if response.status_code == 429:
            self.throttled += 1
        if response.status_code == 200:
            await response.aread()
            try:
                body = response.json()
            except ValueError:
                body = {}
            usage = body.get("usage") or {}
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
            self.total_tokens += usage.get("total_tokens", 0)
            if any((c or {}).get("finish_reason") == "length" for c in body.get("choices") or []):
                self.truncated += 1

    def as_dict(self) -> dict:
        return dict(vars(self))


class CorrectnessAndCoverage:
    """RAGAS FactualCorrectness (F1), plus the share of the reference's claims the answer states.

    FactualCorrectness splits the answer into claims and checks them against the reference, then splits
    the reference and checks it against the answer. Its F1 mixes the two sides (its recall counts the
    answer's claims), so a complete answer that adds true detail scores low. The reference side alone is
    a clearer measure of "did the answer say what it should", so both are kept, from the same four calls.
    Uses the metric's claim helpers, so it is tied to ragas 0.4.x (the offline tests pin it).
    """

    def __init__(self, llm):
        from ragas.metrics.collections import FactualCorrectness

        self.metric = FactualCorrectness(llm=llm, mode="f1")

    async def ascore(self, response: str, reference: str) -> dict:
        from ragas.metrics.utils import fbeta_score

        answer_side = await self.metric._decompose_and_verify_claims(response, reference)
        reference_side = await self.metric._decompose_and_verify_claims(reference, response)
        tp, fp = int(answer_side.sum()), int((~answer_side).sum())
        fn = int((~reference_side).sum())
        coverage = float(reference_side.mean()) if reference_side.size else None
        return {"factual_correctness": round(float(fbeta_score(tp, fp, fn, 1.0)), 4),
                "reference_coverage": None if coverage is None else round(coverage, 4)}


def build_metrics(settings: dict, usage: JudgeUsage, metrics=DEFAULT_METRICS, transport=None,
                  timeout: float = JUDGE_TIMEOUT_S, max_retries: int = 6):
    """The metric objects plus the clients to close afterwards. `transport` is for offline tests."""
    from openai import AsyncAzureOpenAI, DefaultAsyncHttpxClient
    from ragas.llms import llm_factory
    from ragas.metrics.collections import AnswerRelevancy, ContextPrecisionWithReference, ContextRecall, Faithfulness

    def client(endpoint, key, version):
        http = DefaultAsyncHttpxClient(event_hooks={"response": [usage.on_response]},
                                       **({"transport": transport} if transport else {}))
        # The SDK retries 429s itself, honouring Retry-After
        return AsyncAzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=version,
                                max_retries=max_retries, timeout=timeout, http_client=http)

    chat = client(settings["chat_endpoint"], settings["chat_key"], settings["chat_version"])
    clients = [chat]
    llm = llm_factory(settings["chat_deployment"], provider="openai", client=chat, temperature=0,
                      seed=JUDGE_SEED, max_tokens=JUDGE_MAX_TOKENS)
    built = {}
    for name in metrics:
        if name == "faithfulness":
            built[name] = Faithfulness(llm=llm)
        elif name == "factual_correctness":
            built[name] = CorrectnessAndCoverage(llm)
        elif name == "response_relevancy":
            from ragas.embeddings import OpenAIEmbeddings
            embed = client(settings["embed_endpoint"], settings["embed_key"], settings["embed_version"])
            clients.append(embed)
            built[name] = AnswerRelevancy(llm=llm, embeddings=OpenAIEmbeddings(client=embed,
                                                                                model=settings["embed_deployment"]),
                                          strictness=3)
        elif name == "context_precision":
            built[name] = ContextPrecisionWithReference(llm=llm)
        elif name == "context_recall":
            built[name] = ContextRecall(llm=llm)
        else:
            raise ValueError(f"unknown judge metric {name!r}; choose from {', '.join(ALL_METRICS)}")
    return built, clients


def passages(sample: dict) -> list[str]:
    """The chunks as the answer agent saw them: each headed "[Source i: file]", in rank order."""
    out = []
    for i, chunk in enumerate(sample.get("contexts") or [], 1):
        if isinstance(chunk, dict):
            out.append(f"[Source {i}: {chunk.get('source_file') or 'unknown'}]\n{chunk.get('content') or ''}")
        else:
            out.append(str(chunk))
    return out


def metric_inputs(sample: dict, metrics=DEFAULT_METRICS) -> dict:
    """Each metric's arguments."""
    q, a, ref, ctx = sample["question"], sample["answer"], sample["reference"], passages(sample)
    inputs = {
        "faithfulness": {"user_input": q, "response": a,
                         "retrieved_contexts": ctx + [f"{QUESTION_PASSAGE}\n{q}"] if ctx else []},
        "factual_correctness": {"response": a, "reference": ref},
        "response_relevancy": {"user_input": q, "response": a},
        "context_precision": {"user_input": q, "reference": ref, "retrieved_contexts": ctx},
        "context_recall": {"user_input": q, "retrieved_contexts": ctx, "reference": ref},
    }
    return {name: inputs[name] for name in metrics}


def _error_text(e: Exception) -> str:
    text = f"{type(e).__name__}: {sanitize(e, 300)}"
    if "max_tokens" in text or "length limit" in text:
        text = "output truncated (max_tokens): " + text
    return text


async def judge_sample(metrics: dict, sample: dict) -> dict:
    """Score one answer. A metric that fails is stored as None with the reason, never as 0."""
    scores, errors = {}, {}
    started = time.perf_counter()
    for name, kwargs in metric_inputs(sample, tuple(metrics)).items():
        names = METRIC_OUTPUTS[name]
        if "retrieved_contexts" in kwargs and not kwargs["retrieved_contexts"]:
            for out in names:
                scores[out], errors[out] = None, "no retrieved chunks"
            continue
        try:
            result = await metrics[name].ascore(**kwargs)
            values = result if isinstance(result, dict) else {name: getattr(result, "value", result)}
            for out in names:
                value = values.get(out)
                value = float(value) if value is not None else None
                if value is None or math.isnan(value):
                    scores[out], errors[out] = None, "the judge returned no score"
                else:
                    scores[out] = round(value, 4)
        except Exception as e:  # noqa: BLE001 - a judge failure must not stop the other metrics
            for out in names:
                scores[out], errors[out] = None, _error_text(e)
    return {"ragas": scores, "ragas_errors": errors, "judge_seconds": round(time.perf_counter() - started, 1)}


async def judge_all(samples: list, settings: dict, metrics=DEFAULT_METRICS, concurrency: int = 1, on_result=None,
                    transport=None) -> dict:
    """Judge samples with at most `concurrency` answers in flight; on_result(sample, result) after each."""
    usage = JudgeUsage()
    built, clients = build_metrics(settings, usage, metrics, transport=transport)
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(sample):
        async with gate:
            result = await judge_sample(built, sample)
            if on_result:
                on_result(sample, result)
            return result

    try:
        await asyncio.gather(*(one(s) for s in samples))
    finally:
        for c in clients:
            await c.close()
    return usage.as_dict()
