"""Load and check the golden set (evaluation/golden_set.yaml) and plan the runs of both evals.

The golden set feeds the retrieval eval (each question's `evidence` units) and the answer eval
(`reference`, `numbers`, `mentions`, `forbid`, and the behaviour checks). The header of golden_set.yaml
documents every field. load_gold() rejects a malformed file, and an item whose reference fails its own
checks, so a typo in a figure is caught when the file is loaded rather than in a scorecard.
"""

import csv
import hashlib
import re
from pathlib import Path
from typing import Optional

import yaml

from evaluation.evidence import alternatives, files_needed, names_spreadsheet, needs, required_files, unit_files
from evaluation.scoring import parse_one, score_answer

ROOT = Path(__file__).resolve().parents[1]
GOLD_FILE = Path(__file__).resolve().parent / "golden_set.yaml"
INDEX_SELECTION = ROOT / "datasets" / "ai-earnings-desk" / "index_selection.csv"

KINDS = ("lookup", "superlative", "trend", "calendar", "guidance", "explanation", "compare", "aggregate",
         "reconcile", "multi_hop", "unanswerable", "follow_up", "refuse_irrelevant", "refuse_unsafe", "block_input")
EXPECTS = ("answer", "decline", "refuse_irrelevant", "refuse_unsafe", "block_input")
BEHAVIOUR = ("refuse_irrelevant", "refuse_unsafe", "block_input")
# The expect each kind goes with (every other kind expects an answer)
KIND_EXPECT = {"unanswerable": "decline", **{kind: kind for kind in BEHAVIOUR}}
ROUTES = ("graph", "api")
COMPANIES = ("NVIDIA", "AMD", "Broadcom", "Intel", "Micron", "Microsoft", "Alphabet", "Meta")
DIFFICULTIES = ("easy", "medium", "hard")
# Kinds that are never a direct lookup, so never easy
NOT_EASY_KINDS = ("superlative", "trend", "calendar", "compare", "aggregate", "reconcile", "multi_hop", "follow_up")
# full: the app as is. no_xlsx: workbook chunks hidden from search (the XLSX ablation).
# gold: the answer agent gets the evidence chunks from an index snapshot (perfect retrieval).
CONDITIONS = ("full", "no_xlsx", "gold")
# The gold condition hands the answer agent one chunk per fact, at most this many (the app's top_k)
GOLD_CONTEXT_CHUNKS = 5
# Vertical questions read one column down many workbook rows. The Excel narrator writes one sentence per row,
# so the rows are separate passages: a vertical question lists every row it reads as an evidence unit, and the
# gold condition hands over every row's chunk, up to this many (more than the app's top_k, on purpose)
VERTICAL_OPS = ("list", "max_min", "rank", "count", "filter", "sum", "average", "change", "first")
VERTICAL_KEYS = {"op", "sheet", "chunks"}
VERTICAL_MAX_UNITS = 30

ITEM_KEYS = {"id", "question", "kind", "expect", "difficulty", "route", "companies", "reference", "numbers",
             "mentions", "forbid", "evidence", "absent", "turns", "demo", "vertical", "notes"}
TURN_KEYS = {"question", "reference", "numbers", "mentions", "forbid", "evidence"}
NUMBER_KEYS = {"text", "any_of", "required", "sign"}
MENTION_KEYS = {"any", "required"}
UNIT_KEYS = {"fact", "files", "all", "any_of"}
FORM_KEYS = {"files", "all"}


class GoldError(ValueError):
    """The golden set is malformed."""


def load_gold(path=None) -> list[dict]:
    path = Path(path or GOLD_FILE)
    items = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise GoldError(f"{path} must hold a list of items")
    seen, indexed = set(), indexed_files()
    for item in items:
        _check(item, seen, indexed)
    return items


def gold_sha256(path=None) -> str:
    return hashlib.sha256(Path(path or GOLD_FILE).read_bytes()).hexdigest()


def indexed_files() -> Optional[set]:
    """The source_file names of the Tier 1 files (what the index holds), or None without the csv."""
    if not INDEX_SELECTION.exists():
        return None
    with INDEX_SELECTION.open(encoding="utf-8") as f:
        return {Path(row["path"]).name for row in csv.DictReader(f) if row.get("tier") == "1"}


def turns(item: dict) -> list[dict]:
    """The questions sent for an item, in order, each with its own reference and checks."""
    if item.get("turns"):
        return item["turns"]
    return [{key: item.get(key) for key in TURN_KEYS}]


