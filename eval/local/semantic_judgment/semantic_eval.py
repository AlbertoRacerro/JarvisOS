"""Offline, deterministic engineering-judgment cases and grader."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

CASES_PATH = Path(__file__).with_name("cases.json")
VERDICTS = ("no_issue", "invalid_assumption", "cannot_conclude", "contradiction")


def reynolds(rho: float, v: float, D: float, mu: float) -> float:
    return rho * v * D / mu


def biot(h: float, L: float, k: float) -> float:
    return h * L / k


def mass_balance_residual(inlets: list[float], outlets: list[float]) -> float:
    return abs(sum(inlets) - sum(outlets))


def energy_balance_residual(inputs: list[float], outputs: list[float]) -> float:
    return abs(sum(inputs) - sum(outputs))


def range_value(value: float) -> float:
    return value


def required_data_present(known: dict[str, float], required: list[str]) -> bool:
    return all(name in known for name in required)


CHECK_FUNCTIONS = {
    "reynolds": reynolds,
    "biot": biot,
    "mass_balance_residual": mass_balance_residual,
    "energy_balance_residual": energy_balance_residual,
    "range_value": range_value,
    "required_data_present": required_data_present,
}


def check_case(case: dict) -> bool:
    """Recompute the recorded predicate; True means its expected outcome holds."""
    check = case["check"]
    value = CHECK_FUNCTIONS[check["function"]](**check["inputs"])
    threshold = check["threshold"]
    comparison = check["comparison"]
    actual = {
        "lt": lambda: value < threshold,
        "le": lambda: value <= threshold,
        "gt": lambda: value > threshold,
        "ge": lambda: value >= threshold,
        "eq": lambda: value == threshold,
    }[comparison]()
    return actual is check["expected"]


def load_cases() -> list[dict]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if data["schema_version"] != 1:
        raise ValueError("Unsupported case schema")
    return data["cases"]


def shuffled_options(case: dict) -> list[dict]:
    options = [dict(option) for option in case["options"]]
    seed = int.from_bytes(hashlib.sha256(case["id"].encode()).digest()[:8], "big")
    random.Random(seed).shuffle(options)
    return options


def render_prompt(case: dict) -> str:
    choices = "\n".join(
        f'{option["label"]}: {option["text"]}' for option in shuffled_options(case)
    )
    return (
        f'{case["title"]}\n{case["context"]}\n\n{case["question"]}\n'
        f"Choices:\n{choices}\n\n"
        'Reply with one JSON object only: {"verdict": "...", "options": [...], '
        '"missing": [...]}. Use a verdict from no_issue, invalid_assumption, '
        'cannot_conclude, contradiction. Use only choice labels in the arrays; '
        'use [] when an array does not apply.'
    )


def export_entries(cases: list[dict]) -> list[dict]:
    return [
        {
            "name": case["id"],
            "prompt": render_prompt(case),
            "category": case["category"],
            "answer": case["answer"],
        }
        for case in cases
    ]


def first_json_object(reply_text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(reply_text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(reply_text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def grade(case: dict, reply_text: str) -> dict:
    reply = first_json_object(reply_text)
    if reply is None:
        return {
            "verdict_ok": False,
            "options_ok": False,
            "missing_ok": False,
            "score": 0,
            "parse_error": True,
        }
    answer = case["answer"]
    labels = {option["label"] for option in case["options"]}

    def label_set(value: object) -> set[str] | None:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return None
        if len(value) != len(set(value)) or not set(value) <= labels:
            return None
        return set(value)

    options = label_set(reply.get("options"))
    missing = label_set(reply.get("missing"))
    verdict_ok = reply.get("verdict") == answer["verdict"]
    options_ok = options == set(answer["options"])
    distractors = {
        option["label"] for option in case["options"] if option.get("distractor")
    }
    missing_ok = (
        missing is not None
        and set(answer["missing"]) <= missing
        and not missing & distractors
    )
    category = case["category"]
    if category in ("validity_domain", "contradiction"):
        score = int(verdict_ok and options_ok)
    elif category == "insufficient_data":
        score = int(verdict_ok and missing_ok)
    else:
        score = int(verdict_ok and options == set())
    return {
        "verdict_ok": verdict_ok,
        "options_ok": options_ok,
        "missing_ok": missing_ok,
        "score": score,
        "parse_error": False,
    }


def grade_replies(cases: list[dict], replies: dict[str, str]) -> dict:
    per_case = {case["id"]: grade(case, replies.get(case["id"], "")) for case in cases}
    per_category: dict[str, dict[str, int]] = {}
    for case in cases:
        total = per_category.setdefault(case["category"], {"score": 0, "count": 0})
        total["score"] += per_case[case["id"]]["score"]
        total["count"] += 1
    return {"cases": per_case, "categories": per_category}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--emit-prompts", type=Path, metavar="FILE")
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--grade", type=Path, metavar="REPLIES_JSON")
    args = parser.parse_args()
    cases = load_cases()
    if args.emit_prompts:
        args.emit_prompts.write_text(
            json.dumps(export_entries(cases), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    elif args.dry_run:
        for entry in export_entries(cases):
            print(f'[{entry["name"]}]\n{entry["prompt"]}\n')
    else:
        replies = json.loads(args.grade.read_text(encoding="utf-8"))
        print(json.dumps(grade_replies(cases, replies), indent=2))


if __name__ == "__main__":
    main()
