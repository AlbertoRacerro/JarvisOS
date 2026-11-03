from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

FIXTURE_PATH = Path(__file__).with_name("routing_eval_set.json")


class RoutingEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    expected_capability: str = Field(min_length=1)
    expected_context_level: str = Field(min_length=1)
    notes: str | None = None


class RoutingEvalPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    prompt: str
    expected_capability: str
    actual_capability: str
    expected_context_level: str
    actual_context_level: str
    route_class: str | None = None
    classification_source: str | None = None
    classification_confidence: float | None = None


@dataclass(frozen=True)
class LabelAgreement:
    label: str
    correct: int
    total: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class RoutingEvalAgreement:
    total: int
    capability: LabelAgreement
    context_level: LabelAgreement
    exact_match: LabelAgreement
    mismatches: list[RoutingEvalPrediction]
    confusion: dict[str, dict[str, int]]


def load_routing_eval_cases(path: Path = FIXTURE_PATH) -> list[RoutingEvalCase]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list):
        raise ValueError("routing eval set must be a JSON array")
    return [RoutingEvalCase.model_validate(item) for item in raw]


def compute_routing_agreement(predictions: list[RoutingEvalPrediction]) -> RoutingEvalAgreement:
    total = len(predictions)
    capability_correct = sum(1 for item in predictions if item.actual_capability == item.expected_capability)
    context_correct = sum(1 for item in predictions if item.actual_context_level == item.expected_context_level)
    exact_correct = sum(
        1
        for item in predictions
        if item.actual_capability == item.expected_capability
        and item.actual_context_level == item.expected_context_level
    )
    mismatches = [
        item
        for item in predictions
        if item.actual_capability != item.expected_capability
        or item.actual_context_level != item.expected_context_level
    ]
    return RoutingEvalAgreement(
        total=total,
        capability=LabelAgreement("capability", capability_correct, total),
        context_level=LabelAgreement("context_level", context_correct, total),
        exact_match=LabelAgreement("exact_match", exact_correct, total),
        mismatches=mismatches,
        confusion={
            "capability": _confusion(predictions, "expected_capability", "actual_capability"),
            "context_level": _confusion(predictions, "expected_context_level", "actual_context_level"),
        },
    )


def agreement_to_dict(agreement: RoutingEvalAgreement) -> dict[str, Any]:
    def label(item: LabelAgreement) -> dict[str, Any]:
        return {"correct": item.correct, "total": item.total, "accuracy": round(item.accuracy, 4)}

    return {
        "total": agreement.total,
        "capability": label(agreement.capability),
        "context_level": label(agreement.context_level),
        "exact_match": label(agreement.exact_match),
        "confusion": agreement.confusion,
        "mismatches": [item.model_dump() for item in agreement.mismatches],
    }


def render_routing_eval_markdown(report: dict[str, Any]) -> str:
    agreement = report["agreement"]
    lines = [
        "# Local Auto routing eval report",
        "",
        f"Generated: {report['generated_at']}",
        f"Eval cases: {agreement['total']}",
        "",
        "| Label | Correct | Total | Accuracy |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label in ("capability", "context_level", "exact_match"):
        row = agreement[label]
        lines.append(f"| {label} | {row['correct']} | {row['total']} | {row['accuracy']:.2%} |")
    lines.extend(["", "## Mismatches"])
    if not agreement["mismatches"]:
        lines.append("No mismatches.")
    else:
        for item in agreement["mismatches"]:
            lines.append(
                f"- `{item['id']}` capability {item['expected_capability']} -> {item['actual_capability']}; "
                f"context {item['expected_context_level']} -> {item['actual_context_level']}"
            )
    lines.append("")
    return "\n".join(lines)


def render_local_route_smoke_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Local route smoke report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "| Route | Model | Calls | Success | Avg wall ms | Ledger IDs |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for route in report.get("routes", []):
        calls = route.get("calls", [])
        success = [call for call in calls if call.get("status") == "success"]
        avg = sum(call.get("wall_ms", 0) for call in calls) / len(calls) if calls else 0
        ledger_ids = ", ".join(str(call.get("ledger_id")) for call in calls if call.get("ledger_id"))
        lines.append(
            f"| {route.get('route_class')} | {route.get('model_id')} | {len(calls)} | {len(success)} | {avg:.0f} | {ledger_ids} |"
        )
    lines.extend(["", "## Swap sequence", "", "| Step | Route | Model | Status | Wall ms | Ledger ID |", "| ---: | --- | --- | --- | ---: | --- |"])
    for index, call in enumerate(report.get("swap_sequence", []), start=1):
        lines.append(
            f"| {index} | {call.get('route_class')} | {call.get('model_id')} | {call.get('status')} | {call.get('wall_ms')} | {call.get('ledger_id')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _confusion(predictions: list[RoutingEvalPrediction], expected_attr: str, actual_attr: str) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {}
    for item in predictions:
        expected = str(getattr(item, expected_attr))
        actual = str(getattr(item, actual_attr))
        counts.setdefault(expected, Counter())[actual] += 1
    return {expected: dict(counter) for expected, counter in sorted(counts.items())}