def evidence_units(item: dict) -> list[dict]:
    return [unit for turn in turns(item) for unit in turn.get("evidence") or []]


def item_needs(item: dict) -> str:
    """xlsx, docs, both or none: where the item's evidence lives (evaluation.evidence.needs)."""
    return needs(evidence_units(item))


def is_core_question(item: dict) -> bool:
    """A single-turn answer question on the graph route: what both evals score the same way. The answer
    eval's scorecard and judge cover these, and both reports' breakdowns by difficulty, files needed and
    file do (a follow-up's difficulty describes its whole conversation, which the retrieval eval never
    runs)."""
    return item["expect"] == "answer" and not item.get("turns") and "graph" in item["route"]


def item_files_needed(item: dict) -> int:
    """How many files the item's first question needs (evaluation.evidence.files_needed): 2 or more makes
    it a multi-file question. The first turn is what both evals send on its own."""
    return files_needed(turns(item)[0].get("evidence") or [])


def item_required_files(item: dict) -> list[str]:
    """The files the item's first question can't be answered without: each is the only source of some
    fact (evaluation.evidence.required_files). The per-file coverage counts these, over the core
    questions (is_core_question)."""
    return required_files(turns(item)[0].get("evidence") or [])


def is_vertical(item: dict) -> bool:
    """A vertical question: it reads one workbook column down many rows (the item's `vertical` field)."""
    return bool(item.get("vertical"))


def gold_context_chunks(item: dict) -> int:
    """How many chunks the gold condition may hand the answer agent for this item: every row's chunk for a
    vertical question, else the app's top_k."""
    return VERTICAL_MAX_UNITS if is_vertical(item) else GOLD_CONTEXT_CHUNKS


def item_vertical(item: dict) -> Optional[dict]:
    """What the reports group a vertical question by ({op, chunks}), or None for any other question."""
    vertical = item.get("vertical")
    return {"op": vertical["op"], "chunks": vertical["chunks"]} if vertical else None


def vertical_cell(vertical: Optional[dict]) -> Optional[str]:
    """A vertical question's operation and chunks for a report's per-question table: 'max_min · 8 chunks'."""
    if not vertical:
        return None
    return f"{vertical['op']} · {vertical['chunks']} chunk" + ("s" if vertical["chunks"] != 1 else "")


def item_sources(item: dict) -> list[str]:
    """Every file any of the item's evidence units names."""
    files = []
    for unit in evidence_units(item):
        files += [f for f in unit_files(unit) if f not in files]
    return files


def is_behaviour(item: dict) -> bool:
    return item["expect"] in BEHAVIOUR


def parse_ids(spec: Optional[str]) -> Optional[set[int]]:
    """'1,8,17', '1-15' or '1-7,11' -> a set of ids. None or '' means all."""
    if not spec:
        return None
    ids = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high:
                raise ValueError(f"bad range {part!r}")
            ids.update(range(low, high + 1))
        elif part.isdigit():
            ids.add(int(part))
        else:
            raise ValueError(f"bad id {part!r}: use numbers and ranges, e.g. 1,8,17 or 1-15")
    return ids


def check_ids(items: list[dict], only: Optional[set[int]]) -> None:
    if only:
        missing = only - {item["id"] for item in items}
        if missing:
            raise ValueError(f"no golden-set item with id(s): {', '.join(map(str, sorted(missing)))}")


def condition_applies(item: dict, route: str, condition: str) -> Optional[str]:
    """None if the item runs on this route under this condition, else the reason it doesn't."""
    if condition == "full":
        return None
    if route != "graph":
        return "API route, full only"
    if item["expect"] != "answer":
        return "behaviour check, full only" if is_behaviour(item) else "unanswerable question, full only"
    return None


def build_plan(items: list[dict], only: Optional[set[int]] = None, conditions=("full",),
               repeat: int = 1) -> tuple[list[dict], list[str]]:
    """Order the answer eval's runs: per item and repeat, each route, each condition in turn.

    Interleaving the conditions question by question means any drift (quota, a model update) hits
    them equally. Returns (entries, notes); notes list what was skipped and why.
    """
    unknown = set(conditions) - set(CONDITIONS)
    if unknown:
        raise ValueError(f"unknown condition(s): {', '.join(sorted(unknown))}")
    check_ids(items, only)
    entries, notes = [], []
    for item in items:
        if only and item["id"] not in only:
            continue
        for rep in range(1, repeat + 1):
            for route in item["route"]:
                for condition in conditions:
                    reason = condition_applies(item, route, condition)
                    if reason:
                        if rep == 1 and route == item["route"][0]:
                            notes.append(f"Q{item['id']} skipped under {condition}: {reason}")
                        continue
                    entries.append({"item": item, "route": route, "condition": condition, "repeat": rep})
    return entries, notes


