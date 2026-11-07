"""Answer eval, stage 2: score each record with the deterministic scorers, then build the scorecard.

score_record() turns one record from records.jsonl into a score line (scores.jsonl). build_summary()
aggregates the score lines into summary.json, and render_markdown() writes summary.md. Pure functions:
no Azure calls, no ragas.

Besides the averages, the report says WHY answers fail: each answer's context (the chunks the answer
agent saw) is checked against the golden set's evidence, so a wrong answer is either a retrieval miss
(the evidence wasn't in its context) or an answer-agent miss (it was).
"""

import statistics
from collections import Counter, defaultdict

from evaluation.evidence import (chunks_label, chunks_order, evidence_scores, files_label, files_needed,
                                 required_files, unit_files)
from evaluation.gold import (DIFFICULTIES, GOLD_CONTEXT_CHUNKS, VERTICAL_MAX_UNITS, VERTICAL_OPS, is_core_question,
                             item_needs, item_vertical, turns, vertical_cell)
from evaluation.scoring import (BEHAVIOUR_CHECKS, FAIL_OPEN_PREFIX, classify_outcome, false_refusal, record_failed,
                                score_answer)

GROUP_COLUMNS = (("all", "All"), ("xlsx", "Needs a workbook"), ("docs", "Documents"), ("both", "Cross-source"),
                 ("vertical", "Vertical"))
OUTCOMES = ("correct", "partial", "declined", "missed", "wrong", "refused", "error")
DECLINE_OUTCOMES = ("correct", "wrong", "refused", "error")
JUDGE_LABELS = {
    "faithfulness": "Faithfulness (judge)",
    "factual_correctness": "Factual correctness F1 (judge)",
    "reference_coverage": "Reference coverage (judge)",
    "response_relevancy": "Response relevancy (judge)",
    "context_precision": "Context precision (judge)",
    "context_recall": "Context recall (judge)",
}
OUTCOME_NOTE = ("*correct*: every required figure and mention. *partial*: some. *declined*: none, and the answer "
                "says the information isn't there (the honest failure). *missed*: none, without a clean decline "
                "(vague, or other figures instead). *wrong*: asserts a known-wrong conclusion (a forbid pattern). "
                "*refused*: rejected or empty. *error*: the run failed, so no verdict.")
ABLATION_NOTE = ("Simulates \"before XLSX ingestion\" by hiding every spreadsheet chunk from search; the index was "
                 "not modified. Groups come from each question's evidence: *needs a workbook* has a fact only a "
                 "workbook states; *documents* can be answered from PDFs, HTML and DOCX alone (the control); "
                 "*cross-source* needs both; *vertical* is the vertical questions, also counted where their facts "
                 "live.")


def record_key(record: dict) -> tuple:
    return (record["id"], record["turn"], record["repeat"], record["condition"], record["route"])


def is_core(record: dict, item: dict) -> bool:
    """Single-turn answer questions on the graph route: the questions the scorecard and the judge cover."""
    return is_core_question(item) and record["route"] == "graph"


def _error_type(record: dict):
    return (record.get("error") or {}).get("type")


def _fact_label(unit: dict, n: int) -> str:
    return unit.get("fact") or f"evidence[{n}]"


