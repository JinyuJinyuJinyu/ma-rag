#!/usr/bin/env python3
"""The retrieval eval: search the golden-set questions and check the chunks against their evidence.

    python scripts/eval_retrieval.py snapshot                 # copy every chunk in the index (live, read-only)
    python scripts/eval_retrieval.py check-gold               # the golden set against that copy (offline)
    python scripts/eval_retrieval.py run --dry-run            # the plan and call counts; nothing is sent
    python scripts/eval_retrieval.py run                      # hybrid search, top k, full index (live)
    python scripts/eval_retrieval.py run --modes hybrid,vector,keyword --conditions full,no_xlsx
    python scripts/eval_retrieval.py score --run logs/eval/retrieval/<YYYYmmdd-HHMMSS>   # re-score (offline)

`run` writes logs/eval/retrieval/<YYYYmmdd-HHMMSS>/: run_config.json, results.jsonl (every search's
ranked chunks), scores.jsonl, summary.json and summary.md. It searches through the app's own retriever
(AzureSearchRetriever.search): one embedding per question, one search per mode and condition, no LLM
calls. `score` scores a run again, for example after a golden-set edit, without searching again.
docs/EVALUATION.md explains the scores.
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

from evaluation.gold import GOLD_FILE, GoldError, gold_sha256, load_gold, parse_ids, retrieval_queries  # noqa: E402
from evaluation.retrieval import (CONDITIONS, FILTERS, MODES, FilterRejected, build_summary,  # noqa: E402
                                  plan_counts, render_markdown, run_label, run_queries, score_records)
from evaluation.snapshot import (SNAPSHOT_FILE, SnapshotError, check_gold, load_snapshot,  # noqa: E402
                                 load_snapshot_meta)

SECONDS_EMBEDDING = 0.3  # rough timings for --dry-run
SECONDS_SEARCH = 0.4


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rel(path: Path):
    path = Path(path).resolve()
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def git_ignored(path: Path):
    """True/False if git says the path is ignored or not; None when git can't tell (no git, not a repo)."""
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(path)], capture_output=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(done.returncode)


def under_logs(path: Path) -> bool:
    return (ROOT / "logs") in Path(path).resolve().parents


def safe_output(parser, path) -> Path:
    """An output path that can't end up in git: the snapshot and the runs quote the indexed documents.

    Inside the repo it must be a path git ignores (logs/ is), and never under datasets/; outside the
    repo anything goes. Without git to ask, only logs/ counts as safe.
    """
    path = Path(path) if Path(path).is_absolute() else ROOT / path
    resolved = path.resolve()
    if resolved == ROOT / "datasets" or (ROOT / "datasets") in resolved.parents:
        parser.error("write eval output under logs/eval/, never into datasets/")
    if resolved == ROOT or ROOT in resolved.parents:
        ignored = git_ignored(resolved)
        if not (ignored or (ignored is None and under_logs(resolved))):
            parser.error(f"{rel(path)} isn't git-ignored, and eval output quotes the indexed documents: write it "
                         "under logs/eval/ (or outside the repo)")
    return path


def privacy_note(path: Path) -> str:
    if under_logs(path):
        return "It quotes the indexed documents, so it stays under logs/ (git-ignored)."
    return "It quotes the indexed documents: keep it out of anything you publish."


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
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.ERROR)
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    for handler in root.handlers:
        handler.setLevel(logging.ERROR)
    for name in ("httpx", "azure", "urllib3", "opencensus", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)


def make_retriever(k=None):
    """The app's retriever (tests replace this function)."""
    from src.retrieval.retriever import AzureSearchRetriever
    return AzureSearchRetriever(top_k=k)


def split_list(value: str, allowed, parser, name):
    values = [v.strip() for v in (value or "").split(",") if v.strip()]
    unknown = [v for v in values if v not in allowed]
    if unknown or not values:
        parser.error(f"--{name} takes a comma-separated list of {', '.join(allowed)}")
    return list(dict.fromkeys(values))


