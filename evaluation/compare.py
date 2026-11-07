"""Compare two scored answer-eval runs question by question (`scripts/eval_answers.py compare`).

A run on a subset (`run --only` the questions a baseline got wrong) has its own scorecard, but its
percentages cover only that subset, so they can't be set against the baseline's. This pairs the records
both runs asked (same question, turn, repeat and route, in one condition) and sorts them: fixed (now
fully correct), broke (no longer correct), still failing and still correct, with what the answer agent's
context held of the evidence in each run.
"""

# run_config.json settings that can explain a difference between two runs
SETTINGS = ("index_name", "top_k", "hybrid_knn", "workbook_expand", "workbook_trigger", "company_routing",
            "single_company_top_k", "temperature", "chat_deployment", "embedding_deployment", "guardrail_enabled", "guardrail_strictness")
GROUPS = ("fixed", "broke", "still failing", "still correct")


def outcome(line: dict) -> str:
    """A scored record's outcome (correct, partial, wrong, ...), or its behaviour check's status (pass, fail)."""
    return line.get("outcome") or (line.get("behaviour") or {}).get("status") or "error"


def is_ok(line: dict) -> bool:
    return outcome(line) in ("correct", "pass")


def context(line: dict) -> str:
    """What the context held of the record's evidence: all, some or none ('-' without evidence)."""
    evidence = line.get("evidence") or {}
    if not evidence.get("units"):
        return "-"
    return "all" if evidence.get("complete") == 1 else ("some" if evidence.get("hit") else "none")


def format_ids(ids) -> str:
    """Ids as `run --only` takes them: 1,3-5,8."""
    ids, out = sorted(set(ids)), []
    for i in ids:
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


def _key(line: dict) -> tuple:
    return line["id"], line["turn"], line["repeat"], line["route"]


def _settings(config: dict) -> dict:
    out = {name: config.get(name) for name in SETTINGS}
    out["git commit"] = (config.get("git") or {}).get("commit")
    out["golden set"] = ((config.get("gold_file") or {}).get("sha256") or "")[:12] or None
    out["snapshot chunks"] = (config.get("snapshot") or {}).get("chunks")
    return out


def compare(base_lines: list, new_lines: list, base_config: dict, new_config: dict, condition: str = "full") -> dict:
    """The records both runs scored in one condition, sorted into GROUPS."""
    base = {_key(line): line for line in base_lines if line["condition"] == condition}
    new = {_key(line): line for line in new_lines if line["condition"] == condition}
    groups = {name: [] for name in GROUPS}
    for key in sorted(set(base) & set(new)):
        before, after = base[key], new[key]
        row = {"id": key[0], "turn": key[1], "repeat": key[2], "route": key[3],
               "question": after.get("question") or before.get("question") or "",
               "before": outcome(before), "after": outcome(after),
               "context_before": context(before), "context_after": context(after)}
        group = ("still correct" if is_ok(after) else "broke") if is_ok(before) else \
            ("fixed" if is_ok(after) else "still failing")
        groups[group].append(row)
    rows = [row for name in GROUPS for row in groups[name]]
    with_evidence = [row for row in rows if row["context_after"] != "-"]
    old, now = _settings(base_config), _settings(new_config)
    return {
        "base": base_config.get("run_id"), "run": new_config.get("run_id"), "condition": condition,
        "settings": {name: (old[name], now[name]) for name in old if old[name] != now[name]},
        "groups": groups,
        "context_all": {"before": sum(r["context_before"] == "all" for r in with_evidence),
                        "after": sum(r["context_after"] == "all" for r in with_evidence),
                        "n": len(with_evidence)},
        "only_in_base": len({k[0] for k in set(base) - set(new)}),
        "only_in_run": sorted({k[0] for k in set(new) - set(base)}),
        "not_correct_ids": sorted({r["id"] for r in groups["broke"] + groups["still failing"]}),
    }


def _label(row: dict) -> str:
    return f"Q{row['id']}" + (f" turn {row['turn']}" if row["turn"] > 1 else "") + \
        (f" r{row['repeat']}" if row["repeat"] > 1 else "") + (" (api)" if row["route"] == "api" else "")


def render_markdown(result: dict) -> str:
    groups = result["groups"]
    both = sum(len(rows) for rows in groups.values())
    out = ["# Run comparison", "",
           f"`{result['run']}` against the baseline `{result['base']}` · condition {result['condition']} · "
           f"{both} records in both runs" +
           (f" ({result['only_in_base']} question{'s' if result['only_in_base'] != 1 else ''} only in the baseline)"
            if result["only_in_base"] else ""), ""]
    if result["only_in_run"]:
        out += [f"Not in the baseline, so left out: Q{', Q'.join(map(str, result['only_in_run']))}", ""]
    if result["settings"]:
        out += ["Settings that differ: " + "; ".join(f"{name} {before} → {after}"
                                                      for name, (before, after) in result["settings"].items()), ""]
    else:
        out += ["Settings that differ: none recorded (the same app settings; answers can still vary)", ""]
    out += ["| | records |", "|---|---|"] + [f"| {name} | {len(groups[name])} |" for name in GROUPS]
    ctx = result["context_all"]
    out += ["", f"Context held every fact the answer needs: {ctx['before']} → {ctx['after']} of {ctx['n']} records "
                "with evidence.", ""]
    for name in ("fixed", "broke", "still failing"):
        if not groups[name]:
            continue
        out += [f"## {name.capitalize()} ({len(groups[name])})", "",
                "| question | outcome | context held | |", "|---|---|---|---|"]
        out += [f"| {_label(r)} | {r['before']} → {r['after']} | {r['context_before']} → {r['context_after']} | "
                f"{r['question'][:90].replace('|', '/')} |" for r in groups[name]]
        out.append("")
    if result["not_correct_ids"]:
        out += [f"Not fully correct in this run: `--only {format_ids(result['not_correct_ids'])}`", ""]
    out.append("*context held*: every fact (all), some, or none of the golden set's evidence; '-' for a question "
               "without evidence (a decline or behaviour check).")
    return "\n".join(out) + "\n"