def retrieval_queries(items: list[dict], only: Optional[set[int]] = None) -> list[dict]:
    """What the retrieval eval searches for: every answer item's question, with its evidence.

    A follow-up item contributes its first turn only: later turns ("And a year earlier?") mean nothing
    on their own, and the app rewrites them before searching.
    """
    check_ids(items, only)
    queries = []
    for item in items:
        if item["expect"] != "answer" or (only and item["id"] not in only):
            continue
        first = turns(item)[0]
        queries.append({"id": item["id"], "turn": 1, "question": first["question"],
                        "evidence": first.get("evidence") or [], "item": item})
    return queries


# ---- Validation -----------------------------------------------------------------------------------

def _regexes(where: str, value, what: str) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(p, str) and p for p in value):
        raise GoldError(f"{where}: {what} must be a non-empty list of regexes")
    for pattern in value:
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise GoldError(f"{where}: bad regex {pattern!r}: {e}") from e
        if compiled.search(""):
            raise GoldError(f"{where}: regex {pattern!r} matches the empty string (an empty alternative?)")


def _unknown_keys(where: str, entry, allowed: set) -> None:
    if not isinstance(entry, dict):
        raise GoldError(f"{where}: expected a mapping, got {type(entry).__name__}")
    extra = set(entry) - allowed
    if extra:
        raise GoldError(f"{where}: unknown key(s) {', '.join(sorted(extra))}")


def _required_flag(where: str, entry: dict) -> None:
    if not isinstance(entry.get("required", True), bool):
        raise GoldError(f"{where}: `required` must be true or false")


def _check_turn(where: str, turn: dict, expect: str, indexed: Optional[set], absent=(), vertical=False) -> None:
    if not isinstance(turn.get("question"), str) or not turn["question"].strip():
        raise GoldError(f"{where}: missing question")
    if expect in ("answer", "decline") and not (isinstance(turn.get("reference"), str) and turn["reference"].strip()):
        raise GoldError(f"{where}: missing reference")
    for entry in turn.get("numbers") or []:
        _unknown_keys(f"{where} number", entry, NUMBER_KEYS)
        _required_flag(f"{where} number", entry)
        texts = entry.get("any_of") if "any_of" in entry else [entry.get("text")]
        if ("text" in entry) == ("any_of" in entry) or not isinstance(texts, list) or not texts:
            raise GoldError(f"{where}: a number needs `text` or a non-empty `any_of` list, not both")
        for text in texts:
            if not isinstance(text, str):
                raise GoldError(f"{where}: write the number {text!r} as text, e.g. \"$89.0B\" or \"117%\"")
            try:
                parsed = parse_one(text)
            except ValueError as e:
                raise GoldError(f"{where}: bad number {text!r}: {e}") from e
            if parsed.negative and entry.get("sign") != "negative":
                raise GoldError(f"{where}: {text!r} is negative: write it unsigned with sign: negative")
        if entry.get("sign", "negative") != "negative":
            raise GoldError(f"{where}: `sign` can only be negative")
    for entry in turn.get("mentions") or []:
        _unknown_keys(f"{where} mention", entry, MENTION_KEYS)
        _required_flag(f"{where} mention", entry)
        _regexes(f"{where} mention", entry.get("any"), "`any`")
    if turn.get("forbid"):
        _regexes(f"{where} forbid", turn["forbid"], "`forbid`")
    units = turn.get("evidence") or []
    if expect == "answer" and not units:
        raise GoldError(f"{where}: an answer needs evidence (the retrieval gold)")
    if vertical and len(units) > VERTICAL_MAX_UNITS:
        raise GoldError(f"{where}: at most {VERTICAL_MAX_UNITS} evidence units (one per row a vertical question "
                        "reads)")
    if not vertical and len(units) > GOLD_CONTEXT_CHUNKS:
        raise GoldError(f"{where}: at most {GOLD_CONTEXT_CHUNKS} evidence units (the gold condition hands the "
                        "answer agent that many chunks); a vertical question may list more")
    for n, unit in enumerate(units):
        unit_where = f"{where} evidence[{n}]"
        _unknown_keys(unit_where, unit, UNIT_KEYS)
        if ("any_of" in unit) == ("all" in unit or "files" in unit):
            raise GoldError(f"{unit_where}: give `files` and `all`, or an `any_of` list of forms, not both")
        if "any_of" in unit and not isinstance(unit["any_of"], list):
            raise GoldError(f"{unit_where}: `any_of` must be a list of forms")
        for form in alternatives(unit):
            if "any_of" in unit:
                _unknown_keys(f"{unit_where} form", form, FORM_KEYS)
            files = form.get("files")
            if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
                raise GoldError(f"{unit_where}: `files` must be a non-empty list of source file names")
            if indexed is not None and set(files) - indexed:
                raise GoldError(f"{unit_where}: not in the index (index_selection.csv, tier 1): "
                                f"{', '.join(sorted(set(files) - indexed))}")
            _regexes(unit_where, form.get("all"), "`all`")

    # The reference must pass its own checks, and repeating the question must earn nothing
    question, reference = turn["question"], turn.get("reference") or ""
    if expect == "answer":
        if not any(e.get("required", True) for e in (turn.get("numbers") or []) + (turn.get("mentions") or [])):
            raise GoldError(f"{where}: an answer needs at least one required number or mention")
        own = score_answer(turn, reference)
        if not own["all_required"]:
            missed = own["numbers_missed"] + own["mentions_missed"]
            raise GoldError(f"{where}: the reference fails its own checks (missing {', '.join(missed)})")
        if own["forbidden"]:
            raise GoldError(f"{where}: a forbid pattern matches the reference: {own['forbidden'][0]}")
        echo = score_answer(turn, question)
        if echo["some_required"]:
            hits = [label for label in echo["required_found"]]
            raise GoldError(f"{where}: the question already contains required item(s): {', '.join(hits)}")
    elif expect == "decline":
        own = score_answer(turn, reference, absent=absent)
        if not own["declines"]:
            raise GoldError(f"{where}: a decline item's reference must say the documents don't hold the answer")
        if own["forbidden"] or own["asserts_absent"]:
            raise GoldError(f"{where}: the reference asserts its own forbid/absent pattern: "
                            f"{(own['forbidden'] + own['asserts_absent'])[0]}")


