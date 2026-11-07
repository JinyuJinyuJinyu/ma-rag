"""The retrieval eval: send each golden-set question to the retriever, then check the chunks that come
back against the question's evidence units (evaluation/evidence.py).

Stage 1, run_queries(): every question in every mode (hybrid, vector, keyword) under every condition
(full; no_xlsx hides the spreadsheets), through the app's own AzureSearchRetriever.search(). Each
question is embedded once and the vector reused, so every mode searches with the same query. Each
search becomes one record with its ranked chunks (results.jsonl).

Stage 2, score_records() and build_summary(): passage-level scores per record, then a scorecard per
mode and condition (summary.json, summary.md). Pure functions, no Azure calls, so a golden-set edit
only needs a re-score, not new searches.
"""

import time
from collections import defaultdict
from datetime import datetime, timezone

from evaluation.collect import sanitize
from evaluation.evidence import (chunks_label, chunks_order, evidence_scores, files_label, files_needed, needs,
                                 required_files, spreadsheet_only, unit_files)
from evaluation.gold import (DIFFICULTIES, GOLD_CONTEXT_CHUNKS, VERTICAL_OPS, is_core_question, item_vertical, turns,
                             vertical_cell)
from evaluation.probe import NO_XLSX_FILTER, filter_leaked, is_filter_rejection

MODES = ("hybrid", "vector", "keyword")
CONDITIONS = ("full", "no_xlsx")
FILTERS = {"full": None, "no_xlsx": NO_XLSX_FILTER}
NEEDS_COLUMNS = (("all", "All"), ("xlsx", "Needs a workbook"), ("docs", "Docs"), ("both", "Cross-source"),
                 ("vertical", "Vertical"))
METRICS = ("hit_at_1", "hit_at_3", "hit", "recall", "complete", "mrr", "precision", "ndcg", "right_file")
SUMMARY_METRICS = ("hit", "recall", "complete", "mrr")  # the short tables: by difficulty, files, kind, company
CAVEATS = (
    "Evidence is matched by text: a chunk counts when it comes from a file that states the fact and holds "
    "the fact's figures and words (evaluation/evidence.py). `check-gold` shows that every pattern matches "
    "the index.",
    "Small n: one question moves a cell by 1/n. Read differences of a question or two as noise.",
    "Under no_xlsx, facts only a workbook states are left out; a question with nothing else to find is n/a.",
    "A follow-up item counts its first turn only: later turns mean nothing without the conversation.",
    "Right file@k is the old file-level check: a chunk from a file that states the fact, not necessarily "
    "the passage that does.",
    "By difficulty, by files needed and per file cover the single-turn questions, as the answer report does: a "
    "follow-up's difficulty describes its whole conversation. Per file counts the questions that require a file "
    "(it is the only source of one of their facts); a fact that several files state requires none of them.",
    "A vertical question lists every workbook row it reads as evidence: when its rows need more row chunks than "
    "the top k, only column views (where the index has them) can make it complete at k, so read its recall (the "
    "share of its rows found). Under no_xlsx a vertical question is n/a: it reads workbook rows, and the few a "
    "document also states would score it complete.",
)

WORST_SHOWN = 15  # questions listed under "Incomplete evidence"


class FilterRejected(RuntimeError):
    """Azure AI Search refused the no_xlsx filter; the run must stop."""

    def __init__(self, record: dict):
        super().__init__(record["error"]["message"])
        self.record = record


def run_label(mode: str, condition: str) -> str:
    return f"{mode} · {condition}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- Stage 1: search ------------------------------------------------------------------------------

def plan_counts(queries: list[dict], modes, conditions) -> dict:
    """The Azure calls a run makes: one embedding per question (none for keyword only), one search each."""
    embeddings = len(queries) if any(mode != "keyword" for mode in modes) else 0
    return {"questions": len(queries), "embeddings": embeddings,
            "searches": len(queries) * len(modes) * len(conditions)}


def _file_type(doc: dict) -> str:
    file_type = (doc.get("metadata") or {}).get("file_type")
    source = doc.get("source_file") or ""
    return file_type or (source.rsplit(".", 1)[1].lower() if "." in source else None)