def score_record(record: dict, item: dict) -> dict:
    turn = turns(item)[record["turn"] - 1]
    units = turn.get("evidence") or []
    line = {
        "id": record["id"], "turn": record["turn"], "repeat": record["repeat"], "condition": record["condition"],
        "route": record["route"], "kind": item["kind"], "expect": item["expect"], "needs": item_needs(item),
        "difficulty": item.get("difficulty"), "files_needed": files_needed(units), "required_files": required_files(units),
        "vertical": item_vertical(item),
        "companies": item.get("companies") or [], "question": record["question"], "error": record.get("error"),
        "timings_s": record.get("timings_s"), "tokens": record.get("tokens"), "llm_calls": record.get("llm_calls"),
        "llm_errors": record.get("llm_errors") or [], "openai_retries": record.get("openai_retries", 0),
        "retrieval_query": record.get("retrieval_query"), "conversation_turn": record.get("conversation_turn"),
        "guardrail_fail_open": (record.get("guardrail_reason") or "").startswith(FAIL_OPEN_PREFIX),
        "notes": list(record.get("notes") or []), "follow_up": bool(item.get("turns")),
    }
    failed = record_failed(record)
    if item["expect"] in ("answer", "decline"):
        # The answer may repeat the question's figures and, in a conversation, the earlier turns'
        earlier = turns(item)[:record["turn"] - 1]
        context = [turn["question"]] + [t[k] or "" for t in earlier for k in ("question", "reference")]
        score = score_answer(turn, record.get("answer") or "", context=context, absent=item.get("absent") or ())
        outcome = classify_outcome(record, score, item["expect"])
        if failed:  # a failure of the run says nothing about the answer: missing, not 0
            score = {**score, "key_numbers": None, "mentions": None}
        line.update({
            "answer_score": score, "outcome": outcome,
            "answer_correct": None if failed else (1.0 if outcome == "correct" else 0.0),
            "false_refusal": None if failed else false_refusal(record),
        })
        # An answer-agent failure happens after retrieval, so its context still counts. Every unit counts,
        # even under no_xlsx: the question is whether the answer agent had what it needed, and a fact only
        # a workbook states is missing then (the retrieval eval leaves such facts out instead). A question
        # the guardrail or Content Safety stopped never reached retrieval: no evidence check
        stopped = ((record.get("guardrail_enabled", True) and record.get("guardrail_passed") is False)
                   or (record.get("intent") or "") in ("blocked", "rejected"))
        if units and not stopped and (not failed or _error_type(record) == "agent_error"):
            chunks = record.get("retrieved") or []
            evidence = evidence_scores(units, chunks, k=max(1, len(chunks)))
            if evidence is not None:
                evidence["missed_facts"] = [_fact_label(units[i], i) for i in evidence["missed"]]
                # Each required file: did the context hold every fact only that file states?
                line["per_file"] = {source: 0.0 if any(unit_files(units[i]) == [source] for i in evidence["missed"])
                                    else 1.0 for source in line["required_files"]}
            line["evidence"] = evidence
        if record["route"] == "api" and record["turn"] > 1:
            # The API counted this as a later turn of the same session: memory carried the conversation
            line["memory"] = None if failed else record.get("conversation_turn") == record["turn"]
    else:
        line["behaviour"] = BEHAVIOUR_CHECKS[item["expect"]](record)
    return line


# ---- Aggregation ----------------------------------------------------------------------------------

def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def _cell(lines, getter):
    present = [v for v in (getter(line) for line in lines) if v is not None]
    return {"value": _mean(present), "n": len(present), "of": len(lines)}


def _in_group(line, group):
    """A column: all, where the facts live, or the vertical questions (also counted where their facts live)."""
    if group == "vertical":
        return bool(line.get("vertical"))
    return group == "all" or line["needs"] == group


def _shape(line) -> list:
    """The workbook questions as vertical (one column down many rows) or row (each fact in a row); [] for others."""
    if line.get("vertical"):
        return ["vertical"]
    return ["row"] if line["needs"] in ("xlsx", "both") else []


def _evidence(line, key):
    return (line.get("evidence") or {}).get(key)


def judge_outputs(lines) -> list[str]:
    seen = []
    for line in lines:
        for name in line.get("ragas") or {}:
            if name not in seen:
                seen.append(name)
    return [name for name in JUDGE_LABELS if name in seen]


def scorecard(lines: list[dict]) -> dict:
    """Rows x columns of means over the core answers of one condition."""
    rows = {
        "answer correct (strict)": lambda l: l["answer_correct"],
        "key-number accuracy": lambda l: l["answer_score"]["key_numbers"],
        "context held all the evidence": lambda l: _evidence(l, "complete"),
        "context held some evidence": lambda l: _evidence(l, "hit"),
        "correct when the context held all the evidence":
            lambda l: l["answer_correct"] if _evidence(l, "complete") == 1.0 else None,
        "false refusals": None,
    }
    for name in judge_outputs(lines):
        rows[JUDGE_LABELS[name]] = (lambda m: lambda l: (l.get("ragas") or {}).get(m))(name)
    table = {}
    for row, getter in rows.items():
        table[row] = {}
        for group, _ in GROUP_COLUMNS:
            members = [l for l in lines if _in_group(l, group)]
            if getter is None:  # false refusals: a count; app errors don't count
                counted = [l for l in members if l["false_refusal"] is not None]
                table[row][group] = {"count": sum(1 for l in counted if l["false_refusal"]), "n": len(counted),
                                     "of": len(members)}
            else:
                table[row][group] = _cell(members, getter)
    return table


BREAKDOWN_ROWS = (
    ("answer correct", lambda l: l["answer_correct"]),
    ("context held all the evidence", lambda l: _evidence(l, "complete")),
    ("correct when the context held all the evidence",
     lambda l: l["answer_correct"] if _evidence(l, "complete") == 1.0 else None),
)


