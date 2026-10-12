#!/usr/bin/env python3
"""Stage 2 of the evaluation: score a run from scripts/run_eval.py and write the scorecard.

    python scripts/eval_score.py --run logs/eval/<YYYYmmdd-HHMMSS> --dry-run      # what would be judged
    python scripts/eval_score.py --run logs/eval/<YYYYmmdd-HHMMSS> --no-ragas     # deterministic scores only
    python scripts/eval_score.py --run logs/eval/<YYYYmmdd-HHMMSS>                # plus RAGAS (judge calls)

Always runs the deterministic scorers (number check, mentions, hit@5, MRR, behaviour checks). Unless
--no-ragas is given it also runs five RAGAS metrics on Q1-15, judged by gpt-4.1-mini at temperature 0;
by default only the `full` condition is judged (--ragas-on all adds no_xlsx).

Writes scores.jsonl, summary.json and summary.md into the run's folder. RAGAS results are kept in
ragas.jsonl, so scoring again (or after an interruption) only judges what's missing; --rejudge
starts over. docs/EVALUATION.md explains every score.
"""

import argparse
import asyncio
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.gold import gold_sha256, load_gold, parse_ids, turns  # noqa: E402
from evaluation.report import build_summary, is_core, record_key, render_markdown, score_record  # noqa: E402


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def key_of(entry: dict) -> str:
    return "|".join(map(str, record_key(entry)))


def judge_samples(records, items, ragas_on, only) -> list:
    """Answers RAGAS should judge: Q1-15 on the graph route, non-empty answer, chosen conditions."""
    conditions = ("full",) if ragas_on == "full" else ("full", "no_xlsx")
    samples = []
    for record in records:
        item = items[record["id"]]
        if not is_core(record, item) or record["condition"] not in conditions:
            continue
        if only and record["id"] not in only:
            continue
        if not (record.get("answer") or "").strip() or not record.get("guardrail_passed"):
            continue
        samples.append({
            "key": key_of(record), "id": record["id"], "condition": record["condition"],
            "question": record["question"], "answer": record["answer"],
            "reference": turns(item)[record["turn"] - 1]["reference"],
            "contexts": [chunk.get("content") or "" for chunk in record.get("retrieved") or []],
        })
    return samples


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run", required=True, help="the run folder, e.g. logs/eval/20261012-101500")
    parser.add_argument("--set", help="gold questions file (default: the one the run used)")
    parser.add_argument("--no-ragas", action="store_true", help="deterministic scores only; no judge calls")
    parser.add_argument("--ragas-on", choices=("full", "all"), default="full",
                        help="conditions RAGAS judges: full (default) or all (also no_xlsx, twice the calls)")
    parser.add_argument("--only", help="ids and ranges to score, e.g. 1-15 (default: all in the run)")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1,
                        help="answers judged at once (default: 1; the model's quota is shared)")
    parser.add_argument("--rejudge", action="store_true", help="ignore ragas.jsonl and judge again")
    parser.add_argument("--dry-run", action="store_true", help="show what would be judged; no Azure calls")
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    if not run_dir.is_absolute():
        run_dir = (Path.cwd() / run_dir) if (Path.cwd() / run_dir).exists() else ROOT / run_dir
    run_config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    records = read_jsonl(run_dir / "records.jsonl")
    if not records:
        parser.error(f"no records in {run_dir / 'records.jsonl'}")
    gold_path = Path(args.set or run_config["gold_file"]["path"])
    gold_path = gold_path if gold_path.is_absolute() else ROOT / gold_path
    items = {item["id"]: item for item in load_gold(gold_path)}
    if gold_sha256(gold_path) != run_config["gold_file"].get("sha256"):
        print(f"note: {gold_path.name} changed since this run; scoring uses the current version", file=sys.stderr)
    only = parse_ids(args.only)
    records = [r for r in records if not only or r["id"] in only]

    cached = {} if args.rejudge else {e["key"]: e for e in read_jsonl(run_dir / "ragas.jsonl")}
    samples = [] if args.no_ragas else judge_samples(records, items, args.ragas_on, only)
    todo = [s for s in samples if s["key"] not in cached]

    if args.dry_run:
        from evaluation.judge import calls_per_item
        chat = sum(calls_per_item(len(s["contexts"]))["chat"] for s in todo)
        emb = sum(calls_per_item(len(s["contexts"]))["embedding"] for s in todo)
        print(f"Scoring plan for {run_dir.name} (dry run: nothing is sent to Azure)")
        print(f"  records: {len(records)}; deterministic scores for all of them")
        if args.no_ragas:
            print("  RAGAS: off (--no-ragas)")
        else:
            print(f"  RAGAS on {args.ragas_on}: {len(samples)} answers, {len(samples) - len(todo)} already judged, "
                  f"{len(todo)} to judge")
            print(f"  Estimated judge use: {chat} gpt-4.1-mini calls and {emb} embedding calls, "
                  f"about {round(len(todo) * 0.6, 1)} minutes at concurrency {args.concurrency}")
        return 0

    ragas_status = "off (--no-ragas)" if args.no_ragas else None
    usage = None
    if todo:
        from evaluation.judge import judge_all, read_settings
        try:
            settings = read_settings(ROOT)
        except RuntimeError as e:
            parser.error(str(e))
        warnings.filterwarnings("ignore")
        sink = (run_dir / ("ragas.jsonl")).open("w" if args.rejudge else "a", encoding="utf-8")
        done = {"n": 0}

        def save(sample, result):
            entry = {"key": sample["key"], "id": sample["id"], "condition": sample["condition"], **result}
            cached[sample["key"]] = entry
            sink.write(json.dumps(entry) + "\n")
            sink.flush()
            done["n"] += 1
            failed = [m for m, v in result["ragas"].items() if v is None]
            print(f"  judged {done['n']}/{len(todo)}: Q{sample['id']} {sample['condition']}"
                  + (f" ({len(failed)} metric(s) not scored)" if failed else "") + f" in {result['judge_seconds']}s")

        print(f"Judging {len(todo)} answers with RAGAS ({len(samples) - len(todo)} already judged)...")
        try:
            usage = asyncio.run(judge_all(todo, settings, concurrency=args.concurrency, on_result=save))
        except KeyboardInterrupt:
            print("Interrupted: judged answers are saved in ragas.jsonl; run again to continue.", file=sys.stderr)
            return 130
        finally:
            sink.close()
    if not args.no_ragas:
        judged = sum(1 for s in samples if s["key"] in cached)
        ragas_status = f"judged {judged} of {len(samples)} answers on {args.ragas_on}"

    lines = []
    for record in records:
        line = score_record(record, items[record["id"]])
        entry = cached.get(key_of(record))
        if entry:  # with --no-ragas, earlier judge results still show; no new judge calls are made
            line["ragas"], line["ragas_errors"] = entry["ragas"], entry["ragas_errors"]
        lines.append(line)
    if args.no_ragas and any("ragas" in line for line in lines):
        ragas_status = "from an earlier judging (ragas.jsonl); --no-ragas made no new judge calls"

    summary = build_summary(lines, items, run_config, {"status": ragas_status, "usage": usage})
    with (run_dir / "scores.jsonl").open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")

    card = summary["scorecard"]
    rel = run_dir.relative_to(ROOT) if run_dir.is_relative_to(ROOT) else run_dir
    print(f"Scored {len(lines)} records. Answer correct (Q1–15, full): "
          f"{card['answer correct (strict)']['all']['value']}; hit@5: {card['hit@5']['all']['value']}")
    print(f"Scorecard: {rel}/summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