def run_queries(retriever, queries: list[dict], modes=("hybrid",), conditions=("full",), k: int = 5,
                delay: float = 0.0, on_record=None, sleep=time.sleep) -> list[dict]:
    """Search every query in every mode and condition, in that order; on_record(record) after each.

    A failed search is recorded and the run goes on. A filter Azure AI Search rejects stops the run
    (FilterRejected), because every no_xlsx search after it would fail the same way. A failure counts as
    a rejected filter only when the filter is the plausible cause: the search's own embedding worked (or
    it needed none), and the same question in the same mode didn't fail unfiltered just before.
    """
    records, first = [], True
    needs_vector = any(mode != "keyword" for mode in modes)
    unfiltered_ok = {}  # (question id, turn, mode) -> did that search succeed without a filter?
    for query in queries:
        vector, embed_error = None, None
        if needs_vector:
            try:
                vector = retriever.embed_query(query["question"])
            except Exception as e:  # noqa: BLE001 - recorded on every search that needed it
                embed_error = e
        for mode in modes:
            for condition in conditions:
                if not first and delay:
                    sleep(delay)
                first = False
                record = {"id": query["id"], "turn": query["turn"], "question": query["question"], "mode": mode,
                          "condition": condition, "filter": FILTERS[condition], "k": k, "results": [],
                          "seconds": None, "error": None, "started_at": now_iso()}
                key = (query["id"], query["turn"], mode)
                uses_vector = mode != "keyword"
                t0 = time.perf_counter()
                try:
                    if embed_error is not None and uses_vector:
                        raise embed_error
                    docs = retriever.search(query["question"], top_k=k, mode=mode, filter_expr=FILTERS[condition],
                                            vector=vector if uses_vector else None)
                except Exception as e:  # noqa: BLE001 - one failed search must not stop the others
                    record["error"] = {"type": type(e).__name__, "message": sanitize(e)}
                    record["seconds"] = round(time.perf_counter() - t0, 3)
                    if not FILTERS[condition]:
                        unfiltered_ok[key] = False
                    elif ((embed_error is None or not uses_vector) and unfiltered_ok.get(key, True)
                          and is_filter_rejection(e)):
                        record["error"]["type"] = "filter_rejected"
                        records.append(record)
                        if on_record:
                            on_record(record)
                        raise FilterRejected(record) from e
                else:
                    if not FILTERS[condition]:
                        unfiltered_ok[key] = True
                    record["seconds"] = round(time.perf_counter() - t0, 3)
                    record["results"] = [
                        {"rank": rank, "chunk_id": doc.get("id"), "source_file": doc.get("source_file"),
                         "file_type": _file_type(doc), "chunk_index": doc.get("chunk_index"),
                         "score": doc.get("score"), "content": doc.get("content")}
                        for rank, doc in enumerate(docs or [], 1)
                    ]
                    leaks = filter_leaked(condition, record["results"])
                    if leaks:
                        record["error"] = {"type": "filter_leak", "message": "no_xlsx still returned spreadsheet "
                                           f"chunks from {', '.join(sorted(set(leaks)))}"}
                records.append(record)
                if on_record:
                    on_record(record)
    return records


# ---- Stage 2: score -------------------------------------------------------------------------------

def fact_label(unit: dict, index: int) -> str:
    return unit.get("fact") or f"evidence {index + 1}"


