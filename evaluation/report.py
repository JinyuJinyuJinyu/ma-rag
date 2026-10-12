"""Stage 2 helpers: score each record with the deterministic scorers, then build the scorecard.

score_record() turns one record from records.jsonl into a score line (scores.jsonl). build_summary()
aggregates the score lines into summary.json, and render_markdown() writes summary.md, the scorecard
for the demo and the report. Pure functions: no Azure calls, no ragas.
"""

import statistics
from collections import Counter, defaultdict

from evaluation.gold import turns
from evaluation.scoring import (BEHAVIOUR_CHECKS, FAIL_OPEN_PREFIX, classify_outcome, false_refusal,
                                retrieval_scores, score_answer)

RAGAS_METRICS = ("faithfulness", "response_relevancy", "context_precision", "context_recall",
                 "factual_correctness")
RAGAS_LABELS = {
    "faithfulness": "Faithfulness",
    "response_relevancy": "Response relevancy",
    "context_precision": "Context precision",
    "context_recall": "Context recall",
    "factual_correctness": "Factual correctness (F1)",
}
GROUP_COLUMNS = (("all", "All (Q1–15)"), ("xlsx", "XLSX (1–7, 11)"), ("docs", "Docs (8–10, 12)"),
                 ("cross", "Cross-source (13–15)"))
OUTCOMES = ("correct", "partial", "declined", "wrong", "refused", "error")
CAVEATS = (
    "n = 15 questions, so this is a smoke test, not a benchmark.",
    "The judge is the same model that wrote the answers (gpt-4.1-mini grading itself), the project's "
    "only chat deployment.",
    "RAGAS scores are estimates made by an LLM judge, not exact measurements.",
    "The number check is exact up to rounding but ignores signs: \"−$5,855M\" and \"$5,855M\" both match.",
)
ABLATION_NOTE = ("Simulates \"before XLSX ingestion\" by filtering retrieval; the index was not modified. "
                 "The doc-only questions are a control and should not drop.")


def record_key(record: dict) -> tuple:
    return (record["id"], record["turn"], record["repeat"], record["condition"], record["route"])


def is_core(record: dict, item: dict) -> bool:
    """Q1-Q15 on the graph route: the questions the scorecard and RAGAS cover."""
    return item["expect"] == "answer" and item["group"] != "behaviour" and record["route"] == "graph"


def score_record(record: dict, item: dict) -> dict:
    turn = turns(item)[record["turn"] - 1]
    line = {
        "id": record["id"], "turn": record["turn"], "repeat": record["repeat"], "condition": record["condition"],
        "route": record["route"], "group": item["group"], "expect": item["expect"],
        "question": record["question"], "error": record.get("error"),
        "timings_s": record.get("timings_s"), "tokens": record.get("tokens"),
        "openai_retries": record.get("openai_retries", 0),
    }
    line["guardrail_fail_open"] = (record.get("guardrail_reason") or "").startswith(FAIL_OPEN_PREFIX)
    line["notes"] = list(record.get("notes") or [])
    error_type = (record.get("error") or {}).get("type")
    # A failure in the harness or the app (a network error, a 429 that outlasted the retries, an agent's
    # failed LLM call) says nothing about answer quality: those scores are missing, not 0
    app_error = bool(error_type) and error_type != "filter_leak"
    if item["expect"] == "answer":
        answer = score_answer(turn, record.get("answer") or "")
        refused = false_refusal(record)
        line.update({
            "answer_score": answer,
            "false_refusal": None if app_error else refused,
            "answer_correct": None if app_error else (1.0 if (answer["all_required"] and not refused) else 0.0),
            "outcome": classify_outcome(record, answer),
        })
        if app_error:
            line["answer_score"] = {**answer, "key_numbers": None, "mentions": None}
        if is_core(record, item) or (item["id"] == 17 and record["turn"] == 1):
            # An answer-agent failure happened after retrieval, so retrieval still counts
            if app_error and error_type != "agent_error":
                line["retrieval"] = {"hit_at_5": None, "source_recall_at_5": None, "mrr": None}
            else:
                sources = [chunk.get("source_file") for chunk in record.get("retrieved") or []]
                line["retrieval"] = retrieval_scores(sources, item["sources"],
                                                     exclude_xlsx=record["condition"] == "no_xlsx")
    else:
        line["behaviour"] = BEHAVIOUR_CHECKS[item["expect"]](record)
    return line