def _check(item: dict, seen: set, indexed: Optional[set]) -> None:
    where = f"golden-set item {item.get('id')!r}" if isinstance(item, dict) else "golden-set item"
    _unknown_keys(where, item, ITEM_KEYS)
    if not isinstance(item.get("id"), int) or isinstance(item["id"], bool) or item["id"] in seen:
        raise GoldError(f"{where}: ids must be unique integers")
    seen.add(item["id"])
    if item.get("kind") not in KINDS:
        raise GoldError(f"{where}: kind must be one of {KINDS}")
    if item.get("expect") not in EXPECTS:
        raise GoldError(f"{where}: expect must be one of {EXPECTS}")
    route = item.get("route")
    if not isinstance(route, list) or not route or not set(route) <= set(ROUTES) or len(set(route)) != len(route):
        raise GoldError(f"{where}: route must be a list drawn from {ROUTES}")
    if not set(item.get("companies") or []) <= set(COMPANIES):
        raise GoldError(f"{where}: companies must be drawn from {COMPANIES}")
    if not isinstance(item.get("question"), str) or not item["question"].strip():
        raise GoldError(f"{where}: missing question (for a follow-up, a summary of its turns)")
    if item.get("turns") is not None:
        if item["kind"] != "follow_up" or not isinstance(item["turns"], list) or len(item["turns"]) < 2:
            raise GoldError(f"{where}: `turns` is for follow_up items, with two or more turns")
        if "api" not in route or route != ["api"]:
            raise GoldError(f"{where}: a follow-up item needs route [api] (session memory is API-only)")
        for key in TURN_KEYS - {"question"}:
            if item.get(key):
                raise GoldError(f"{where}: put `{key}` inside each turn of a follow-up item")
    elif item["kind"] == "follow_up":
        raise GoldError(f"{where}: a follow_up item needs `turns`")
    expected_expect = KIND_EXPECT.get(item["kind"], "answer")
    if item["expect"] != expected_expect:
        raise GoldError(f"{where}: kind {item['kind']} goes with expect {expected_expect}, not {item['expect']}")
    for n, turn in enumerate(turns(item), 1):
        if item.get("turns"):
            _unknown_keys(f"{where} turn {n}", turn, TURN_KEYS)
        _check_turn(f"{where}" + (f" turn {n}" if item.get("turns") else ""), turn, item["expect"], indexed,
                    item.get("absent") or (), vertical=is_vertical(item))
    if item["expect"] == "decline":
        if item.get("numbers") or item.get("mentions") or item.get("evidence"):
            raise GoldError(f"{where}: a decline item has no numbers, mentions or evidence")
        _regexes(f"{where} absent", item.get("absent"), "`absent`")
    elif item.get("absent"):
        raise GoldError(f"{where}: `absent` is for decline items")
    if item["expect"] in BEHAVIOUR and (item.get("numbers") or item.get("mentions") or item.get("evidence")):
        raise GoldError(f"{where}: a behaviour check has no numbers, mentions or evidence")
    if not isinstance(item.get("demo", False), bool):
        raise GoldError(f"{where}: `demo` must be true or false")
    if "vertical" in item:
        _check_vertical(where, item)
    _check_difficulty(where, item)