def score_record(record: dict, item: dict) -> dict:
    """One search's passage-level scores against its question's evidence units.

    scores is None for a failed search, a filter leak (the condition didn't hold), or a question with
    no unit left to find under no_xlsx (na=True).
    """
    units = turns(item)[record["turn"] - 1].get("evidence") or []
    k = record["k"]
    # Under no_xlsx the facts only a workbook states are left out, so the files counted are the scored facts'
    exclude = record["condition"] == "no_xlsx"
    kept = [unit for unit in units if not (exclude and spreadsheet_only(unit))]
    line = {"id": record["id"], "turn": record["turn"], "mode": record["mode"], "condition": record["condition"],
            "question": record["question"], "kind": item["kind"], "companies": list(item.get("companies") or []),
            "needs": needs(units), "difficulty": item.get("difficulty"), "files_needed": files_needed(kept),
            "required_files": required_files(kept), "core": is_core_question(item), "vertical": item_vertical(item),
            "k": k, "error": record.get("error"), "scores": None, "na": False, "first_rank": None, "missed": [],
            "units": len(units), "per_file": {}}
    if record.get("error"):
        return line
    if exclude and line["vertical"] and any(spreadsheet_only(unit) for unit in units):
        # A vertical question reads workbook rows: without the workbooks it can't be answered, and the few rows a
        # document also states would score it complete
        line["na"] = True
        return line
    results = record.get("results") or []
    at_k = evidence_scores(units, results, k, exclude_spreadsheets=exclude)
    if at_k is None:
        line["na"] = True
        return line
    files = {f for unit in kept for f in unit_files(unit)}
    line["scores"] = {
        "hit_at_1": evidence_scores(units, results, 1, exclude_spreadsheets=exclude)["hit"],
        "hit_at_3": evidence_scores(units, results, 3, exclude_spreadsheets=exclude)["hit"] if k >= 3 else None,
        **{key: at_k[key] for key in ("hit", "recall", "complete", "mrr", "precision", "ndcg")},
        "right_file": 1.0 if any(chunk.get("source_file") in files for chunk in results[:k]) else 0.0,
    }
    line["first_rank"] = at_k["first_rank"]
    line["units"] = at_k["units"]
    line["missed"] = [fact_label(units[i], i) for i in at_k["missed"]]
    # Each file the question requires: did the top k hold every fact only that file states (found), and
    # any chunk of the file at all (seen)? Under no_xlsx a workbook-only fact is out, so is its file
    missed = set(at_k["missed"])
    for source in line["required_files"]:
        own = {i for i, unit in enumerate(units) if unit_files(unit) == [source]}
        line["per_file"][source] = {
            "found": 0.0 if own & missed else 1.0,
            "seen": 1.0 if any(chunk.get("source_file") == source for chunk in results[:k]) else 0.0,
        }
    return line


def score_records(records: list[dict], items: dict) -> tuple[list[dict], list[str]]:
    """Score every record whose question is still in the golden set, unchanged; notes say what was skipped.

    A search is scored only against the item it was made for: if the item's question was reworded (or
    its id now belongs to another question), its old chunks say nothing about the new question.
    """
    lines, skipped = [], {}
    for record in records:
        item = items.get(record["id"])
        if item is None or item["expect"] != "answer" or record["turn"] > len(turns(item)):
            reason = "no longer a question with evidence in the golden set"
        elif turns(item)[record["turn"] - 1]["question"] != record["question"]:
            reason = "its question changed since this run (search again to score it)"
        else:
            lines.append(score_record(record, item))
            continue
        label = f"Q{record['id']}" + (f" turn {record['turn']}" if record["turn"] > 1 else "")
        reason_seen, count = skipped.get(label, (reason, 0))
        skipped[label] = (reason_seen, count + 1)
    notes = [f"{label} skipped ({count} search{'es' if count > 1 else ''}): {reason}"
             for label, (reason, count) in skipped.items()]
    return lines, notes


# ---- Aggregation ----------------------------------------------------------------------------------

def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def _cell(lines: list[dict], metric: str) -> dict:
    """The mean over the scored searches, with what wasn't scored counted by reason: failed searches,
    filter leaks, and n/a (nothing left to find under the condition)."""
    values = [(line["scores"] or {}).get(metric) for line in lines]
    present = [v for v in values if v is not None]
    error_types = [(line.get("error") or {}).get("type") for line in lines]
    return {"value": _mean(present), "n": len(present), "of": len(lines),
            "failed": sum(1 for t in error_types if t and t != "filter_leak"),
            "leaked": sum(1 for t in error_types if t == "filter_leak")}


def _file_cell(lines: list[dict], source: str, key: str) -> dict:
    """Like _cell, for one required file's found or seen: the searches that failed or leaked count in `of`."""
    values = [line["per_file"][source][key] for line in lines if source in line.get("per_file", {})]
    error_types = [(line.get("error") or {}).get("type") for line in lines]
    return {"value": _mean(values), "n": len(values), "of": len(lines),
            "failed": sum(1 for t in error_types if t and t != "filter_leak"),
            "leaked": sum(1 for t in error_types if t == "filter_leak")}


def _in_column(line: dict, column: str) -> bool:
    """A column: all, where the facts live, or the vertical questions (also counted where their facts live)."""
    if column == "vertical":
        return bool(line.get("vertical"))
    return column == "all" or line["needs"] == column