# ---- Aggregation ----------------------------------------------------------------------------------

def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def _cell(lines, getter, expected_n):
    values = [getter(line) for line in lines]
    present = [v for v in values if v is not None]
    return {"value": _mean(present), "n": len(present), "of": expected_n}


def _in_group(line, group):
    return group == "all" or line["group"] == group


def scorecard(lines: list[dict]) -> dict:
    """Rows x columns of means for the core questions of one condition."""
    rows = {
        "hit@5": lambda l: (l.get("retrieval") or {}).get("hit_at_5"),
        "source recall@5": lambda l: (l.get("retrieval") or {}).get("source_recall_at_5"),
        "MRR": lambda l: (l.get("retrieval") or {}).get("mrr"),
        "key-number accuracy": lambda l: l["answer_score"]["key_numbers"],
        "answer correct (strict)": lambda l: l["answer_correct"],
        "false refusals": lambda l: 1.0 if l["false_refusal"] else 0.0,
    }
    for metric in RAGAS_METRICS:
        rows[RAGAS_LABELS[metric]] = (lambda m: lambda l: (l.get("ragas") or {}).get(m))(metric)
    table = {}
    for row, getter in rows.items():
        table[row] = {}
        for group, _ in GROUP_COLUMNS:
            members = [l for l in lines if _in_group(l, group)]
            cell = _cell(members, getter, len(members))
            if row == "false refusals":
                counted = [l for l in members if l["false_refusal"] is not None]  # app errors don't count
                cell = {"count": sum(1 for l in counted if l["false_refusal"]), "n": len(counted), "of": len(members)}
            table[row][group] = cell
    return table


def _percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    return round(statistics.quantiles(values, n=100, method="inclusive")[q - 1], 2)


def ops(lines: list[dict]) -> dict:
    graph = [l for l in lines if l["route"] == "graph" and l.get("timings_s")]
    latency = {}
    for key in ("total", "guardrail", "supervisor_retrieval", "intent_identifier", "answer_generator"):
        values = [l["timings_s"][key] for l in graph if l["timings_s"].get(key) is not None]
        latency[key] = {"p50": _percentile(values, 50), "p95": _percentile(values, 95), "n": len(values)}
    with_tokens = [l["tokens"] for l in lines if l.get("tokens")]
    return {
        "latency_s": latency,
        "tokens": {
            "records_with_counts": len(with_tokens),
            "mean_total": round(sum(t["total"] for t in with_tokens) / len(with_tokens)) if with_tokens else None,
            "sum_input": sum(t["input"] for t in with_tokens),
            "sum_output": sum(t["output"] for t in with_tokens),
            "sum_total": sum(t["total"] for t in with_tokens),
        },
        "openai_retries": sum(l.get("openai_retries") or 0 for l in lines),
        "errors": dict(Counter((l["error"] or {}).get("type") for l in lines if l.get("error"))),
    }