def breakdown(lines: list[dict], key_of, order=()) -> dict:
    """The core answers grouped by key_of(line) (a list of keys), in `order` first: n (questions, not
    answers: with --repeat each question has several) and the BREAKDOWN_ROWS means over the answers."""
    groups = defaultdict(list)
    for line in lines:
        for key in key_of(line):
            groups[key].append(line)
    ranked = sorted(groups, key=lambda key: (list(order).index(key) if key in order else len(order), str(key)))
    return {key: {"n": len({l["id"] for l in groups[key]}),
                  **{row: _cell(groups[key], getter) for row, getter in BREAKDOWN_ROWS}} for key in ranked}


def per_file(lines: list[dict]) -> dict:
    """For each file some question requires (the only source of one of its facts): how often those answers
    were correct, and how often their context held the facts only that file states."""
    groups = defaultdict(list)
    for line in lines:
        for source in line.get("required_files") or []:
            groups[source].append(line)
    return {source: {"n": len({l["id"] for l in members}),
                     "answer correct": _cell(members, lambda l: l["answer_correct"]),
                     "context held its facts": _cell(members, lambda l, s=source: (l.get("per_file") or {}).get(s))}
            for source, members in sorted(groups.items())}


def attribution(lines: list[dict]) -> dict:
    """Why each non-correct answer failed: refused (the guardrail or Content Safety stopped it, or it was
    empty), its context lacked evidence (retrieval), or its context held it all (answer)."""
    out = {"refused": [], "retrieval": [], "answer": [], "unknown": []}
    for line in lines:
        if line["outcome"] in ("correct", "error"):
            continue
        complete = _evidence(line, "complete")
        if line["outcome"] == "refused":
            side = "refused"
        else:
            side = "unknown" if complete is None else ("answer" if complete == 1.0 else "retrieval")
        out[side].append({"id": line["id"], "repeat": line["repeat"], "outcome": line["outcome"],
                          "missed_facts": _evidence(line, "missed_facts") or []})
    return out


def _percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    return round(statistics.quantiles(values, n=100, method="inclusive")[q - 1], 2)


def ops(lines: list[dict]) -> dict:
    latency = {}
    for route in ("graph", "api"):
        timed = [l for l in lines if l["route"] == route and l.get("timings_s") and l["condition"] != "gold"]
        for key in ("total", "guardrail", "supervisor_retrieval", "intent_identifier", "answer_generator"):
            values = [l["timings_s"][key] for l in timed if l["timings_s"].get(key) is not None]
            if values:
                latency.setdefault(route, {})[key] = {"p50": _percentile(values, 50), "p95": _percentile(values, 95),
                                                      "n": len(values)}
    tokens = {}
    # The gold condition skips the guardrail and search (2 calls), so it isn't the app's cost per question
    for label in sorted({"gold contexts" if l["condition"] == "gold" else l["route"] for l in lines}):
        counted = [l["tokens"] for l in lines if l.get("tokens")
                   and ("gold contexts" if l["condition"] == "gold" else l["route"]) == label]
        if counted:
            tokens[label] = {"records": len(counted), "sum_total": sum(t["total"] for t in counted),
                             "mean_total": round(sum(t["total"] for t in counted) / len(counted))}
    llm_failures = Counter(e.get("node") or "unknown" for l in lines for e in l.get("llm_errors") or [])
    return {
        "latency_s": latency, "tokens": tokens,
        "openai_retries": sum(l.get("openai_retries") or 0 for l in lines),
        "errors": dict(Counter(_error_type(l) for l in lines if l.get("error") and _error_type(l) != "filter_leak")),
        "filter_leaks": sum(1 for l in lines if _error_type(l) == "filter_leak"),
        "llm_failures": dict(llm_failures),
        "fail_opens": sum(1 for l in lines if l.get("guardrail_fail_open")),
    }


def _paired(by_cond: dict, first: str, second: str):
    """(id, repeat) pairs present in both conditions with no failed run and no filter leak in either."""
    a = {(l["id"], l["repeat"]): l for l in by_cond[first]}
    b = {(l["id"], l["repeat"]): l for l in by_cond[second]}
    pairs, excluded = [], []
    for key in sorted(set(a) & set(b)):
        bad = [l for l in (a[key], b[key]) if l.get("error")]
        if bad:
            excluded.append({"id": key[0], "repeat": key[1], "why": ", ".join(sorted({_error_type(l) for l in bad}))})
        else:
            pairs.append((a[key], b[key]))
    return pairs, excluded


