#!/usr/bin/env python3
"""The answer eval: ask the golden-set questions through the app, then score the answers.

    python scripts/eval_answers.py run --dry-run                                     # the plan; no Azure calls
    LLM_TEMPERATURE=0 python scripts/eval_answers.py run --only 1,8 --conditions full,no_xlsx   # smoke test
    LLM_TEMPERATURE=0 python scripts/eval_answers.py run --conditions full,no_xlsx,gold         # full run
    python scripts/eval_answers.py score --run logs/eval/answers/<run> --dry-run     # what would be judged
    python scripts/eval_answers.py score --run logs/eval/answers/<run> --no-judge    # exact scores only
    python scripts/eval_answers.py score --run logs/eval/answers/<run>               # plus the LLM judge
    python scripts/eval_answers.py compare --base logs/eval/answers/<baseline> --run logs/eval/answers/<run>

`run` writes logs/eval/answers/<YYYYmmdd-HHMMSS>/records.jsonl (one line per question, turn, repeat and
condition) and run_config.json. Conditions: full (the app as is), no_xlsx (spreadsheet chunks hidden from
search: the XLSX ablation) and gold (the answer agent gets the evidence chunks from an index snapshot made
with `scripts/eval_retrieval.py snapshot`: perfect retrieval; a vertical question gets every row's chunk).

`score` scores a run without asking again: the exact checks always, and unless --no-judge the RAGAS judge
on the core answers. Judge results are kept in ragas.jsonl with a fingerprint of what was judged, so
scoring again only judges what's new or changed. docs/EVALUATION.md explains every score.

`compare` pairs two scored runs question by question (fixed, broke, still failing, still correct), for a run
on a subset (`run --only` a baseline's failures) whose percentages can't be set against the baseline's. It
prints the comparison and writes compare-<baseline>.md in the run's folder.

LLM_TEMPERATURE=0 makes the agents' answers vary less between runs (config/azure_config.py reads it from
the environment, and load_dotenv doesn't override it). The temperature in effect is in run_config.json.
"""

import argparse
import asyncio
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

from evaluation.gold import (GOLD_CONTEXT_CHUNKS, GOLD_FILE, VERTICAL_MAX_UNITS, GoldError,  # noqa: E402
                             build_plan, gold_context_chunks, gold_sha256, is_behaviour, is_vertical, load_gold,
                             parse_ids, turns)

# Rough cost per question for --dry-run (rate limits seen on the deployments: gpt-4.1-mini 100 RPM / 100K TPM,
# ada-002 60K TPM; Content Safety F0 allows 5,000 text records a month)
SECONDS_ANSWER = 7.0     # guardrail + intent + answer calls and one search
SECONDS_GOLD = 4.0       # intent + answer calls
SECONDS_REJECTED = 2.0   # the guardrail call only
RUNS_DIR = ROOT / "logs" / "eval" / "answers"


def rel(path: Path) -> str:
    path = Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def resolve(path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def safe_out(parser, path) -> Path:
    """An output folder that git won't track: the records quote the companies' IR documents."""
    path = resolve(path).resolve()
    if not path.is_relative_to(ROOT):
        return path
    try:
        ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(path)],
                                 capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ignored = (ROOT / "logs") in path.parents
    if not ignored:
        parser.error(f"{rel(path)} isn't git-ignored: write runs under logs/eval/ (or outside the repo), "
                     "because they quote the companies' IR documents")
    return path


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
        item = entry["item"]
        if entry["condition"] == "gold":
            calls["chat"] += 2
            seconds += SECONDS_GOLD + delay
            continue
        for n, _ in enumerate(turns(item) if entry["route"] == "api" else [None], 1):
            if entry["route"] == "api" and cs_configured:
                calls["content_safety"] += 2  # moderation + Prompt Shields on the question
                if item["expect"] == "block_input":
                    seconds += 1.0
                    continue
            if is_behaviour(item):
                calls["chat"] += 1
                seconds += SECONDS_REJECTED
                continue
            calls["chat"] += 3 + (1 if n > 1 else 0)  # a follow-up turn is rewritten before the search
            calls["embedding"] += 1
            calls["search"] += 1
            if entry["route"] == "api" and cs_configured:
                calls["content_safety"] += 2  # moderation on the answer, and on the guardrail reason + search query
            seconds += SECONDS_ANSWER
        seconds += delay
    return {"calls": calls, "minutes": round(seconds / 60, 1)}


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


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def find_run(parser, path) -> Path:
    """A run folder named on the command line, from the working directory or else the project root."""
    run_dir = Path(path)
    if not run_dir.is_absolute():
        run_dir = (Path.cwd() / run_dir) if (Path.cwd() / run_dir).exists() else ROOT / run_dir
    if not (run_dir / "run_config.json").exists():
        parser.error(f"no run at {rel(run_dir)} (it has no run_config.json)")
    return run_dir