def ablation(lines: list[dict]) -> dict:
    """Compare full with no_xlsx, over the core questions present in both conditions."""
    by_cond = defaultdict(list)
    for line in lines:
        by_cond[line["condition"]].append(line)
    leaks = sorted({l["id"] for l in by_cond["no_xlsx"] if (l.get("error") or {}).get("type") == "filter_leak"})
    # A question whose filter leaked isn't a fair comparison: it leaves the table and the counts entirely
    paired = ({l["id"] for l in by_cond["full"]} & {l["id"] for l in by_cond["no_xlsx"]}) - set(leaks)
    full = [l for l in by_cond["full"] if l["id"] in paired]
    none = [l for l in by_cond["no_xlsx"] if l["id"] in paired]
    groups = (("xlsx", "XLSX (Q1–7, 11)"), ("docs", "Docs (Q8–10, 12; control)"),
              ("cross", "Cross-source (Q13–15)"), ("all", "All"))
    table = {}
    for group, _ in groups:
        f = [l for l in full if _in_group(l, group)]
        n = [l for l in none if _in_group(l, group)]
        correct_f, correct_n = _mean([l["answer_correct"] for l in f]), _mean([l["answer_correct"] for l in n])
        table[group] = {
            "n": len({l["id"] for l in f}),
            "correct_full": correct_f, "correct_no_xlsx": correct_n,
            "difference": round(correct_n - correct_f, 3) if None not in (correct_f, correct_n) else None,
            "key_numbers_full": _mean([l["answer_score"]["key_numbers"] for l in f]),
            "key_numbers_no_xlsx": _mean([l["answer_score"]["key_numbers"] for l in n]),
            "faithfulness_full": _mean([(l.get("ragas") or {}).get("faithfulness") for l in f]),
            "faithfulness_no_xlsx": _mean([(l.get("ragas") or {}).get("faithfulness") for l in n]),
        }
    per_question = []
    for qid in sorted(paired):
        per_question.append({
            "id": qid,
            "full": [l["outcome"] for l in sorted(full, key=lambda l: l["repeat"]) if l["id"] == qid],
            "no_xlsx": [l["outcome"] for l in sorted(none, key=lambda l: l["repeat"]) if l["id"] == qid],
            "no_xlsx_hit_at_5": _mean([(l.get("retrieval") or {}).get("hit_at_5") for l in none if l["id"] == qid]),
            "group": next(l["group"] for l in full if l["id"] == qid),
        })
    return {
        "groups": dict(groups), "table": table,
        "outcomes": {cond: dict(Counter(l["outcome"] for l in ls)) for cond, ls in (("full", full), ("no_xlsx", none))},
        "per_question": per_question,
        "leaks": leaks,
    }


def build_summary(lines: list[dict], items: dict, run_config: dict, ragas_info: dict) -> dict:
    core = [l for l in lines if is_core(l, items[l["id"]])]
    core_full = [l for l in core if l["condition"] == "full"]
    behaviour = [l for l in lines if items[l["id"]]["expect"] != "answer" or items[l["id"]]["group"] == "behaviour"]
    per_question = []
    for line in sorted(core_full, key=lambda l: (l["id"], l["repeat"])):
        notes = []
        if line["outcome"] != "correct":
            notes.append(line["outcome"])
        score = line["answer_score"]
        if score["numbers_missed"]:
            notes.append("missed " + ", ".join(score["numbers_missed"]))
        if score["mentions_missed"]:
            notes.append("no mention of " + ", ".join(score["mentions_missed"]))
        if line.get("error"):
            notes.append(f"{line['error']['type']}: {line['error'].get('message', '')[:60]}")
        notes.extend(line.get("notes") or [])
        if line.get("guardrail_fail_open") and not any("failed open" in n for n in notes):
            notes.append("the guardrail agent failed open")
        for metric, reason in (line.get("ragas_errors") or {}).items():
            notes.append(f"{metric} not scored ({reason[:40]})")
        per_question.append({
            "id": line["id"], "repeat": line["repeat"], "question": line["question"],
            "correct": None if line["answer_correct"] is None else bool(line["answer_correct"]),
            "outcome": line["outcome"],
            "hit_at_5": (line.get("retrieval") or {}).get("hit_at_5"), "mrr": (line.get("retrieval") or {}).get("mrr"),
            "faithfulness": (line.get("ragas") or {}).get("faithfulness"),
            "factual_correctness": (line.get("ragas") or {}).get("factual_correctness"),
            "notes": "; ".join(notes),
        })
    summary = {
        "run": {k: run_config.get(k) for k in ("run_id", "started_at", "finished_at", "index_name", "top_k",
                                                 "temperature", "chat_deployment", "embedding_deployment",
                                                 "guardrail_strictness", "content_safety", "conditions", "git")},
        "n_core_full": len(core_full),
        "scorecard": scorecard(core_full),
        "behaviour": [_behaviour_row(l) for l in sorted(behaviour, key=lambda l: (l["id"], l["route"], l["repeat"], l["turn"]))],
        "ops": ops(lines),
        "per_question": per_question,
        "ragas": ragas_info,
    }
    if any(l["condition"] == "no_xlsx" for l in core):
        summary["ablation"] = ablation(core)
    return summary


