#!/usr/bin/env python3
"""Stage 1 of the evaluation: ask the gold questions and record what the app did.

    python scripts/run_eval.py --dry-run                                        # the plan; no Azure calls
    LLM_TEMPERATURE=0 python scripts/run_eval.py --only 1,8 --conditions full,no_xlsx   # smoke test
    LLM_TEMPERATURE=0 python scripts/run_eval.py --conditions full,no_xlsx              # full run

Writes logs/eval/<YYYYmmdd-HHMMSS>/records.jsonl (one line per question, turn, repeat and condition)
and run_config.json. Score a run with scripts/eval_score.py. docs/EVALUATION.md explains it all.

LLM_TEMPERATURE=0 makes runs repeatable: config/azure_config.py reads it from the environment, and
load_dotenv doesn't override it. The temperature in effect is recorded in run_config.json.
"""

import argparse
import json
import logging
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.gold import GOLD_FILE, build_plan, gold_sha256, load_gold, parse_ids, turns  # noqa: E402

# Rough cost of one run, per question, for --dry-run (rate limits seen on the deployments: gpt-4.1-mini
# 100 RPM / 100K TPM, ada-002 60K TPM; Content Safety F0 allows 5,000 text records a month)
SECONDS_ANSWER = 7.0     # guardrail + intent + answer calls and one search
SECONDS_REJECTED = 2.0   # the guardrail call only


def content_safety_configured() -> bool:
    """Both Content Safety settings non-empty, from the environment or .env (values are never shown)."""
    import os
    from dotenv import dotenv_values

    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    return bool((values.get("AZURE_CONTENT_SAFETY_ENDPOINT") or "").strip()
                and (values.get("AZURE_CONTENT_SAFETY_KEY") or "").strip())


def estimate(entries: list, cs_configured: bool, delay: float) -> dict:
    calls = {"chat": 0, "embedding": 0, "search": 0, "content_safety": 0}
    seconds = 0.0
    for entry in entries:
        expect = entry["item"]["expect"]
        for _ in turns(entry["item"]) if entry["route"] == "api" else [None]:
            if entry["route"] == "api" and cs_configured:
                calls["content_safety"] += 2  # moderation + Prompt Shields on the question
                if expect == "block_input":
                    seconds += 1.0
                    continue
            if expect == "answer":
                calls["chat"] += 3
                calls["embedding"] += 1
                calls["search"] += 1
                if entry["route"] == "api" and cs_configured:
                    calls["content_safety"] += 1  # moderation on the answer
                seconds += SECONDS_ANSWER
            else:
                calls["chat"] += 1
                seconds += SECONDS_REJECTED
        seconds += delay
    return {"calls": calls, "minutes": round(seconds / 60, 1)}


def print_plan(entries, notes, est, args, cs_configured):
    print("Evaluation plan (dry run: nothing is sent to Azure)")
    print(f"  gold file: {Path(args.set).resolve().relative_to(ROOT) if Path(args.set).resolve().is_relative_to(ROOT) else args.set}")
    print(f"  conditions: {', '.join(args.conditions)}; repeat: {args.repeat}; delay: {args.delay}s")
    print(f"  Content Safety: {'configured' if cs_configured else 'not configured (Q19 will be not checked)'}")
    print()
    for n, entry in enumerate(entries, 1):
        item = entry["item"]
        n_turns = len(turns(item)) if entry["route"] == "api" else 1
        print(f"  [{n:>2}] Q{item['id']:<2} {entry['route']:<5} {entry['condition']:<7} r{entry['repeat']}"
              f"  {item['expect']:<17} {'(' + str(n_turns) + ' turns) ' if n_turns > 1 else ''}"
              f"{item['question'][:60]}")
    for note in notes:
        print(f"  note: {note}")
    c = est["calls"]
    print()
    print(f"Estimated Azure use: {c['chat']} gpt-4.1-mini calls, {c['embedding']} embedding calls, "
          f"{c['search']} searches, {c['content_safety']} Content Safety calls, 0 Document Intelligence pages")
    print(f"Estimated time: about {est['minutes']} minutes (one question at a time, {args.delay}s apart)")


def git_state():
    def git(*cmd):
        try:
            done = subprocess.run(["git", "-C", str(ROOT), *cmd], capture_output=True, text=True, timeout=10)
            return done.stdout.strip() if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    commit = git("rev-parse", "--short", "HEAD")
    changes = git("status", "--porcelain")
    return {"commit": commit, "uncommitted_changes": bool(changes) if changes is not None else None}


def quiet_logging():
    """Keep the console to this script's progress lines (the app logs at INFO otherwise)."""
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    for handler in root.handlers:
        handler.setLevel(logging.ERROR)
    for name in ("httpx", "azure", "urllib3", "opencensus"):
        logging.getLogger(name).setLevel(logging.WARNING)