def _shape(line: dict) -> list[str]:
    """The workbook questions as vertical (one column down many rows) or row (each fact in a row); [] for others."""
    if line.get("vertical"):
        return ["vertical"]
    return ["row"] if line["needs"] in ("xlsx", "both") else []


def beyond_k(line: dict, k: int) -> bool:
    """A vertical question whose rows need more chunks than a search returns: it can't be complete at k."""
    return bool(line.get("vertical")) and line["vertical"]["chunks"] > k


def _runs(lines: list[dict], run_config: dict) -> list[tuple]:
    present = {(line["mode"], line["condition"]) for line in lines}
    ordered = [(m, c) for m in run_config.get("modes") or MODES for c in run_config.get("conditions") or CONDITIONS]
    return [run for run in ordered if run in present] + sorted(present - set(ordered))


def _rank_cell(line: dict) -> str:
    if line["error"]:
        return "leak" if line["error"]["type"] == "filter_leak" else "error"
    if line["na"]:
        return "n/a"
    return str(line["first_rank"]) if line["first_rank"] else "–"


def build_summary(lines: list[dict], run_config: dict, notes=(), scored_with=None) -> dict:
    """The scorecards. scored_with ({path, sha256}) is the golden set the lines were scored with, when it
    may differ from the one the run searched with (a re-score)."""
    runs = _runs(lines, run_config)
    by_run = {run: [line for line in lines if (line["mode"], line["condition"]) == run] for run in runs}
    primary = runs[0] if runs else None
    k = run_config.get("k") or GOLD_CONTEXT_CHUNKS  # the vertical questions' chunks are grouped against it
    scorecards = {}
    for run in runs:
        card = {}
        for metric in METRICS:
            card[metric] = {}
            for column, _ in NEEDS_COLUMNS:
                members = [line for line in by_run[run] if _in_column(line, column)]
                card[metric][column] = _cell(members, metric)
        scorecards[run_label(*run)] = card

    def breakdown(key_of, order=(), core_only=False):
        groups = defaultdict(list)
        for line in by_run.get(primary, []):
            if core_only and not line.get("core", True):
                continue
            for key in key_of(line):
                groups[key].append(line)
        ranked = sorted(groups, key=lambda key: (list(order).index(key) if key in order else len(order), str(key)))
        return {key: {"n": len(groups[key]), **{m: _cell(groups[key], m) for m in SUMMARY_METRICS}} for key in ranked}

    requiring = defaultdict(list)  # file -> the core searches whose question requires it, failed ones included
    for line in by_run.get(primary, []):
        if line.get("core", True):
            for source in line.get("required_files", []):
                requiring[source].append(line)

    questions = {}
    labels = [run_label(*run) for run in runs]
    for line in lines:
        # A search the run never made (it stopped early) reads "not run", not "–" (no evidence found)
        row = questions.setdefault((line["id"], line["turn"]), {
            "id": line["id"], "turn": line["turn"], "question": line["question"], "kind": line["kind"],
            "difficulty": line.get("difficulty"), "files_needed": line.get("files_needed"), "needs": line["needs"],
            "vertical": vertical_cell(line.get("vertical")), "ranks": {label: "not run" for label in labels},
            "missed": None})
        row["ranks"][run_label(line["mode"], line["condition"])] = _rank_cell(line)
        if (line["mode"], line["condition"]) == primary and line["scores"]:
            row["missed"] = line["missed"]
    # The questions that can't be complete at k (vertical, more chunks than k) are left out: they would crowd out
    # the misses retrieval could fix
    scored = [line for line in by_run.get(primary, []) if line["scores"]]
    worst = sorted((line for line in scored if not line["scores"]["complete"] and not beyond_k(line, k)),
                   key=lambda line: (line["scores"]["recall"], line["scores"]["mrr"], line["id"]))
    # The best complete@k a perfect search could reach, by column. Only vertical questions carry a chunks count; the
    # others are taken to fit in k, which holds for k of 5 or more (they list at most 5 units)
    ceiling = {column: _mean([0.0 if beyond_k(line, k) else 1.0 for line in scored if _in_column(line, column)])
               for column, _ in NEEDS_COLUMNS}
    gold = run_config.get("gold_file") or {}
    gold_changed = bool(scored_with and scored_with.get("sha256") != gold.get("sha256"))
    return {
        "run": {key: run_config.get(key) for key in ("run_id", "started_at", "finished_at", "index_name", "k",
                                                       "modes", "conditions", "embedding_deployment", "git",
                                                       "gold_file", "stopped")},
        "k": run_config.get("k"),
        "questions": len({(line["id"], line["turn"]) for line in lines}),
        "runs": [run_label(*run) for run in runs],
        "primary": run_label(*primary) if primary else None,
        "scorecards": scorecards,
        "by_difficulty": breakdown(lambda line: [line.get("difficulty") or "(none)"], DIFFICULTIES, core_only=True),
        "by_files": breakdown(lambda line: [files_label(line.get("files_needed", 0))], core_only=True),
        "by_kind": breakdown(lambda line: [line["kind"]]),
        "by_shape": breakdown(_shape, ("vertical", "row"), core_only=True),
        "by_vertical_chunks": breakdown(lambda line: [chunks_label(line["vertical"]["chunks"], k)]
                                        if line.get("vertical") else [], chunks_order(k), core_only=True),
        "by_vertical_op": breakdown(lambda line: [line["vertical"]["op"]] if line.get("vertical") else [],
                                    VERTICAL_OPS, core_only=True),
        "by_company": breakdown(lambda line: line["companies"] or ["(none)"]),
        "per_file": {source: {"n": len({line["id"] for line in members}),
                              "found": _file_cell(members, source, "found"),
                              "seen": _file_cell(members, source, "seen")}
                     for source, members in sorted(requiring.items())},
        "per_question": sorted(questions.values(), key=lambda row: (row["id"], row["turn"])),
        "worst": [{"id": line["id"], "question": line["question"], "recall": line["scores"]["recall"],
                   "first_rank": line["first_rank"], "missed": line["missed"]} for line in worst[:WORST_SHOWN]],
        "worst_total": len(worst),
        "beyond_k": len({line["id"] for line in scored if beyond_k(line, k)}),
        "complete_ceiling": ceiling,
        "scored_with": scored_with or {"path": gold.get("path"), "sha256": gold.get("sha256")},
        "gold_changed": gold_changed,
        "errors": [{"id": line["id"], "run": run_label(line["mode"], line["condition"]), **line["error"]}
                   for line in lines if line["error"] and line["error"]["type"] != "filter_leak"],
        "leaks": [{"id": line["id"], "run": run_label(line["mode"], line["condition"]),
                   "message": line["error"]["message"]}
                  for line in lines if line["error"] and line["error"]["type"] == "filter_leak"],
        "notes": list(notes),
    }