def _behaviour_row(line: dict) -> dict:
    if "behaviour" in line:
        check = line["behaviour"]
        return {"id": line["id"], "route": line["route"], "repeat": line["repeat"], "turn": line["turn"],
                "status": check["status"], "layer": check.get("layer"), "note": check.get("note", "")}
    score = line["answer_score"]  # Q17: a session-memory turn
    if line["answer_correct"] is None:
        status, note = "error", f"{line['error']['type']}: {line['error'].get('message', '')[:80]}"
    elif line["answer_correct"]:
        status, note = "pass", ""
    else:
        status = "fail"
        note = line["outcome"] + (f"; missed {', '.join(score['numbers_missed'])}" if score["numbers_missed"] else "")
    return {"id": line["id"], "route": line["route"], "repeat": line["repeat"], "turn": line["turn"],
            "status": status, "layer": None, "note": note}


# ---- Markdown -------------------------------------------------------------------------------------

def _fmt(value, digits=2):
    return "–" if value is None else f"{value:.{digits}f}"


def _fmt_cell(cell):
    if "count" in cell:
        return f"{cell['count']}/{cell['n']}"
    if cell["value"] is None:
        return "–"
    text = _fmt(cell["value"])
    return text if cell["n"] == cell["of"] else f"{text} (n={cell['n']})"