def compare(lines: list[dict], first: str, second: str, groups) -> dict:
    """Paired comparison of two conditions over the core answers, by evidence group."""
    by_cond = defaultdict(list)
    for line in lines:
        by_cond[line["condition"]].append(line)
    pairs, excluded = _paired(by_cond, first, second)
    judged = judge_outputs([l for pair in pairs for l in pair])
    getters = [("correct", lambda l: l["answer_correct"]),
               ("key_numbers", lambda l: l["answer_score"]["key_numbers"]),
               ("evidence_complete", lambda l: _evidence(l, "complete"))]
    getters += [(name, (lambda m: lambda l: (l.get("ragas") or {}).get(m))(name)) for name in judged]
    table = {}
    for group, _ in groups:
        members = [(x, y) for x, y in pairs if _in_group(x, group)]
        row = {"n": len(members)}
        for key, getter in getters:
            # Only the pairs where both sides have a value, so both means cover the same questions
            both = [(getter(a), getter(b)) for a, b in members if getter(a) is not None and getter(b) is not None]
            x, y = _mean([v for v, _ in both]), _mean([v for _, v in both])
            row[f"{key}_{first}"], row[f"{key}_{second}"], row[f"{key}_n"] = x, y, len(both)
            if key == "correct":
                row["difference"] = round(y - x, 3) if None not in (x, y) else None
        table[group] = row
    per_question = [{"id": a["id"], "repeat": a["repeat"], "needs": a["needs"], first: a["outcome"],
                     second: b["outcome"], "evidence_" + second: _evidence(b, "complete")} for a, b in pairs]
    leaks = sorted({l["id"] for l in by_cond[second] if _error_type(l) == "filter_leak"})
    return {"conditions": [first, second], "table": table, "judged": judged,
            "outcomes": {cond: dict(Counter(pair[i]["outcome"] for pair in pairs)) for i, cond in enumerate((first, second))},
            "per_question": per_question, "excluded": excluded, "leaks": leaks}


def build_summary(lines: list[dict], items: dict, run_config: dict, judge_info: dict) -> dict:
    core = [l for l in lines if is_core(l, items[l["id"]])]
    core_full = [l for l in core if l["condition"] == "full"]
    declines = [l for l in lines if l["expect"] == "decline" and l["condition"] == "full"]
    conversation = [l for l in lines if l.get("follow_up")]
    behaviour = [l for l in lines if "behaviour" in l]
    top_k = run_config.get("top_k") or GOLD_CONTEXT_CHUNKS  # the vertical questions' chunks are grouped against it
    summary = {
        "run": {k: run_config.get(k) for k in ("run_id", "started_at", "finished_at", "index_name", "top_k",
                                                 "temperature", "chat_deployment", "embedding_deployment",
                                                 "guardrail_enabled", "guardrail_strictness", "content_safety",
                                                 "conditions", "git", "gold_file", "stopped", "records",
                                                 "planned_records")},
        "n_core_full": len({l["id"] for l in core_full}),
        "repeats": max([l["repeat"] for l in core_full] or [1]),
        "scorecard": scorecard(core_full),
        "by_difficulty": breakdown(core_full, lambda l: [l.get("difficulty") or "(none)"], DIFFICULTIES),
        "by_files": breakdown(core_full, lambda l: [files_label(l.get("files_needed", 0))]),
        "per_file": per_file(core_full),
        "by_shape": breakdown(core_full, _shape, ("vertical", "row")),
        "by_vertical_chunks": breakdown(core_full, lambda l: [chunks_label(l["vertical"]["chunks"], top_k)]
                                        if l.get("vertical") else [], chunks_order(top_k)),
        "by_vertical_op": breakdown(core_full, lambda l: [l["vertical"]["op"]] if l.get("vertical") else [],
                                    VERTICAL_OPS),
        # Vertical questions whose rows need more chunks than the app retrieves: their context can't hold every row
        "beyond_top_k": sorted({l["id"] for l in core_full if l.get("vertical") and l["vertical"]["chunks"] > top_k}),
        "outcomes": {cond: dict(Counter(l["outcome"] for l in core if l["condition"] == cond))
                     for cond in sorted({l["condition"] for l in core})},
        "attribution": attribution(core_full),
        "declines": {"n": len(declines), "outcomes": dict(Counter(l["outcome"] for l in declines)),
                     "answered_anyway": [l["id"] for l in declines if l["outcome"] == "wrong"]},
        "conversation": [_conversation_row(l) for l in sorted(conversation, key=lambda l: (l["id"], l["repeat"], l["turn"]))],
        "behaviour": [_behaviour_row(l) for l in sorted(behaviour, key=lambda l: (l["id"], l["route"], l["repeat"]))],
        "ops": ops(lines),
        "per_question": [_question_row(l) for l in sorted(core_full, key=lambda l: (l["id"], l["repeat"]))],
        "judge": judge_info,
    }
    conditions = {l["condition"] for l in core}
    if "no_xlsx" in conditions:
        summary["ablation"] = compare(core, "full", "no_xlsx", GROUP_COLUMNS)
    if "gold" in conditions:
        summary["gold_contexts"] = compare(core, "full", "gold", GROUP_COLUMNS)
    return summary