def describe(record) -> str:
    if record.get("error") and record["error"]["type"] not in ("filter_leak",):
        return f"ERROR {record['error']['type']}: {record['error']['message'][:80]}"
    if record.get("intent") == "blocked":
        return "blocked by Content Safety"
    if record.get("guardrail_passed") is False:
        return "rejected by the guardrail agent"
    sources = [c["source_file"] for c in record.get("retrieved") or []]
    workbooks = sum(1 for s in sources if (s or "").lower().endswith(".xlsx"))
    text = f"answered, {len(sources)} chunks" + (f" ({workbooks} from workbooks)" if workbooks else "")
    if record.get("error"):
        text += f"; WARNING {record['error']['type']}: {record['error']['message']}"
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--set", default=str(GOLD_FILE), help="gold questions file (default: %(default)s)")
    parser.add_argument("--only", help="ids and ranges to run, e.g. 1,8,17 or 1-15 (default: all)")
    parser.add_argument("--conditions", default="full",
                        help="full, no_xlsx or full,no_xlsx (the XLSX ablation). Default: full")
    parser.add_argument("--out", help="output folder (default: logs/eval/<YYYYmmdd-HHMMSS>)")
    parser.add_argument("--repeat", type=int, default=1, help="ask each question N times (default: 1)")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between questions (default: 2)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and estimate; no Azure calls")
    args = parser.parse_args(argv)
    args.conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    items = load_gold(args.set)
    try:
        entries, notes = build_plan(items, parse_ids(args.only), args.conditions, args.repeat)
    except ValueError as e:
        parser.error(str(e))
    if not entries:
        parser.error("nothing to run")
    cs_configured = content_safety_configured()
    est = estimate(entries, cs_configured, args.delay)
    if args.dry_run:
        print_plan(entries, notes, est, args, cs_configured)
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else ROOT / "logs" / "eval" / stamp
    if (ROOT / "datasets") in out.resolve().parents:
        parser.error("write runs under logs/eval/, never into datasets/")
    out.mkdir(parents=True, exist_ok=False)

    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.ERROR)
    quiet_logging()
    from evaluation.collect import FilterRejected, RetryCounter, run_api_entry, run_graph_entry

    from config import get_config
    from src.agents.rag_agent import MultiAgentRAG

    config = get_config()
    retries = RetryCounter().install()
    run_config = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": stamp,
        "git": git_state(),
        "index_name": config.search_index_name,
        "top_k": config.retrieval_top_k,
        "temperature": config.llm_temperature,
        "chat_deployment": config.ai_foundry_gpt4_deployment,
        "embedding_deployment": config.ai_foundry_embedding_deployment,
        "guardrail_enabled": config.guardrail_enabled,
        "guardrail_strictness": config.guardrail_strictness,
        "content_safety": {"configured": cs_configured, "input_check": config.input_guardrail_enabled,
                           "output_check": config.output_guardrail_enabled},
        "gold_file": {"path": str(Path(args.set).resolve().relative_to(ROOT))
                      if Path(args.set).resolve().is_relative_to(ROOT) else str(args.set),
                      "sha256": gold_sha256(args.set)},
        "conditions": args.conditions,
        "args": {k: v for k, v in vars(args).items()},
        "planned_records": sum(len(turns(e["item"])) if e["route"] == "api" else 1 for e in entries),
        "skipped": notes,
    }
    (out / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")
    print(f"Run {stamp}: {len(entries)} runs, temperature {config.llm_temperature}, "
          f"index {config.search_index_name}, writing to {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    if config.llm_temperature != 0:
        print(f"  note: temperature is {config.llm_temperature}; use LLM_TEMPERATURE=0 for repeatable runs")

    rag = MultiAgentRAG(top_k=config.retrieval_top_k, guardrail_enabled=config.guardrail_enabled,
                        guardrail_strictness=config.guardrail_strictness)
    client_cm = client = None
    if any(e["route"] == "api" for e in entries):
        from fastapi.testclient import TestClient
        from src.api.main import app
        quiet_logging()  # importing the API sets up INFO logging; keep its startup lines quiet too
        client_cm = TestClient(app)
        client = client_cm.__enter__()

    status, written, errors = 0, 0, 0
    try:
        with (out / "records.jsonl").open("a", encoding="utf-8") as sink:
            for n, entry in enumerate(entries, 1):
                if n > 1 and args.delay:
                    time.sleep(args.delay)
                item = entry["item"]
                try:
                    if entry["route"] == "graph":
                        sid = f"eval-{stamp}-q{item['id']}-r{entry['repeat']}-{entry['condition']}"
                        records = [run_graph_entry(rag, entry, sid, retries)]
                    else:
                        sid = f"eval-{stamp}-q{item['id']}-r{entry['repeat']}"
                        records = run_api_entry(client, entry, sid, config.retrieval_top_k, retries, args.delay)
                except FilterRejected as stop:
                    sink.write(json.dumps(stop.record, ensure_ascii=False) + "\n")
                    written += 1
                    print(f"STOPPED: Azure AI Search rejected the no_xlsx filter \"{stop.record['retrieval_filter']}\": "
                          f"{stop.record['error']['message']}", file=sys.stderr)
                    status = 2
                    break
                for record in records:
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                    written += 1
                    errors += bool(record.get("error"))
                    if record.get("error", {}) and record["error"]["type"] == "filter_leak":
                        print(f"WARNING: filter_leak in Q{item['id']}: {record['error']['message']}", file=sys.stderr)
                    turn = f" t{record['turn']}" if len(records) > 1 else ""
                    seconds = (record.get("timings_s") or {}).get("total")
                    print(f"[{n:>2}/{len(entries)}] Q{item['id']:<2} {entry['route']:<5} {entry['condition']:<7} "
                          f"r{entry['repeat']}{turn}: {describe(record)}"
                          + (f" ({seconds:.1f}s)" if seconds is not None else ""))
    except KeyboardInterrupt:
        print("Interrupted: the records written so far are kept.", file=sys.stderr)
        status = 130
    finally:
        if client_cm is not None:
            client_cm.__exit__(None, None, None)
        run_config.update({"finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "records": written, "records_with_errors": errors,
                           "stopped": {2: "filter_rejected", 130: "interrupted"}.get(status)})
        (out / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")

    rel = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"\n{written} records in {rel}/records.jsonl ({errors} with errors).")
    print(f"Next: python scripts/eval_score.py --run {rel}")
    return status


if __name__ == "__main__":
    sys.exit(main())