def render_markdown(summary: dict) -> str:
    run = summary["run"]
    out = ["# Evaluation scorecard", ""]
    out.append(f"Run `{run.get('run_id')}` · {run.get('started_at')} · index `{run.get('index_name')}` · "
               f"top_k {run.get('top_k')} · temperature {run.get('temperature')} · model "
               f"`{run.get('chat_deployment')}` · n = {summary['n_core_full']} answers (Q1–15)")
    out.append("")
    out.append("*A smoke test, not a benchmark: 15 questions, and RAGAS scores come from the same model "
               "grading its own answers. See the caveats at the end.*")
    out.append("")
    out.append("## Scorecard (all scores 0–1, higher is better; false refusals are counts)")
    out.append("")
    header = "| | " + " | ".join(label for _, label in GROUP_COLUMNS) + " |"
    out += [header, "|---|" + "---|" * len(GROUP_COLUMNS)]
    for row, cells in summary["scorecard"].items():
        out.append(f"| {row} | " + " | ".join(_fmt_cell(cells[g]) for g, _ in GROUP_COLUMNS) + " |")
    ragas = summary.get("ragas") or {}
    if ragas.get("status"):
        out += ["", f"RAGAS: {ragas['status']}"]

    out += ["", "## Behaviour checks", "", "| Q | route | turn | result | stopped by | note |", "|---|---|---|---|---|---|"]
    for row in summary["behaviour"]:
        rep = f" (r{row['repeat']})" if row["repeat"] > 1 else ""
        out.append(f"| {row['id']}{rep} | {row['route']} | {row['turn']} | **{row['status']}** | "
                   f"{row['layer'] or '–'} | {row['note'] or ''} |")

    o = summary["ops"]
    out += ["", "## Ops", "", "| | p50 (s) | p95 (s) | n |", "|---|---|---|---|"]
    names = {"total": "whole question", "guardrail": "guardrail agent", "supervisor_retrieval": "retrieval agent",
             "intent_identifier": "intent agent", "answer_generator": "answer agent"}
    for key, label in names.items():
        lat = o["latency_s"][key]
        out.append(f"| {label} | {_fmt(lat['p50'])} | {_fmt(lat['p95'])} | {lat['n']} |")
    t = o["tokens"]
    out.append("")
    out.append(f"Tokens (app, graph route): {t['sum_total']:,} in total over {t['records_with_counts']} records, "
               f"mean {t['mean_total'] if t['mean_total'] is not None else '–'} per record. "
               f"Retries by the openai SDK: {o['openai_retries']}. "
               f"Errors: {', '.join(f'{k} ×{v}' for k, v in o['errors'].items()) or 'none'}.")
    if ragas.get("usage"):
        u = ragas["usage"]
        out.append(f"Judge (RAGAS): {u.get('chat_requests', 0)} chat and {u.get('embedding_requests', 0)} embedding "
                   f"requests, {u.get('total_tokens', 0):,} tokens, {u.get('throttled', 0)} throttled (429).")

    out += ["", "## Per question (full condition)", "",
            "| Q | question | correct | hit@5 | MRR | faithfulness | factual correctness | notes |",
            "|---|---|---|---|---|---|---|---|"]
    for row in summary["per_question"]:
        q = row["question"] if len(row["question"]) <= 60 else row["question"][:57] + "…"
        rep = f" (r{row['repeat']})" if row["repeat"] > 1 else ""
        correct = "–" if row["correct"] is None else ("yes" if row["correct"] else "no")
        out.append(f"| {row['id']}{rep} | {q} | {correct} | {_fmt(row['hit_at_5'], 0)} | "
                   f"{_fmt(row['mrr'])} | {_fmt(row['faithfulness'])} | {_fmt(row['factual_correctness'])} | "
                   f"{row['notes']} |")

    if "ablation" in summary:
        a = summary["ablation"]
        out += ["", "## XLSX ablation: what the A2A ingestion adds", ""]
        if a["leaks"]:
            out += [f"> **Warning: the filter leaked.** Workbook chunks still came back under no_xlsx for "
                    f"Q{', Q'.join(map(str, a['leaks']))}, so those questions are left out of this comparison. "
                    "Check the `file_type` values in the index before trusting the ablation.", ""]
        out += [ABLATION_NOTE, ""]
        out += ["| | " + " | ".join(a["groups"][g] for g in a["table"]) + " |", "|---|" + "---|" * len(a["table"])]
        rows = (("answer correct, with XLSX", "correct_full"), ("answer correct, without XLSX", "correct_no_xlsx"),
                ("difference", "difference"), ("key-number accuracy, with XLSX", "key_numbers_full"),
                ("key-number accuracy, without XLSX", "key_numbers_no_xlsx"))
        if any(v.get("faithfulness_no_xlsx") is not None for v in a["table"].values()):
            rows += (("faithfulness, with XLSX", "faithfulness_full"), ("faithfulness, without XLSX", "faithfulness_no_xlsx"))
        for label, key in rows:
            cells = []
            for g, v in a["table"].items():
                value = v[key]
                cells.append("–" if value is None else (f"{value:+.2f}" if key == "difference" else f"{value:.2f}"))
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        out += ["", "Outcomes (count of answers):", "", "| condition | " + " | ".join(OUTCOMES) + " |",
                "|---|" + "---|" * len(OUTCOMES)]
        for cond in ("full", "no_xlsx"):
            counts = a["outcomes"].get(cond, {})
            out.append(f"| {cond} | " + " | ".join(str(counts.get(k, 0)) for k in OUTCOMES) + " |")
        out += ["", "Under no_xlsx, *wrong* means the model stated figures it couldn't have known (the hallucination "
                "signal), and *declined* is the honest failure.", "",
                "| Q | group | full → no_xlsx | no_xlsx hit@5 (non-XLSX sources) |", "|---|---|---|---|"]
        for row in a["per_question"]:
            out.append(f"| {row['id']} | {row['group']} | {', '.join(row['full'])} → {', '.join(row['no_xlsx'])} | "
                       f"{'n/a' if row['no_xlsx_hit_at_5'] is None else _fmt(row['no_xlsx_hit_at_5'], 0)} |")

    out += ["", "## Caveats", ""]
    out += [f"- {c}" for c in CAVEATS]
    out.append(f"- Temperature {run.get('temperature')} for the app's agents"
               + (" (repeatable runs)." if run.get("temperature") == 0 else "; answers vary between runs."))
    return "\n".join(out) + "\n"
