"""Load the gold questions (evaluation/gold_questions.yaml) and plan which runs to make."""

import hashlib
import re
from pathlib import Path
from typing import Optional

import yaml

from evaluation.scoring import parse_one

GOLD_FILE = Path(__file__).resolve().parent / "gold_questions.yaml"
GROUPS = ("xlsx", "docs", "cross", "behaviour")
ROUTES = ("graph", "api")
EXPECTS = ("answer", "refuse_irrelevant", "refuse_unsafe", "block_input")
CONDITIONS = ("full", "no_xlsx")


class GoldError(ValueError):
    """The gold file is malformed."""


def load_gold(path=None) -> list[dict]:
    path = Path(path or GOLD_FILE)
    items = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise GoldError(f"{path} must hold a list of questions")
    seen = set()
    for item in items:
        _check(item, seen)
    return items


def gold_sha256(path=None) -> str:
    return hashlib.sha256(Path(path or GOLD_FILE).read_bytes()).hexdigest()


def turns(item: dict) -> list[dict]:
    """The questions sent for an item, in order, each with its own reference, numbers and mentions."""
    if item.get("turns"):
        return item["turns"]
    return [{"question": item["question"], "reference": item.get("reference", ""),
             "numbers": item.get("numbers") or [], "mentions": item.get("mentions") or []}]


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


def ablation_eligible(item: dict) -> bool:
    """Only graph-route questions that expect an answer (Q1-Q15) run under no_xlsx."""
    return item["expect"] == "answer" and "graph" in item["route"]


def build_plan(items: list[dict], only: Optional[set[int]] = None, conditions=("full",),
               repeat: int = 1) -> tuple[list[dict], list[str]]:
    """Order the runs: per question and repeat, each route; on the graph route `full` then `no_xlsx`.

    Interleaving the two conditions question by question means any drift (temperature, quota) hits
    both equally. Returns (entries, notes); notes list what was skipped and why.
    """
    unknown = set(conditions) - set(CONDITIONS)
    if unknown:
        raise ValueError(f"unknown condition(s): {', '.join(sorted(unknown))}")
    if only:
        missing = only - {item["id"] for item in items}
        if missing:
            raise ValueError(f"no gold question with id(s): {', '.join(map(str, sorted(missing)))}")
    entries, notes = [], []
    for item in items:
        if only and item["id"] not in only:
            continue
        for rep in range(1, repeat + 1):
            for route in item["route"]:
                for condition in conditions:
                    if condition != "full" and not (route == "graph" and ablation_eligible(item)):
                        if rep == 1 and route == item["route"][0]:
                            notes.append(f"Q{item['id']} skipped under {condition}: "
                                         f"{'API route' if route == 'api' else 'behaviour check'}, full only")
                        continue
                    entries.append({"item": item, "route": route, "condition": condition, "repeat": rep})
    return entries, notes


def _check(item: dict, seen: set) -> None:
    where = f"gold question {item.get('id')!r}"
    if not isinstance(item.get("id"), int) or item["id"] in seen:
        raise GoldError(f"{where}: ids must be unique integers")
    seen.add(item["id"])
    if not isinstance(item.get("question"), str) or not item["question"].strip():
        raise GoldError(f"{where}: missing question")
    if item.get("group") not in GROUPS:
        raise GoldError(f"{where}: group must be one of {GROUPS}")
    if not item.get("route") or not set(item["route"]) <= set(ROUTES):
        raise GoldError(f"{where}: route must be a list drawn from {ROUTES}")
    if item.get("expect") not in EXPECTS:
        raise GoldError(f"{where}: expect must be one of {EXPECTS}")
    if item["expect"] == "answer" and not (item.get("reference") and item.get("sources")):
        raise GoldError(f"{where}: an answer question needs a reference and sources")
    for turn in turns(item):
        for entry in turn.get("numbers") or []:
            try:
                parse_one(entry["text"])
            except (KeyError, ValueError) as e:
                raise GoldError(f"{where}: bad number {entry!r}: {e}") from e
        for entry in turn.get("mentions") or []:
            if not entry.get("any"):
                raise GoldError(f"{where}: a mention needs an `any` list of regexes")
            for pattern in entry["any"]:
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise GoldError(f"{where}: bad regex {pattern!r}: {e}") from e