def _notes(line: dict) -> str:
    notes = []
    score = line.get("answer_score") or {}
    if score.get("numbers_missed"):
        notes.append("missed " + ", ".join(score["numbers_missed"]))
    if score.get("mentions_missed"):
        notes.append("no mention of " + ", ".join(score["mentions_missed"]))
    if score.get("forbidden"):
        notes.append("asserts a forbidden conclusion")
    if score.get("found_only_in_declines"):
        notes.append("only in a declining sentence: " + ", ".join(score["found_only_in_declines"]))
    if line.get("error"):
        notes.append(f"{line['error']['type']}: {line['error'].get('message', '')[:60]}")
    notes.extend(line.get("notes") or [])
    for metric, reason in (line.get("ragas_errors") or {}).items():
        notes.append(f"{metric} not judged ({reason[:50]})")
    return "; ".join(notes)


def _question_row(line: dict) -> dict:
    evidence = line.get("evidence") or {}
    return {"id": line["id"], "repeat": line["repeat"], "question": line["question"], "kind": line["kind"],
            "difficulty": line.get("difficulty"), "files_needed": line.get("files_needed"),
            "needs": line["needs"], "vertical": vertical_cell(line.get("vertical")), "outcome": line["outcome"],
            "evidence": None if not evidence else ("all" if evidence["complete"] else ("some" if evidence["hit"] else "none")),
            "first_evidence_rank": evidence.get("first_rank"), "missed_facts": evidence.get("missed_facts") or [],
            "judge": {k: v for k, v in (line.get("ragas") or {}).items()}, "notes": _notes(line)}


def _conversation_row(line: dict) -> dict:
    return {"id": line["id"], "repeat": line["repeat"], "turn": line["turn"], "outcome": line["outcome"],
            "memory": line.get("memory"), "retrieval_query": line.get("retrieval_query"),
            "evidence": None if not line.get("evidence") else ("all" if line["evidence"]["complete"] else
                                                               ("some" if line["evidence"]["hit"] else "none")),
            "notes": _notes(line)}


def _behaviour_row(line: dict) -> dict:
    check = line["behaviour"]
    return {"id": line["id"], "route": line["route"], "repeat": line["repeat"], "status": check["status"],
            "layer": check.get("layer"), "note": check.get("note", ""), "leaked": check.get("leaked") or []}


# ---- Markdown -------------------------------------------------------------------------------------

def _fmt(value, digits=2):
    return "–" if value is None else f"{value:.{digits}f}"


def _fmt_cell(cell):
    if "count" in cell:
        return f"{cell['count']}/{cell['n']}" if cell["of"] else "–"
    if cell["value"] is None:
        return "–"
    text = _fmt(cell["value"])
    return text if cell["n"] == cell["of"] else f"{text} (n={cell['n']} of {cell['of']})"


def _table(header, rows):
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + \
        ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def _compare_section(out, data, title, note, labels):
    first, second = data["conditions"]
    out += ["", f"## {title}", ""]
    if data["leaks"]:
        out += [f"> **Warning: the filter leaked.** Spreadsheet chunks still came back under {second} for "
                f"Q{', Q'.join(map(str, data['leaks']))}, so those questions are left out. Check the `file_type` "
                "values in the index before trusting this comparison.", ""]
    out += [note, ""]
    groups = [g for g, _ in GROUP_COLUMNS if data["table"][g]["n"]]
    header = [""] + [f"{dict(GROUP_COLUMNS)[g]} (n={data['table'][g]['n']})" for g in groups]
    rows = [(f"answer correct, {labels[0]}", "correct_" + first), (f"answer correct, {labels[1]}", "correct_" + second),
            ("difference", "difference"),
            (f"key-number accuracy, {labels[0]}", "key_numbers_" + first),
            (f"key-number accuracy, {labels[1]}", "key_numbers_" + second),
            (f"context held all the evidence, {labels[0]}", "evidence_complete_" + first),
            (f"context held all the evidence, {labels[1]}", "evidence_complete_" + second)]
    for name in data["judged"]:
        rows += [(f"{JUDGE_LABELS[name]}, {labels[0]}", f"{name}_{first}"),
                 (f"{JUDGE_LABELS[name]}, {labels[1]}", f"{name}_{second}")]
    body = []
    for label, key in rows:
        values = [data["table"][g].get(key) for g in groups]
        if all(v is None for v in values):
            continue
        # A row averages only the pairs where both conditions have a value: say so when that's fewer
        base = "correct" if key == "difference" else key.rsplit("_", 1)[0]
        cells = []
        for g, v in zip(groups, values):
            n_row = data["table"][g].get(f"{base}_n", data["table"][g]["n"])
            text = "–" if v is None else (f"{v:+.2f}" if key == "difference" else f"{v:.2f}")
            cells.append(text if v is None or n_row == data["table"][g]["n"] else f"{text} (n={n_row})")
        body.append([label] + cells)
    out += _table(header, body)
    out += ["", "Outcomes (count of answers):", ""]
    out += _table(["condition"] + list(OUTCOMES),
                  [[cond] + [data["outcomes"].get(cond, {}).get(k, 0) for k in OUTCOMES] for cond in (first, second)])
    out += ["", OUTCOME_NOTE, ""]
    out += _table(["Q", "needs", f"{first} → {second}"],
                  [[f"{r['id']}" + (f" (r{r['repeat']})" if r["repeat"] > 1 else ""), r["needs"],
                    f"{r[first]} → {r[second]}"] for r in data["per_question"]])
    if data["excluded"]:
        out += ["", "Left out (the run failed in one condition): " +
                "; ".join(f"Q{e['id']} ({e['why']})" for e in data["excluded"])]