def _check_vertical(where: str, item: dict) -> None:
    vertical = item["vertical"]
    _unknown_keys(f"{where} vertical", vertical, VERTICAL_KEYS)
    if set(vertical) != VERTICAL_KEYS:
        raise GoldError(f"{where}: `vertical` needs {', '.join(sorted(VERTICAL_KEYS))}")
    if not is_core_question(item):
        raise GoldError(f"{where}: a vertical question is a single-turn answer question on the graph route")
    if vertical["op"] not in VERTICAL_OPS:
        raise GoldError(f"{where}: vertical op must be one of {VERTICAL_OPS}")
    if not isinstance(vertical["sheet"], str) or not vertical["sheet"].strip():
        raise GoldError(f"{where}: vertical sheet must name the workbook sheet the question reads")
    units = item.get("evidence") or []
    chunks = vertical["chunks"]
    if not isinstance(chunks, int) or isinstance(chunks, bool) or not 1 <= chunks <= len(units):
        raise GoldError(f"{where}: vertical chunks must be a whole number from 1 to the number of evidence units "
                        "(the fewest index chunks that hold them all)")
    if sum(1 for unit in units if names_spreadsheet(unit)) < 2:
        raise GoldError(f"{where}: a vertical question reads two or more workbook rows (one evidence unit per row)")


def _check_difficulty(where: str, item: dict) -> None:
    """The label is the author's call (golden_set.yaml's header has the rubric); the extremes are checked:
    an easy question is a direct lookup in one file, and one that needs three or more files is hard. A
    vertical question is easy only when it reads its rows off one chunk, and hard exactly when its rows need
    more chunks than the app retrieves, when it adds or averages them, or when it needs a second file."""
    level = item.get("difficulty")
    if level not in DIFFICULTIES:
        raise GoldError(f"{where}: difficulty must be one of {DIFFICULTIES}")
    files = max(files_needed(turn.get("evidence") or []) for turn in turns(item))
    if level == "easy" and item["kind"] in NOT_EASY_KINDS:
        raise GoldError(f"{where}: a {item['kind']} question isn't easy (easy is a direct lookup)")
    if level == "easy" and files > 1:
        raise GoldError(f"{where}: a question that needs {files} files isn't easy")
    if files >= 3 and level != "hard":
        raise GoldError(f"{where}: a question that needs {files} files is hard")
    vertical = item.get("vertical")
    if not vertical:
        return
    if level == "easy" and (vertical["op"] != "list" or vertical["chunks"] != 1):
        raise GoldError(f"{where}: an easy vertical question reads its rows off one chunk (op list, chunks 1)")
    hard = vertical["chunks"] > GOLD_CONTEXT_CHUNKS or vertical["op"] in ("sum", "average") or files > 1
    if level == "hard" and not hard:
        raise GoldError(f"{where}: a hard vertical question needs more than {GOLD_CONTEXT_CHUNKS} chunks, adds or "
                        "averages its rows, or needs a second file")
    if level != "hard":
        if vertical["chunks"] > GOLD_CONTEXT_CHUNKS:
            raise GoldError(f"{where}: a vertical question whose rows need {vertical['chunks']} chunks (more than "
                            f"the app's top {GOLD_CONTEXT_CHUNKS}) is hard")
        if vertical["op"] in ("sum", "average"):
            raise GoldError(f"{where}: a vertical {vertical['op']} is hard (the answer computes it from every row)")
        if files > 1:
            raise GoldError(f"{where}: a vertical question that needs {files} files is hard")