# ---- Markdown -------------------------------------------------------------------------------------

def _fmt(value, digits=2):
    return "–" if value is None else f"{value:.{digits}f}"


def _fmt_cell(cell: dict) -> str:
    """A mean, or why there is none: error (every search failed), leak, n/a (nothing to find), – (no rows)."""
    failed, leaked = cell.get("failed", 0), cell.get("leaked", 0)
    if cell["value"] is None:
        if cell["of"] == 0:
            return "–"
        return "error" if failed else ("leak" if leaked else "n/a")
    extras = [f"n={cell['n']}"] if cell["n"] != cell["of"] else []
    extras += [f"{failed} failed"] if failed else []
    return f"{_fmt(cell['value'])}" + (f" ({'; '.join(extras)})" if extras else "")


def metric_labels(k) -> dict:
    return {"hit_at_1": "hit@1", "hit_at_3": "hit@3", "hit": f"hit@{k}", "recall": f"recall@{k}",
            "complete": f"complete@{k}", "mrr": "MRR", "precision": f"precision@{k}", "ndcg": f"nDCG@{k}",
            "right_file": f"right file@{k}"}


METRIC_HELP = {
    "hit": "some chunk in the top k holds some evidence",
    "recall": "the share of the question's facts the top k hold",
    "complete": "the top k hold every fact the answer needs",
    "mrr": "1 / rank of the first chunk with evidence",
    "precision": "the share of the top k that hold evidence",
    "ndcg": "rank-discounted: facts found early count more",
    "right_file": "a chunk from a file that states a fact (not necessarily the passage)",
}


