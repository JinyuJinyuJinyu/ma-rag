"""Stage 2 helpers: the five RAGAS scores, judged by gpt-4.1-mini at temperature 0.

Uses the ragas 0.4 "collections" metrics (the SingleTurnSample API is legacy). The judge talks to the
same Azure deployments as the app, through the openai SDK's AsyncAzureOpenAI client, so no LiteLLM is
needed. A response hook on that client counts requests, 429s and tokens.

ragas sends usage analytics to its own server unless RAGAS_DO_NOT_TRACK is "true", so this module sets
it before ragas is imported.
"""

import asyncio
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


def calls_per_item(n_contexts: int) -> dict:
    """Judge calls for one answer: faithfulness 2, response relevancy 3 (+2 embeddings),
    context precision one per retrieved chunk, context recall 1, factual correctness (F1) 4."""
    return {"chat": 2 + 3 + n_contexts + 1 + 4, "embedding": 2}


def read_settings(root) -> dict:
    """The judge's settings from the environment, then .env (like load_dotenv). Values are never printed."""
    from dotenv import dotenv_values

    values = {**dotenv_values(root / ".env"), **os.environ}
    settings = {key: (values.get(name) or "").strip() for key, name in ENV_NAMES.items()}
    missing = [ENV_NAMES[k] for k, v in settings.items() if not v]
    if missing:
        raise RuntimeError(f"set {', '.join(missing)} in .env (the same settings the app uses)")
    return settings


class JudgeUsage:
    """Counts the judge's HTTP requests, 429s and tokens from each response's `usage`."""

    def __init__(self):
        self.chat_requests = self.embedding_requests = self.throttled = 0
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
                usage = response.json().get("usage") or {}
            except ValueError:
                usage = {}
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
            self.total_tokens += usage.get("total_tokens", 0)

    def as_dict(self) -> dict:
        return dict(vars(self))


def build_metrics(settings: dict, usage: JudgeUsage, transport=None, timeout: float = 60.0, max_retries: int = 6):
    """The five metric objects plus the clients to close afterwards. `transport` is for offline tests."""
    from openai import AsyncAzureOpenAI, DefaultAsyncHttpxClient
    from ragas.embeddings import OpenAIEmbeddings
    from ragas.llms import llm_factory
    from ragas.metrics.collections import (AnswerRelevancy, ContextPrecisionWithReference, ContextRecall,
                                           FactualCorrectness, Faithfulness)

    def client(endpoint, key, version):
        http = DefaultAsyncHttpxClient(event_hooks={"response": [usage.on_response]},
                                       **({"transport": transport} if transport else {}))
        # The SDK retries 429s itself, honouring Retry-After
        return AsyncAzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=version,
                                max_retries=max_retries, timeout=timeout, http_client=http)

    chat = client(settings["chat_endpoint"], settings["chat_key"], settings["chat_version"])
    embed = client(settings["embed_endpoint"], settings["embed_key"], settings["embed_version"])
    llm = llm_factory(settings["chat_deployment"], provider="openai", client=chat, temperature=0)
    embeddings = OpenAIEmbeddings(client=embed, model=settings["embed_deployment"])
    metrics = {
        "faithfulness": Faithfulness(llm=llm),
        "response_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings, strictness=3),
        "context_precision": ContextPrecisionWithReference(llm=llm),
        "context_recall": ContextRecall(llm=llm),
        "factual_correctness": FactualCorrectness(llm=llm, mode="f1"),
    }
    return metrics, (chat, embed)


def metric_inputs(sample: dict) -> dict:
    """Each metric's arguments. The contexts are the chunks in rank order, as the answer agent saw them."""
    q, a, ref, ctx = sample["question"], sample["answer"], sample["reference"], sample["contexts"]
    return {
        "faithfulness": {"user_input": q, "response": a, "retrieved_contexts": ctx},
        "response_relevancy": {"user_input": q, "response": a},
        "context_precision": {"user_input": q, "reference": ref, "retrieved_contexts": ctx},
        "context_recall": {"user_input": q, "retrieved_contexts": ctx, "reference": ref},
        "factual_correctness": {"response": a, "reference": ref},
    }


async def judge_sample(metrics: dict, sample: dict) -> dict:
    """Score one answer. A metric that fails is stored as None with the reason, never as 0."""
    scores, errors = {}, {}
    started = time.perf_counter()
    for name, kwargs in metric_inputs(sample).items():
        if "retrieved_contexts" in kwargs and not kwargs["retrieved_contexts"]:
            scores[name], errors[name] = None, "no retrieved chunks"
            continue
        try:
            result = await metrics[name].ascore(**kwargs)
            value = getattr(result, "value", result)
            value = float(value) if value is not None else None
            if value is None or math.isnan(value):
                scores[name], errors[name] = None, "the judge returned no score (NaN)"
            else:
                scores[name] = round(value, 4)
        except Exception as e:  # noqa: BLE001 - a judge failure must not stop the other metrics
            scores[name], errors[name] = None, f"{type(e).__name__}: {sanitize(e, 160)}"
    return {"ragas": scores, "ragas_errors": errors, "judge_seconds": round(time.perf_counter() - started, 1)}


async def judge_all(samples: list, settings: dict, concurrency: int = 1, on_result=None, transport=None) -> dict:
    """Judge samples with at most `concurrency` answers in flight; on_result(sample, result) after each."""
    usage = JudgeUsage()
    metrics, clients = build_metrics(settings, usage, transport=transport)
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(sample):
        async with gate:
            result = await judge_sample(metrics, sample)
            if on_result:
                on_result(sample, result)
            return result

    try:
        await asyncio.gather(*(one(s) for s in samples))
    finally:
        for c in clients:
            await c.close()
    return usage.as_dict()