# ---- run ------------------------------------------------------------------------------------------

def describe(record) -> str:
    error = record.get("error") or {}
    if error and error["type"] != "filter_leak":
        return f"ERROR {error['type']}: {error['message'][:80]}"
    if record.get("intent") == "blocked":
        return "blocked by Content Safety"
    if record.get("guardrail_passed") is False:
        return "rejected by the guardrail agent"
    chunks = record.get("retrieved") or []
    sheets = sum(1 for c in chunks if (c.get("file_type") or "") in ("xlsx", "csv")
                 or (c.get("source_file") or "").lower().endswith((".xlsx", ".csv")))
    text = f"answered, {len(chunks)} chunks" + (f" ({sheets} from spreadsheets)" if sheets else "")
    if record["condition"] == "gold":
        text += " (gold contexts)"
    if error:
        text += f"; WARNING {error['type']}: {error['message']}"
    return text


def cmd_run(args, parser) -> int:
    args.conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    args.set = resolve(args.set or GOLD_FILE)
    items = list(load_items(parser, args.set)[0].values())
    try:
        entries, notes = build_plan(items, parse_ids(args.only), args.conditions, args.repeat)
    except ValueError as e:
        parser.error(str(e))
    if not entries:
        parser.error("nothing to run")

    snapshot, contexts = None, {}
    if "gold" in args.conditions:
        from evaluation.snapshot import SNAPSHOT_FILE, evidence_chunks, load_snapshot
        path = resolve(args.snapshot or SNAPSHOT_FILE)
        if not path.exists():
            parser.error(f"the gold condition needs an index snapshot ({rel(path)}): "
                         "run `python scripts/eval_retrieval.py snapshot` first")
        try:
            snapshot = {"path": rel(path), "chunks": load_snapshot(path)}
        except ValueError as e:  # not a snapshot file
            parser.error(str(e))
        for entry in entries:
            if entry["condition"] == "gold":
                units = turns(entry["item"])[0].get("evidence") or []
                contexts[entry["item"]["id"]] = evidence_chunks(units, snapshot["chunks"],
                                                                k=gold_context_chunks(entry["item"]))
    vertical_gold = sorted({e["item"]["id"] for e in entries if e["condition"] == "gold" and is_vertical(e["item"])})

    out = safe_out(parser, args.out) if args.out else None  # checked before a dry run too
    cs_configured = content_safety_configured()
    est = estimate(entries, cs_configured, args.delay)
    if args.dry_run:
        print("Answer eval plan (dry run: nothing is sent to Azure)")
        print(f"  golden set: {rel(args.set)}")
        print(f"  conditions: {', '.join(args.conditions)}; repeat: {args.repeat}; delay: {args.delay}s")
        print(f"  Content Safety: {'configured' if cs_configured else 'not configured (block_input checks will be not checked)'}")
        if snapshot:
            empty = [i for i, c in contexts.items() if not c]
            print(f"  gold contexts from {snapshot['path']} ({len(snapshot['chunks'])} chunks)"
                  + (f"; no evidence chunk found for Q{', Q'.join(map(str, empty))}" if empty else ""))
            print(f"  gold contexts: up to {GOLD_CONTEXT_CHUNKS} chunks per question; every row's chunk (up to "
                  f"{VERTICAL_MAX_UNITS}) for a vertical question"
                  + (f" (Q{', Q'.join(map(str, vertical_gold))})" if vertical_gold else ""))
        print()
        for n, entry in enumerate(entries, 1):
            item = entry["item"]
            n_turns = len(turns(item)) if entry["route"] == "api" else 1
            print(f"  [{n:>3}] Q{item['id']:<3} {entry['route']:<5} {entry['condition']:<7} r{entry['repeat']}"
                  f"  {item['expect']:<17} {'(' + str(n_turns) + ' turns) ' if n_turns > 1 else ''}{item['question'][:60]}")
        for note in notes:
            print(f"  note: {note}")
        c = est["calls"]
        print()
        print(f"Estimated Azure use: {c['chat']} gpt-4.1-mini calls, {c['embedding']} embedding calls, "
              f"{c['search']} searches, {c['content_safety']} Content Safety calls, 0 Document Intelligence pages")
        print(f"Estimated time: about {est['minutes']} minutes (one question at a time, {args.delay}s apart)")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out or RUNS_DIR / stamp
    if out.exists():
        parser.error(f"{rel(out)} already exists; choose another --out")
    out.mkdir(parents=True)

    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.ERROR)
    quiet_logging()
    from evaluation.collect import FilterRejected, RetryCounter, run_api_entry, run_gold_entry, run_graph_entry

    from config import get_config
    from src.agents.rag_agent import MultiAgentRAG

    config = get_config()
    retries = RetryCounter().install()
    run_config = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": stamp, "eval": "answers",
        "git": git_state(),
        "index_name": config.search_index_name,
        "top_k": config.retrieval_top_k,
        "hybrid_knn": config.retrieval_hybrid_knn,
        "workbook_expand": config.retrieval_workbook_expand,
        "workbook_trigger": config.retrieval_workbook_trigger,
        "company_routing": config.retrieval_company_routing,
        "single_company_top_k": config.retrieval_single_company_top_k,
        "temperature": config.llm_temperature,
        "chat_deployment": config.ai_foundry_gpt4_deployment,
        "embedding_deployment": config.ai_foundry_embedding_deployment,
        "guardrail_enabled": config.guardrail_enabled,
        "guardrail_strictness": config.guardrail_strictness,
        "content_safety": {"configured": cs_configured, "input_check": config.input_guardrail_enabled,
                           "output_check": config.output_guardrail_enabled},
        "gold_file": {"path": rel(args.set), "sha256": gold_sha256(args.set)},
        "snapshot": {"path": snapshot["path"], "chunks": len(snapshot["chunks"])} if snapshot else None,
        "gold_context_chunks": GOLD_CONTEXT_CHUNKS if snapshot else None,
        "gold_context_chunks_vertical": VERTICAL_MAX_UNITS if snapshot else None,
        "conditions": args.conditions,
        "args": {k: (rel(v) if k == "set" else v) for k, v in vars(args).items() if k != "func"},
        "planned_records": sum(len(turns(e["item"])) if e["route"] == "api" else 1 for e in entries),
        "skipped": notes,
    }
    (out / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")
    print(f"Run {stamp}: {len(entries)} runs, temperature {config.llm_temperature}, "
          f"index {config.search_index_name}, writing to {rel(out)}")
    if config.llm_temperature != 0:
        print(f"  note: temperature is {config.llm_temperature}; LLM_TEMPERATURE=0 makes answers vary less")
    if snapshot and config.retrieval_top_k != GOLD_CONTEXT_CHUNKS:
        print(f"  note: RETRIEVAL_TOP_K is {config.retrieval_top_k}, but the gold condition hands the answer agent up "
              f"to {GOLD_CONTEXT_CHUNKS} chunks (a vertical question every row's chunk, up to {VERTICAL_MAX_UNITS}): "
              "the two conditions' contexts differ in size")
    elif snapshot and vertical_gold:
        print(f"  note: under gold, a vertical question's context holds every row's chunk (up to {VERTICAL_MAX_UNITS}), "
              f"more than RETRIEVAL_TOP_K ({config.retrieval_top_k})")

    rag = MultiAgentRAG(top_k=config.retrieval_top_k, guardrail_enabled=config.guardrail_enabled,
                        guardrail_strictness=config.guardrail_strictness)
    client_cm = client = None
    if any(e["route"] == "api" for e in entries):
        from fastapi.testclient import TestClient
        from src.api.main import app
        quiet_logging()  # importing the API sets up INFO logging; keep its startup lines quiet too
        client_cm = TestClient(app)
        client = client_cm.__enter__()

    status, stopped, written, errors = 0, None, 0, 0
    try:
        with (out / "records.jsonl").open("a", encoding="utf-8") as sink:
            for n, entry in enumerate(entries, 1):
                if n > 1 and args.delay:
                    time.sleep(args.delay)
                item = entry["item"]
                sid = f"eval-{stamp}-q{item['id']}-r{entry['repeat']}-{entry['condition']}"
                try:
                    if entry["condition"] == "gold":
                        records = [run_gold_entry(rag, entry, contexts.get(item["id"]) or [], sid, retries)]
                    elif entry["route"] == "graph":
                        records = [run_graph_entry(rag, entry, sid, retries)]
                    else:
                        records = run_api_entry(client, entry, sid, config.retrieval_top_k, retries, args.delay,
                                                guardrail_enabled=config.guardrail_enabled)
                except FilterRejected as stop:
                    sink.write(json.dumps(stop.record, ensure_ascii=False) + "\n")
                    written += 1
                    errors += 1
                    print(f"STOPPED: Azure AI Search rejected the no_xlsx filter \"{stop.record['retrieval_filter']}\": "
                          f"{stop.record['error']['message']}", file=sys.stderr)
                    status, stopped = 2, "filter_rejected"
                    break
                for record in records:
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                    written += 1
                    errors += bool(record.get("error"))
                    if (record.get("error") or {}).get("type") == "filter_leak":
                        print(f"WARNING: filter_leak in Q{item['id']}: {record['error']['message']}", file=sys.stderr)
                    turn = f" t{record['turn']}" if len(records) > 1 else ""
                    seconds = (record.get("timings_s") or {}).get("total")
                    print(f"[{n:>3}/{len(entries)}] Q{item['id']:<3} {entry['route']:<5} {entry['condition']:<7} "
                          f"r{entry['repeat']}{turn}: {describe(record)}" + (f" ({seconds:.1f}s)" if seconds is not None else ""))
    except KeyboardInterrupt:
        print("Interrupted: the records written so far are kept.", file=sys.stderr)
        status, stopped = 130, "interrupted"
    except Exception as e:  # keep what was written, say why the run stopped, then fail loudly
        status, stopped = 1, f"crashed: {type(e).__name__}"
        raise
    finally:
        if client_cm is not None:
            client_cm.__exit__(None, None, None)
        run_config.update({"finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "records": written, "records_with_errors": errors, "stopped": stopped})
        (out / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")

    print(f"\n{written} records in {rel(out)}/records.jsonl ({errors} with errors).")
    print(f"Next: python scripts/eval_answers.py score --run {rel(out)}")
    return status


# ---- score ----------------------------------------------------------------------------------------

def judge_samples(records, items, judge_on, only) -> list:
    """Answers the judge should score: the core answers (single-turn, graph route) of the chosen conditions,
    with a non-empty answer that wasn't rejected and a run that didn't fail."""
    from evaluation.report import is_core, record_key
    from evaluation.scoring import false_refusal, record_failed

    conditions = ("full",) if judge_on == "full" else ("full", "no_xlsx", "gold")
    samples = []
    for record in records:
        item = items.get(record["id"])
        if not item or not is_core(record, item) or record["condition"] not in conditions:
            continue
        if (only and record["id"] not in only) or record_failed(record) or false_refusal(record):
            continue
        samples.append({
            "key": "|".join(map(str, record_key(record))), "id": record["id"], "condition": record["condition"],
            "question": record["question"], "answer": record["answer"],
            "reference": turns(item)[record["turn"] - 1]["reference"],
            "contexts": [{"source_file": c.get("source_file"), "content": c.get("content") or ""}
                         for c in record.get("retrieved") or []],
        })
    return samples


def load_items(parser, path: Path, fallback: bool = False) -> tuple:
    """The golden set at `path` as {id: item}, and the path used. With fallback, a run's own golden set
    that is gone or no longer loads (an older run's file) gives way to the current one, with a note."""
    try:
        return {item["id"]: item for item in load_gold(path)}, path
    except (FileNotFoundError, GoldError) as e:
        if not fallback or Path(path).resolve() == GOLD_FILE.resolve():
            parser.error(f"{rel(path)}: {e}" if isinstance(e, GoldError) else f"no golden set at {rel(path)}")
        print(f"note: {rel(path)} can't be used ({'missing' if isinstance(e, FileNotFoundError) else 'old format'}); "
              f"scoring with {rel(GOLD_FILE)}", file=sys.stderr)
        return {item["id"]: item for item in load_gold(GOLD_FILE)}, GOLD_FILE


def cmd_score(args, parser) -> int:
    from evaluation.judge import (ALL_METRICS, DEFAULT_METRICS, METRIC_OUTPUTS, calls_per_item, fingerprint,
                                  judge_config, outputs)
    from evaluation.report import build_summary, record_key, render_markdown, score_record

    run_dir = find_run(parser, args.run)
    run_config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    records = read_jsonl(run_dir / "records.jsonl")
    if not records:
        parser.error(f"no records in {rel(run_dir / 'records.jsonl')}")
    if args.set:
        items, gold_path = load_items(parser, resolve(args.set))
    else:
        items, gold_path = load_items(parser, resolve(run_config["gold_file"]["path"]), fallback=True)
    if gold_sha256(gold_path) != run_config["gold_file"].get("sha256"):
        print(f"note: the golden set changed since this run; scoring (and judging) use {rel(gold_path)} as it is now",
              file=sys.stderr)
    unknown = sorted({r["id"] for r in records} - set(items))
    if unknown:
        print(f"note: no golden-set item for Q{', Q'.join(map(str, unknown))}; those records are left out", file=sys.stderr)
        records = [r for r in records if r["id"] in items]
    # A record is scored only against the question it asked: an item reworded since the run (or an id
    # reused) would otherwise be scored on another question's facts
    changed = sorted({r["id"] for r in records if r["turn"] > len(turns(items[r["id"]]))
                      or turns(items[r["id"]])[r["turn"] - 1]["question"] != r["question"]})
    if changed:
        print(f"note: Q{', Q'.join(map(str, changed))} changed since this run (another question); those records "
              "are left out", file=sys.stderr)
        records = [r for r in records if r["id"] not in changed]

    metrics = tuple(m.strip() for m in (args.metrics or ",".join(DEFAULT_METRICS)).split(",") if m.strip())
    unknown_metrics = set(metrics) - set(ALL_METRICS)
    if unknown_metrics:
        parser.error(f"unknown metric(s): {', '.join(sorted(unknown_metrics))}; choose from {', '.join(ALL_METRICS)}")
    only = parse_ids(args.only)
    from evaluation.judge import read_settings
    settings = None
    if not args.no_judge and not args.dry_run:
        try:
            settings = read_settings(ROOT)
        except RuntimeError as e:
            parser.error(str(e))
    config = judge_config(settings or {"chat_deployment": run_config.get("chat_deployment")})

    # Every answer the judge could score, in every condition, with a fingerprint of what it would judge:
    # a cached judgment counts only while its fingerprint matches (same answer, chunks, reference and judge
    # settings). --only and --judge-on choose what to judge now; they never hide a valid judgment.
    eligible = judge_samples(records, items, "all", None)
    for sample in eligible:
        sample["fingerprint"] = fingerprint(sample, config)
    by_key = {sample["key"]: sample for sample in eligible}
    cache = {e["key"]: e for e in read_jsonl(run_dir / "ragas.jsonl")}

    def valid(key):
        entry, sample = cache.get(key), by_key.get(key)
        return entry if entry and sample and entry.get("fingerprint") == sample["fingerprint"] else None

    wanted = outputs(metrics)
    conditions = ("full",) if args.judge_on == "full" else ("full", "no_xlsx", "gold")
    selected = [x for x in eligible if x["condition"] in conditions and (not only or x["id"] in only)]
    plan = []  # (sample, the metrics it still needs)
    if not args.no_judge:
        for sample in selected:
            entry = valid(sample["key"])
            have = (entry or {}).get("ragas") or {}
            need = tuple(m for m in metrics if args.rejudge or entry is None
                         or any(have.get(o) is None for o in METRIC_OUTPUTS[m]))
            if need:
                plan.append((sample, need))

    if args.dry_run:
        chat = sum(calls_per_item(len(x["contexts"]), need)["chat"] for x, need in plan)
        emb = sum(calls_per_item(len(x["contexts"]), need)["embedding"] for x, need in plan)
        print(f"Scoring plan for {run_dir.name} (dry run: nothing is sent to Azure)")
        print(f"  records: {len(records)}; exact scores for all of them")
        if args.no_judge:
            print("  judge: off (--no-judge)")
        else:
            print(f"  judge ({', '.join(metrics)}) on {args.judge_on}: {len(selected)} answers, "
                  f"{len(selected) - len(plan)} already judged, {len(plan)} to judge")
            print(f"  Estimated judge use: {chat} gpt-4.1-mini calls and {emb} embedding calls, "
                  f"about {round(len(plan) * 0.4, 1)} minutes at concurrency {args.concurrency}")
        return 0

    usage = None
    if plan:
        from evaluation.judge import judge_all
        warnings.filterwarnings("ignore")
        done = {"n": 0}
        sink = (run_dir / "ragas.jsonl").open("a", encoding="utf-8")

        def save(sample, result):
            # New scores join the answer's valid judgment; other metrics' scores are kept
            entry = valid(sample["key"]) or {"key": sample["key"], "id": sample["id"], "condition": sample["condition"],
                                             "fingerprint": sample["fingerprint"], "config": config,
                                             "ragas": {}, "ragas_errors": {}, "judge_seconds": 0.0}
            entry["ragas"].update(result["ragas"])
            for name, value in result["ragas"].items():
                if value is not None:
                    entry["ragas_errors"].pop(name, None)
            entry["ragas_errors"].update(result["ragas_errors"])
            entry["judge_seconds"] = round(entry.get("judge_seconds", 0.0) + result["judge_seconds"], 1)
            cache[sample["key"]] = entry
            sink.write(json.dumps(entry, ensure_ascii=False) + "\n")
            sink.flush()
            done["n"] += 1
            failed = [m for m, v in result["ragas"].items() if v is None]
            print(f"  judged {done['n']}/{len(plan)}: Q{sample['id']} {sample['condition']}"
                  + (f" ({len(failed)} score(s) missing)" if failed else "") + f" in {result['judge_seconds']}s")

        print(f"Judging {len(plan)} answers ({len(selected) - len(plan)} already judged)...")
        usage = {}
        try:
            for need in sorted({need for _, need in plan}):
                group = [x for x, n in plan if n == need]
                part = asyncio.run(judge_all(group, settings, need, concurrency=args.concurrency, on_result=save))
                usage = {k: usage.get(k, 0) + v for k, v in part.items()}
        except KeyboardInterrupt:
            print("Interrupted: judged answers are saved in ragas.jsonl; score again to continue.", file=sys.stderr)
            return 130
        finally:
            sink.close()
            # Keep one line per answer: the latest judgment
            with (run_dir / "ragas.jsonl").open("w", encoding="utf-8") as f:
                for entry in cache.values():
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    lines, stale, judge_errors = [], 0, []
    for record in records:
        line = score_record(record, items[record["id"]])
        key = "|".join(map(str, record_key(record)))
        entry = valid(key)
        if entry:
            line["ragas"], line["ragas_errors"] = dict(entry["ragas"]), dict(entry["ragas_errors"])
            judge_errors += [{"id": record["id"], "condition": record["condition"], "metric": m, "reason": reason}
                             for m, reason in (entry.get("ragas_errors") or {}).items()]
        elif key in cache and key in by_key:
            stale += 1
        lines.append(line)

    complete = sum(1 for x in selected if valid(x["key"]) and all(valid(x["key"])["ragas"].get(o) is not None
                                                                   for o in wanted))
    partly = sum(1 for x in selected if valid(x["key"])) - complete
    if args.no_judge:
        shown = sum(1 for line in lines if "ragas" in line)
        status = "no new judging (--no-judge)" + (f"; showing {shown} earlier judgments that still match" if shown else "")
    else:
        status = f"judged {complete} of {len(selected)} answers on {args.judge_on} ({', '.join(metrics)})"
        if partly:
            status += f"; {partly} with missing scores (listed under Not judged)"
    if stale:
        status += (f"; {stale} earlier judgment(s) left out because the answer, its chunks, the reference or the "
                   "judge settings changed" + ("" if args.no_judge else " and they weren't selected for judging"))

    judge_info = {"status": status, "usage": usage, "metrics": list(metrics), "errors": judge_errors}
    summary = build_summary(lines, items, run_config, judge_info)
    summary["scored_with"] = {"gold_file": rel(gold_path), "sha256": gold_sha256(gold_path)}
    with (run_dir / "scores.jsonl").open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")

    card = summary["scorecard"]
    print(f"Scored {len(lines)} records. Answer correct (full): {card['answer correct (strict)']['all']['value']}; "
          f"context held all the evidence: {card['context held all the evidence']['all']['value']}")
    print(f"Scorecard: {rel(run_dir)}/summary.md")
    return 0


# ---- compare --------------------------------------------------------------------------------------

def cmd_compare(args, parser) -> int:
    from evaluation.compare import compare, render_markdown

    runs = []
    for path in (args.base, args.run):
        run_dir = find_run(parser, path)
        scores = read_jsonl(run_dir / "scores.jsonl")
        if not scores:
            parser.error(f"{rel(run_dir)} isn't scored yet: `score --run {rel(run_dir)} --no-judge` first")
        runs.append((run_dir, json.loads((run_dir / "run_config.json").read_text(encoding="utf-8")), scores))
    (base_dir, base_config, base_scores), (run_dir, run_config, run_scores) = runs
    text = render_markdown(compare(base_scores, run_scores, base_config, run_config, args.condition))
    out = run_dir / f"compare-{base_dir.name}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"Wrote {rel(out)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="ask the golden-set questions and record what the app did")
    run.add_argument("--set", help=f"golden set (default: {rel(GOLD_FILE)})")
    run.add_argument("--only", help="ids and ranges to run, e.g. 1,8,17 or 1-15 (default: all)")
    run.add_argument("--conditions", default="full",
                     help="any of full, no_xlsx (the XLSX ablation) and gold (perfect retrieval), comma-separated. "
                          "Default: full")
    run.add_argument("--snapshot", help="index snapshot for the gold condition (default: logs/eval/index_snapshot.jsonl)")
    run.add_argument("--out", help="output folder (default: logs/eval/answers/<YYYYmmdd-HHMMSS>)")
    run.add_argument("--repeat", type=int, default=1, help="ask each question N times (default: 1)")
    run.add_argument("--delay", type=float, default=2.0, help="seconds between questions (default: 2)")
    run.add_argument("--dry-run", action="store_true", help="print the plan and estimate; no Azure calls")
    run.set_defaults(func=cmd_run)

    score = sub.add_parser("score", help="score a run and write its scorecard")
    score.add_argument("--run", required=True, help="the run folder, e.g. logs/eval/answers/20261018-101500")
    score.add_argument("--set", help="golden set (default: the one the run used, else the current one)")
    score.add_argument("--no-judge", "--no-ragas", action="store_true", help="exact scores only; no judge calls")
    score.add_argument("--judge-on", "--ragas-on", choices=("full", "all"), default="full",
                       help="conditions the judge scores: full (default), or all (also no_xlsx and gold)")
    score.add_argument("--metrics", help="judge metrics, comma-separated (default: faithfulness,factual_correctness; "
                                         "also response_relevancy, context_precision, context_recall)")
    score.add_argument("--only", help="judge only these ids, e.g. 1-15 (the scorecard always covers the whole run)")
    score.add_argument("--concurrency", type=int, choices=(1, 2), default=1,
                       help="answers judged at once (default: 1; the model's quota is shared)")
    score.add_argument("--rejudge", action="store_true", help="judge the selected answers again, even if unchanged")
    score.add_argument("--dry-run", action="store_true", help="show what would be judged; no Azure calls")
    score.set_defaults(func=cmd_score)

    cmp = sub.add_parser("compare", help="compare a scored run with a scored baseline, question by question")
    cmp.add_argument("--base", required=True, help="the baseline run folder, e.g. logs/eval/answers/20261020-002251")
    cmp.add_argument("--run", required=True, help="the run to compare, e.g. one on --only the baseline's failures")
    cmp.add_argument("--condition", choices=("full", "no_xlsx", "gold"), default="full",
                     help="the condition to compare (default: full)")
    cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args, parser)


if __name__ == "__main__":
    sys.exit(main())