def _retrieval_settings(run: dict) -> str:
    """The run's search settings beyond top_k, as the scorecard header shows them (older runs have none)."""
    out = f" (hybrid: {run['hybrid_knn']} vector neighbours)" if run.get("hybrid_knn") else ""
    if run.get("workbook_expand"):
        out += (f" · workbook expansion: up to {run['workbook_expand']} more workbook chunks when "
                f"{run.get('workbook_trigger')} of the top {run.get('top_k')} are workbook chunks")
    if run.get("company_routing"):
        out += " · company routing: one search per company a question names (up to 3)"
        if run.get("single_company_top_k"):
            out += f", {run['single_company_top_k']} results for one company"
    return out


def render_markdown(summary: dict) -> str:
    run = summary["run"]
    n, repeats = summary["n_core_full"], summary.get("repeats", 1)
    size = f"{n} answer questions" + (f" × {repeats} repeats" if repeats > 1 else "")
    out = ["# Answer eval scorecard", ""]
    git = run.get("git") or {}
    out.append(f"Run `{run.get('run_id')}` · {run.get('started_at')} · index `{run.get('index_name')}` · "
               f"top_k {run.get('top_k')}" + _retrieval_settings(run) + f" · temperature {run.get('temperature')}"
               f" · model `{run.get('chat_deployment')}`"
               f" · commit `{git.get('commit')}`" + (" (with uncommitted changes)" if git.get("uncommitted_changes") else "")
               + f" · n = {size}")
    if run.get("stopped"):
        out += ["", f"> **This run stopped early ({run['stopped']})**: {run.get('records')} of "
                    f"{run.get('planned_records')} planned records."]
    out += ["", f"*{size} show what works and what doesn't; they are not a benchmark (one question is "
                f"{100 / n:.0f} points of \"answer correct\"). Caveats at the end.*" if n else "*No answer questions.*"]

    out += ["", "## Scorecard (scores 0–1, higher is better; false refusals are counts)", ""]
    header = [""] + [label for _, label in GROUP_COLUMNS]
    out += _table(header, [[row] + [_fmt_cell(cells[g]) for g, _ in GROUP_COLUMNS]
                           for row, cells in summary["scorecard"].items()])
    out += ["", "*Context* is the 5 chunks the answer agent saw; *evidence* is the golden set's facts for that "
                "question (evaluation/evidence.py)."]

    a = summary["attribution"]
    out += ["", "## Where the failures come from", ""]
    if not (a["retrieval"] or a["answer"] or a["unknown"] or a.get("refused")):
        out.append("No failed answers.")
    else:
        def ids(entries):
            return ", ".join(f"Q{e['id']} ({e['outcome']})" for e in entries) or "none"
        if a.get("refused"):
            out.append(f"- **Refused** (the guardrail or Content Safety stopped it, or the answer was empty; no "
                       f"retrieval to judge): {len(a['refused'])}: {ids(a['refused'])}")
        out.append(f"- **Retrieval** (the context lacked some evidence): {len(a['retrieval'])}: {ids(a['retrieval'])}")
        out.append(f"- **Answer agent** (the context held all the evidence): {len(a['answer'])}: {ids(a['answer'])}")
        if a["unknown"]:
            out.append(f"- Not attributable (no evidence check): {ids(a['unknown'])}")

    for title, key in (("By difficulty", "by_difficulty"), ("By files needed", "by_files")):
        rows = summary.get(key)
        if not rows:
            continue
        out += ["", f"## {title} (full condition)", ""]
        out += _table(["", "n"] + [row for row, _ in BREAKDOWN_ROWS],
                      [[name, row["n"]] + [_fmt_cell(row[r]) for r, _ in BREAKDOWN_ROWS] for name, row in rows.items()])
        if key == "by_files":
            out += ["", "*Files needed*: the fewest files whose chunks can hold every fact the answer needs "
                        "(2 or more: a multi-file question)."]
    if summary.get("per_file"):
        out += ["", "## Per file (full condition)", "",
                "For each file, the questions that require it (the file is the only source of one of their facts).", ""]
        out += _table(["file", "n", "answer correct", "context held its facts"],
                      [[source, row["n"], _fmt_cell(row["answer correct"]), _fmt_cell(row["context held its facts"])]
                       for source, row in summary["per_file"].items()])
    if summary.get("by_vertical_op"):
        out += ["", "## Vertical questions (full condition)", "",
                "*A vertical question reads one workbook column down many rows. The Excel narrator writes one sentence "
                "per row, so the answer needs every row's chunk, often more than the app's top_k. Row: the other "
                "questions that need a workbook, each of whose facts one row states.*"]
        for title, key in (("Vertical vs row", "by_shape"), ("By chunks needed", "by_vertical_chunks"),
                           ("By operation", "by_vertical_op")):
            out += ["", f"### {title}", ""]
            out += _table(["", "n"] + [row for row, _ in BREAKDOWN_ROWS],
                          [[name, row["n"]] + [_fmt_cell(row[r]) for r, _ in BREAKDOWN_ROWS]
                           for name, row in summary[key].items()])
        beyond = summary.get("beyond_top_k") or []
        if beyond:
            out += ["", f"{len(beyond)} vertical question(s) need more row chunks than the app's top_k "
                        f"({summary['run'].get('top_k')}): under full, their context holds every row only through "
                        "column views or workbook expansion: Q" + ", Q".join(map(str, beyond)) + "."]
        out += ["", f"Under the gold condition a vertical question's context holds every row it reads (up to "
                    f"{VERTICAL_MAX_UNITS} chunks), more than the app's top_k: full vs gold separates a context too "
                    "small (or a search that misses rows) from an answer agent that can't add the rows up."]

    out += ["", "## Outcomes", ""]
    conds = list(summary["outcomes"])
    out += _table(["condition"] + list(OUTCOMES), [[c] + [summary["outcomes"][c].get(k, 0) for k in OUTCOMES]
                                                    for c in conds])
    out += ["", OUTCOME_NOTE]

    d = summary["declines"]
    if d["n"]:
        out += ["", "## Unanswerable questions (the documents don't hold the answer)", ""]
        out += _table(["n"] + [k.replace("correct", "declined (right)").replace("wrong", "answered anyway")
                               for k in DECLINE_OUTCOMES],
                      [[d["n"]] + [d["outcomes"].get(k, 0) for k in DECLINE_OUTCOMES]])
        if d["answered_anyway"]:
            out += ["", "Answered instead of declining (a hallucination risk): Q" + ", Q".join(map(str, d["answered_anyway"]))]

    if summary["behaviour"]:
        out += ["", "## Behaviour checks", ""]
        out += _table(["Q", "route", "result", "stopped by", "note"],
                      [[f"{r['id']}" + (f" (r{r['repeat']})" if r["repeat"] > 1 else ""), r["route"], f"**{r['status']}**",
                        r["layer"] or "–", r["note"] + (f" ({', '.join(r['leaked'])})" if r["leaked"] else "")]
                       for r in summary["behaviour"]])

    if summary["conversation"]:
        out += ["", "## Conversations (API route, one session per question)", ""]
        out += _table(["Q", "turn", "outcome", "memory", "context evidence", "search query", "notes"],
                      [[f"{r['id']}" + (f" (r{r['repeat']})" if r["repeat"] > 1 else ""), r["turn"], r["outcome"],
                        "–" if r["memory"] is None else ("kept" if r["memory"] else "**lost**"), r["evidence"] or "–",
                        (r["retrieval_query"] or "–")[:70], r["notes"]] for r in summary["conversation"]])
        out += ["", "*memory*: the API counted the turn as part of the same session. A later turn can still fail if "
                    "its search misses the evidence; the search query shows what the app searched for."]

    if "ablation" in summary:
        _compare_section(out, summary["ablation"], "XLSX ablation: what the spreadsheet ingestion adds", ABLATION_NOTE,
                         ("with spreadsheets", "without"))
    if "gold_contexts" in summary:
        _compare_section(out, summary["gold_contexts"], "With perfect retrieval (gold contexts)",
                         "The answer agent gets exactly the evidence chunks from an index snapshot instead of searching "
                         f"(a vertical question every row's chunk, up to {VERTICAL_MAX_UNITS}). "
                         "The gap to the app is what better retrieval could gain; what fails here fails in the answer "
                         "agent.", ("app retrieval", "gold contexts"))

    out += ["", "## Per question (full condition)", ""]
    judged = judge_outputs([{"ragas": r["judge"]} for r in summary["per_question"]])
    out += _table(["Q", "kind", "difficulty", "files", "needs", "vertical", "outcome", "evidence in context",
                   "first rank"] + [JUDGE_LABELS[j].replace(" (judge)", "") for j in judged] + ["notes"],
                  [[f"{r['id']}" + (f" (r{r['repeat']})" if r["repeat"] > 1 else ""), r["kind"],
                    r.get("difficulty") or "–", r.get("files_needed") or "–", r["needs"], r.get("vertical") or "–",
                    r["outcome"],
                    (r["evidence"] or "–") + (f" (missed: {', '.join(r['missed_facts'])})" if r["missed_facts"] else ""),
                    r["first_evidence_rank"] or "–"] + [_fmt(r["judge"].get(j)) for j in judged] + [r["notes"]]
                   for r in summary["per_question"]])

    o = summary["ops"]
    out += ["", "## Ops", ""]
    names = {"total": "whole question", "guardrail": "guardrail agent", "supervisor_retrieval": "retrieval agent",
             "intent_identifier": "intent agent", "answer_generator": "answer agent"}
    rows = []
    for route, latency in o["latency_s"].items():
        for key, label in names.items():
            if key in latency:
                lat = latency[key]
                rows.append([route, label, _fmt(lat["p50"]), _fmt(lat["p95"]), lat["n"]])
    out += _table(["route", "", "p50 (s)", "p95 (s)", "n"], rows)
    tokens = "; ".join(f"{route}: {t['sum_total']:,} over {t['records']} records (mean {t['mean_total']})"
                       for route, t in o["tokens"].items()) or "none counted"
    out += ["", f"Tokens (the app's LLM calls, embeddings not included): {tokens}. Retries by the openai SDK: "
                f"{o['openai_retries']}."]
    errors = ", ".join(f"{k} ×{v}" for k, v in o["errors"].items()) or "none"
    failures = ", ".join(f"{k} ×{v}" for k, v in o["llm_failures"].items()) or "none"
    out.append(f"Failed runs: {errors}. Agents' failed LLM calls (caught by the app): {failures}. "
               f"Guardrail fail-opens: {o['fail_opens']}."
               + (f" No_xlsx filter leaks: {o['filter_leaks']} (left out of the ablation)." if o.get("filter_leaks") else ""))

    judge = summary.get("judge") or {}
    out += ["", "## Judge", "", f"Status: {judge.get('status') or 'not run'}."]
    usage = judge.get("usage")
    if usage:
        out.append(f"Requests: {usage.get('chat_requests', 0)} chat, {usage.get('embedding_requests', 0)} embedding; "
                   f"{usage.get('total_tokens', 0):,} tokens; {usage.get('throttled', 0)} throttled (429); "
                   f"{usage.get('truncated', 0)} cut off at max_tokens.")
    if judge.get("errors"):
        out += ["", "Not judged:", ""]
        out += _table(["Q", "condition", "metric", "reason"],
                      [[e["id"], e["condition"], e["metric"], e["reason"][:120]] for e in judge["errors"]])

    out += ["", "## Caveats", ""]
    t = run.get("temperature")
    caveats = [
        f"{size}: a smoke test that shows what works and what breaks, not a benchmark.",
        "The judge is gpt-4.1-mini, the same model that wrote the answers (the project's only chat deployment). "
        "A model tends to rate its own answers kindly.",
        "Judge scores are estimates. Even at temperature 0 with a fixed seed the same answer can be scored "
        "differently: treat per-answer differences under about 0.1 as noise.",
        "Exact checks: figures may be rounded to the precision written; signs count only where the golden set asks "
        "for a negative figure; figures and mentions inside a sentence that declines don't count.",
        "Evidence is matched by text patterns (evaluation/evidence.py), checked against an index snapshot with "
        "`scripts/eval_retrieval.py check-gold`.",
        f"Temperature {t} for the app's agents" + ("; answers still vary a little between runs at 0, so compare "
                                                     "conditions with --repeat." if t == 0 else "; answers vary between runs."),
    ]
    out += [f"- {c}" for c in caveats]
    return "\n".join(out) + "\n"