def resolve(path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def gold_path_of(args_set) -> Path:
    return resolve(args_set or GOLD_FILE)


def load_items(parser, path: Path) -> list:
    try:
        return load_gold(path)
    except FileNotFoundError:
        parser.error(f"no golden set at {rel(path)}")
    except GoldError as e:
        parser.error(f"{rel(path)}: {e}")


# ---- snapshot -------------------------------------------------------------------------------------

def cmd_snapshot(args, parser) -> int:
    from evaluation.snapshot import export_snapshot

    path = safe_output(parser, args.out or SNAPSHOT_FILE)
    try:
        old = load_snapshot_meta(path) if path.exists() else None
    except ValueError:  # an unreadable old summary: it is replaced anyway
        old = None
    quiet_logging()
    retriever = make_retriever()
    print(f"Reading every chunk of index {retriever.index_name} (read-only: one facet query, then one "
          "query per source file)...")
    meta = export_snapshot(retriever, path)
    if old:
        print(f"  replaced the snapshot from {old.get('created_at', '?')} ({old.get('chunks', '?')} chunks)")
    print(f"Saved {meta['chunks']} chunks from {len(meta['files'])} files to {rel(path)} "
          f"({', '.join(f'{t} {n}' for t, n in meta['file_types'].items())})")
    print(privacy_note(path))
    print("Next: python scripts/eval_retrieval.py check-gold"
          + (f" --snapshot {rel(path)}" if path.resolve() != SNAPSHOT_FILE.resolve() else ""))
    return 0


# ---- check-gold -----------------------------------------------------------------------------------

def cmd_check_gold(args, parser) -> int:
    path = resolve(args.snapshot or SNAPSHOT_FILE)
    try:
        chunks = load_snapshot(path)
        meta = load_snapshot_meta(path)
    except (FileNotFoundError, SnapshotError, ValueError) as e:
        parser.error(str(e))
    gold_path = gold_path_of(args.set)
    items = load_items(parser, gold_path)
    report = check_gold(items, chunks)
    units = report["units"]
    print(f"Golden set {rel(gold_path)} against the snapshot {rel(path)} "
          f"({meta.get('chunks', len(chunks))} chunks, taken {meta.get('created_at', '?')})")
    for row in units:
        if args.verbose or row["status"] != "ok":
            turn = f" t{row['turn']}" if row["turn"] > 1 else ""
            mark = {"ok": " ", "loose": "?", "broken": "!"}[row["status"]]
            print(f"  {mark} Q{row['id']}{turn} evidence[{row['unit']}] {row['status']:<6} support={row['support']:<3} "
                  f"elsewhere={row['elsewhere']:<3} {row['fact']}")
    for hit in report["absent"]:
        print(f"  ! Q{hit['id']} absent pattern {hit['pattern']!r} matches {hit['count']} chunk(s): "
              f"{', '.join(hit['chunks'])}")
    for name in report["missing_files"]:
        print(f"  ! {name} is named by the golden set but has no chunk in the snapshot")
    vertical = report.get("vertical") or []
    for row in vertical:
        if not row["ok"]:
            found = ("no chunk holds some of its evidence" if row["fewest"] is None
                     else f"the fewest chunks holding its evidence is {row['fewest']}")
            print(f"  ! Q{row['id']} vertical chunks: labelled {row['label']}, but {found}")
        elif args.verbose:
            print(f"    Q{row['id']} vertical chunks: {row['label']}, as labelled")
    broken = [u for u in units if u["status"] == "broken"]
    loose = [u for u in units if u["status"] == "loose"]
    mislabelled = [row for row in vertical if not row["ok"]]
    print(f"\n{len(units)} evidence units: {len(units) - len(broken) - len(loose)} ok, {len(loose)} loose "
          f"(support of 10 or more: check the pattern is specific), {len(broken)} broken (no chunk holds them). "
          f"{len(report['absent'])} decline item pattern(s) found in the index; "
          f"{len(report['missing_files'])} file(s) missing; "
          f"{len(mislabelled)} of {len(vertical)} vertical question(s) with a chunks label the index doesn't match.")
    return 1 if (broken or report["absent"] or report["missing_files"] or mislabelled) else 0


# ---- run ------------------------------------------------------------------------------------------

def describe(record) -> str:
    if record.get("error"):
        return f"{record['error']['type']}: {record['error']['message'][:80]}"
    return f"{len(record['results'])} chunks"


def cmd_run(args, parser) -> int:
    modes = split_list(args.modes, MODES, parser, "modes")
    conditions = split_list(args.conditions, CONDITIONS, parser, "conditions")
    gold_path = gold_path_of(args.set)
    items = load_items(parser, gold_path)
    try:
        queries = retrieval_queries(items, parse_ids(args.only))
    except ValueError as e:
        parser.error(str(e))
    if not queries:
        parser.error("nothing to run: no question with evidence matches --only")
    from config import get_config
    config = get_config()
    k = config.retrieval_top_k if args.k is None else args.k
    if k < 1:
        parser.error("--k must be at least 1")
    if args.out:
        safe_output(parser, args.out)  # checked before a dry run too
    counts = plan_counts(queries, modes, conditions)
    minutes = round((counts["embeddings"] * SECONDS_EMBEDDING
                     + counts["searches"] * (SECONDS_SEARCH + args.delay)) / 60, 1)
    if args.dry_run:
        print("Retrieval eval plan (dry run: nothing is sent to Azure)")
        print(f"  golden set: {rel(gold_path)}; {counts['questions']} questions with evidence")
        print(f"  runs: {', '.join(run_label(m, c) for m in modes for c in conditions)}; top {k}; "
              f"delay {args.delay}s")
        for condition in conditions:
            if FILTERS[condition]:
                print(f"  {condition} filter: {FILTERS[condition]}")
        print(f"Estimated Azure use: {counts['embeddings']} embedding calls and {counts['searches']} searches "
              "(0 LLM calls, 0 Document Intelligence pages)")
        print(f"Estimated time: about {minutes} minutes")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = safe_output(parser, args.out or ROOT / "logs" / "eval" / "retrieval" / stamp)
    if out.exists():
        parser.error(f"{rel(out)} already exists: pick another --out (a run folder is written once)")
    out.mkdir(parents=True)
    quiet_logging()
    run_config = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": stamp,
        "git": git_state(),
        "index_name": config.search_index_name,
        "k": k,
        "hybrid_knn": config.retrieval_hybrid_knn,
        "modes": modes,
        "conditions": conditions,
        "filters": {c: FILTERS[c] for c in conditions},
        "embedding_deployment": config.ai_foundry_embedding_deployment,
        "gold_file": {"path": str(rel(gold_path)), "sha256": gold_sha256(gold_path)},
        "args": {"only": args.only, "delay": args.delay},
        "planned_searches": counts["searches"],
        "questions": counts["questions"],
    }
    (out / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")
    print(f"Run {stamp}: {counts['questions']} questions, {counts['searches']} searches "
          f"({', '.join(run_label(m, c) for m in modes for c in conditions)}), top {k}, "
          f"index {config.search_index_name}, writing to {rel(out)}")

    retriever = make_retriever(k)
    status, written, errors, leaks = 0, 0, 0, 0
    try:
        with (out / "results.jsonl").open("a", encoding="utf-8") as sink:
            def save(record):
                nonlocal written, errors, leaks
                sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                sink.flush()
                written += 1
                error_type = (record.get("error") or {}).get("type")
                errors += bool(error_type) and error_type != "filter_leak"
                leaks += error_type == "filter_leak"
                print(f"[{written:>3}/{counts['searches']}] Q{record['id']:<3} {record['mode']:<7} "
                      f"{record['condition']:<7}: {describe(record)} ({record['seconds']:.1f}s)")

            run_queries(retriever, queries, modes, conditions, k, delay=args.delay, on_record=save,
                        sleep=time.sleep)
    except FilterRejected as stop:
        print(f"STOPPED: Azure AI Search rejected the no_xlsx filter \"{stop.record['filter']}\": "
              f"{stop.record['error']['message']}", file=sys.stderr)
        status = 2
    except KeyboardInterrupt:
        print("Interrupted: the searches written so far are kept.", file=sys.stderr)
        status = 130
    except Exception as e:  # noqa: BLE001 - keep what's written and say why the run stopped
        print(f"STOPPED: {type(e).__name__}: {e}", file=sys.stderr)
        status = 1
    finally:
        run_config.update({"finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "records": written, "records_with_errors": errors, "filter_leaks": leaks,
                           "stopped": {1: "error", 2: "filter_rejected", 130: "interrupted"}.get(status)})
        (out / "run_config.json").write_text(json.dumps(run_config, indent=2), encoding="utf-8")

    print(f"\n{written} searches in {rel(out)}/results.jsonl ({errors} failed, {leaks} with a leaking filter).")
    if written:
        score_run(out, None)
    if status == 0 and (errors or leaks):
        print(f"Exit status 1: {errors} search(es) failed and {leaks} leaked; see Failed searches in the scorecard.",
              file=sys.stderr)
        status = 1
    return status


# ---- score ----------------------------------------------------------------------------------------

def score_run(run_dir: Path, gold_arg) -> dict:
    run_config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    records = read_jsonl(run_dir / "results.jsonl")
    gold_path = gold_path_of(gold_arg or run_config["gold_file"]["path"])
    items = {item["id"]: item for item in load_gold(gold_path)}
    scored_with = {"path": str(rel(gold_path)), "sha256": gold_sha256(gold_path)}
    lines, notes = score_records(records, items)
    if scored_with["sha256"] != run_config["gold_file"].get("sha256"):
        note = (f"The golden set {scored_with['path']} differs from the one this run searched with "
                f"({run_config['gold_file'].get('path')}): scores use {scored_with['path']}.")
        print(f"note: {note}", file=sys.stderr)
        notes.insert(0, note)
    summary = build_summary(lines, run_config, notes, scored_with=scored_with)
    with (run_dir / "scores.jsonl").open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    if summary["primary"]:
        card = summary["scorecards"][summary["primary"]]
        k = summary["k"]
        fmt = lambda cell: "–" if cell["value"] is None else f"{cell['value']:.2f}"  # noqa: E731
        print(f"Scored {len(lines)} searches. {summary['primary']}: hit@{k} {fmt(card['hit']['all'])}, "
              f"complete@{k} {fmt(card['complete']['all'])}, MRR {fmt(card['mrr']['all'])}")
    print(f"Scorecard: {rel(run_dir)}/summary.md")
    return summary


def cmd_score(args, parser) -> int:
    run_dir = Path(args.run)
    if not run_dir.is_absolute():
        run_dir = (Path.cwd() / run_dir) if (Path.cwd() / run_dir).exists() else ROOT / run_dir
    if not (run_dir / "results.jsonl").exists():
        parser.error(f"no results.jsonl in {run_dir}")
    try:
        score_run(run_dir, args.set)
    except (FileNotFoundError, GoldError) as e:
        parser.error(str(e))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="copy every chunk in the index to logs/eval/ (live, read-only)")
    p.add_argument("--out", help=f"snapshot file (default: {rel(SNAPSHOT_FILE)})")

    p = sub.add_parser("check-gold", help="check the golden set against the snapshot (offline)")
    p.add_argument("--snapshot", help=f"snapshot file (default: {rel(SNAPSHOT_FILE)})")
    p.add_argument("--set", help=f"golden set (default: {rel(GOLD_FILE)})")
    p.add_argument("--verbose", action="store_true", help="list every evidence unit, not just problems")

    p = sub.add_parser("run", help="search the golden-set questions and score them (live)")
    p.add_argument("--set", help=f"golden set (default: {rel(GOLD_FILE)})")
    p.add_argument("--only", help="ids and ranges to run, e.g. 1-15 or 8,20 (default: all with evidence)")
    p.add_argument("--modes", default="hybrid", help="hybrid, vector, keyword or a list (default: hybrid)")
    p.add_argument("--conditions", default="full", help="full, no_xlsx or full,no_xlsx (default: full)")
    p.add_argument("--k", type=int, help="chunks per search (default: RETRIEVAL_TOP_K, as the app uses)")
    p.add_argument("--delay", type=float, default=0.2, help="seconds between searches (default: 0.2)")
    p.add_argument("--out", help="output folder (default: logs/eval/retrieval/<YYYYmmdd-HHMMSS>)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and call counts; no Azure calls")

    p = sub.add_parser("score", help="score a run again (offline), e.g. after editing the golden set")
    p.add_argument("--run", required=True, help="the run folder, e.g. logs/eval/retrieval/20261018-101500")
    p.add_argument("--set", help="golden set (default: the one the run used)")

    args = parser.parse_args(argv)
    commands = {"snapshot": cmd_snapshot, "check-gold": cmd_check_gold, "run": cmd_run, "score": cmd_score}
    return commands[args.command](args, parser)


if __name__ == "__main__":
    sys.exit(main())