def render_markdown(summary: dict) -> str:
    run, k = summary["run"], summary["k"]
    labels = metric_labels(k)
    out = ["# Retrieval scorecard", ""]
    out.append(f"Run `{run.get('run_id')}` · {run.get('started_at')} · index `{run.get('index_name')}` · "
               f"top {k}" + (f" (hybrid: {run['hybrid_knn']} vector neighbours)" if run.get("hybrid_knn") else "")
               + f" · {summary['questions']} questions · runs: {', '.join(summary['runs']) or 'none'}")
    out += ["", "*Each question's chunks are checked against the facts its answer needs (the golden set's "
            "evidence). All scores run from 0 to 1; higher is better. See the caveats at the end.*", ""]
    if run.get("stopped"):
        out += [f"> **This run stopped early ({run['stopped']}):** the scores cover only the searches it made.", ""]
    if summary.get("gold_changed"):
        scored = summary.get("scored_with") or {}
        out += [f"> **The golden set changed since this run.** Scored with `{scored.get('path')}` (sha256 "
                f"{(scored.get('sha256') or '')[:12]}…); a search whose question changed is left out (see the notes).", ""]
    failed = summary.get("errors") or []
    if failed:
        out += [f"> **{len(failed)} search(es) failed** (listed under Failed searches): a cell reads *error* when "
                "every search behind it failed, and *(k failed)* when some did.", ""]
    if summary["leaks"]:
        ids = sorted({leak["id"] for leak in summary["leaks"]})
        out += [f"> **Warning: the no_xlsx filter leaked** for Q{', Q'.join(map(str, ids))}: spreadsheet chunks "
                "still came back, so those searches are left out. Check the `file_type` values in the index.", ""]

    if len(summary["runs"]) > 1:
        out += ["## Overview (all questions)", "", "| | " + " | ".join(summary["runs"]) + " |",
                "|---|" + "---|" * len(summary["runs"])]
        for metric in METRICS:
            cells = [_fmt_cell(summary["scorecards"][r][metric]["all"]) for r in summary["runs"]]
            out.append(f"| {labels[metric]} | " + " | ".join(cells) + " |")
        out.append("")

    for label in summary["runs"]:
        card = summary["scorecards"][label]
        out += [f"## {label}", "", "| | " + " | ".join(name for _, name in NEEDS_COLUMNS) + " |",
                "|---|" + "---|" * len(NEEDS_COLUMNS)]
        for metric in METRICS:
            out.append(f"| {labels[metric]} | " + " | ".join(_fmt_cell(card[metric][c]) for c, _ in NEEDS_COLUMNS)
                       + " |")
        out.append("")
    out += ["What the rows mean: " + "; ".join(f"**{labels[m]}**: {text}" for m, text in METRIC_HELP.items())
            + ". Columns: where the facts live (a workbook only, documents, or both), and the vertical questions "
            "(they also count under where their facts live).", ""]

    for title, key in (("By difficulty", "by_difficulty"), ("By files needed", "by_files"), ("By kind", "by_kind"),
                       ("By company", "by_company")):
        rows = summary.get(key)
        if not rows:
            continue
        out += [f"## {title} ({summary['primary']})", "",
                "| | n | " + " | ".join(labels[m] for m in SUMMARY_METRICS) + " |",
                "|---|---|" + "---|" * len(SUMMARY_METRICS)]
        for name, row in rows.items():
            out.append(f"| {name} | {row['n']} | " + " | ".join(_fmt_cell(row[m]) for m in SUMMARY_METRICS) + " |")
        out.append("")
        if key == "by_difficulty":
            out += ["*By difficulty, by files needed and per file cover the single-turn questions, as the answer "
                    "report does (a follow-up's difficulty describes its whole conversation).*", ""]
        if key == "by_files":
            out += ["*Files needed*: the fewest files whose chunks can hold every fact the answer needs; a "
                    "multi-file question is complete only when one search surfaces all of them.", ""]

    if summary.get("by_vertical_op"):
        out += [f"## Vertical questions ({summary['primary']})", "",
                "*A vertical question reads one workbook column down many rows. The Excel narrator writes one sentence "
                "per row, so every row is its own passage (or a few rows share one), and the search must surface every "
                "row's chunk. Chunks needed: the fewest row chunks that hold every row the question reads; a question "
                f"that needs more than the top {k} can be complete at {k} only through column views (passages that "
                "list one column down the rows, where the index has them), so read its recall (the share of its rows "
                "found). Row: the other questions that need a workbook, each of whose facts one row states.*", ""]
        ceiling = summary.get("complete_ceiling") or {}
        if summary.get("beyond_k"):
            capped = ", ".join(f"{_fmt(ceiling.get(column))} for {name}" for column, name in NEEDS_COLUMNS
                               if ceiling.get(column) is not None and ceiling[column] < 1)
            out += [f"{summary['beyond_k']} vertical question(s) need more than {k} row chunks, so a search that "
                    f"finds row chunks only can't hold all their rows: complete@{k} can then reach at most {capped}. "
                    "They are left out of Incomplete evidence below.", ""]
        for title, key in (("Vertical vs row", "by_shape"), ("By chunks needed", "by_vertical_chunks"),
                           ("By operation", "by_vertical_op")):
            out += [f"### {title}", "", "| | n | " + " | ".join(labels[m] for m in SUMMARY_METRICS) + " |",
                    "|---|---|" + "---|" * len(SUMMARY_METRICS)]
            for name, row in summary[key].items():
                out.append(f"| {name} | {row['n']} | " + " | ".join(_fmt_cell(row[m]) for m in SUMMARY_METRICS) + " |")
            out.append("")

    if summary.get("per_file"):
        out += [f"## Per file ({summary['primary']})", "",
                "For each file, the searches for questions that require it (the file is the only source of one of "
                f"their facts). *found*: the top {k} held every fact only that file states. *seen*: some chunk of "
                f"the file was in the top {k}.", "",
                "| file | n | found | seen |", "|---|---|---|---|"]
        for source, row in summary["per_file"].items():
            out.append(f"| {source} | {row['n']} | {_fmt_cell(row['found'])} | {_fmt_cell(row['seen'])} |")
        out.append("")

    out += ["## Per question", "",
            f"First rank holding evidence (– = none in the top {k}; n/a = nothing to find under that condition; "
            f"error = the search failed; leak = spreadsheet chunks came back under no_xlsx; not run = the run "
            f"stopped before this search). "
            f"Facts missed are for {summary['primary']}.", "",
            "| Q | question | kind | difficulty | files | needs | vertical | " + " | ".join(summary["runs"])
            + " | facts missed |",
            "|---|---|---|---|---|---|---|" + "---|" * len(summary["runs"]) + "---|"]
    for row in summary["per_question"]:
        q = row["question"] if len(row["question"]) <= 60 else row["question"][:57] + "…"
        turn = f" t{row['turn']}" if row["turn"] > 1 else ""
        ranks = " | ".join(row["ranks"].get(r, "not run") for r in summary["runs"])
        missed = "; ".join(row["missed"]) if row["missed"] else ""
        out.append(f"| {row['id']}{turn} | {q} | {row['kind']} | {row.get('difficulty') or '–'} | "
                   f"{row.get('files_needed') or '–'} | {row['needs']} | {row.get('vertical') or '–'} | {ranks} | "
                   f"{missed} |")

    if summary["worst"]:
        total = summary.get("worst_total", len(summary["worst"]))
        title = (f"## Incomplete evidence ({summary['primary']}): the {len(summary['worst'])} worst of {total}"
                 if total > len(summary["worst"]) else f"## Incomplete evidence ({summary['primary']}), worst first")
        out += ["", title, "",
                f"| Q | question | recall@{k} | first rank | facts missed |", "|---|---|---|---|---|"]
        for row in summary["worst"]:
            q = row["question"] if len(row["question"]) <= 60 else row["question"][:57] + "…"
            out.append(f"| {row['id']} | {q} | {_fmt(row['recall'])} | {row['first_rank'] or '–'} | "
                       f"{'; '.join(row['missed'])} |")

    if summary["errors"]:
        out += ["", "## Failed searches", "", "| Q | run | error |", "|---|---|---|"]
        for error in summary["errors"]:
            out.append(f"| {error['id']} | {error['run']} | {error['type']}: {error['message'][:100]} |")
    if summary["notes"]:
        out += ["", "Notes:", ""] + [f"- {note}" for note in summary["notes"]]

    out += ["", "## Caveats", ""] + [f"- {c}" for c in CAVEATS]
    return "\n".join(out) + "\n"
