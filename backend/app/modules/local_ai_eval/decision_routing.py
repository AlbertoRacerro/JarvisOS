"""Small typed golden set and deterministic scoring for the rule ranker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.modules.local_ai.decision_contracts import DecisionOutputSpec, DecisionRequest
from app.modules.local_ai.decision_service import LocalCandidate, RuleDecisionModel
from app.modules.local_ai.resource_contracts import LoadedModelState, RuntimeResourceSnapshot


@dataclass(frozen=True)
class DecisionCase:
    case_id: str
    candidates: tuple[LocalCandidate, ...]
    loaded_models: tuple[str, ...]
    context_tokens: int
    capability: str
    expected: str | None


@dataclass(frozen=True)
class CalibrationBucket:
    lower: float
    upper: float
    count: int
    accuracy: float | None


@dataclass(frozen=True)
class DecisionMetrics:
    count: int
    correct: int
    accuracy: float
    abstentions: int
    abstention_rate: float
    buckets: tuple[CalibrationBucket, ...]


_FAST = LocalCandidate("local:fast", "qwen3:8b", 8192, frozenset({"fast", "general"}), latency_ms=700)
_CODER = LocalCandidate("local:coder", "coder:16b", 8192, frozenset({"coding"}), latency_ms=1100)

_CASES = (
    DecisionCase("loaded_fast", (_FAST, _CODER), ("qwen3:8b", "coder:16b"), 100, "fast", "local:fast"),
    DecisionCase("coding", (_FAST, _CODER), ("qwen3:8b", "coder:16b"), 100, "coding", "local:coder"),
    DecisionCase("cold_unknown", (_FAST,), (), 100, "fast", None),
    DecisionCase("context_exceeds", (_FAST,), ("qwen3:8b",), 9000, "fast", None),
    DecisionCase("missing_capability", (_FAST,), ("qwen3:8b",), 100, "vision", None),
)


def load_decision_cases() -> tuple[DecisionCase, ...]:
    if len({case.case_id for case in _CASES}) != len(_CASES):
        raise ValueError("duplicate decision case ids")
    return _CASES


def score_rule_ranker(cases: tuple[DecisionCase, ...] | None = None) -> DecisionMetrics:
    selected = cases if cases is not None else load_decision_cases()
    correct = abstentions = 0
    bucket_counts = [0, 0, 0, 0, 0]
    bucket_correct = [0, 0, 0, 0, 0]
    ranker = RuleDecisionModel()
    for case in selected:
        now = datetime.now(UTC)
        request = DecisionRequest(
            decision_id=case.case_id, decision_type="local_model.select",
            candidate_set=tuple(candidate.candidate_id for candidate in case.candidates),
            output_specs=(DecisionOutputSpec(name="fit", kind="score"),),
            constraints={"context_tokens": case.context_tokens, "capability": case.capability},
            resource_snapshot=RuntimeResourceSnapshot(
                generation=1, observed_at=now,
                loaded_models=tuple(LoadedModelState(name=name) for name in case.loaded_models),
            ),
            requested_at=now, deadline_at=now + timedelta(seconds=30),
        )
        result = ranker.decide(request, case.candidates)
        hit = result.selected_candidate == case.expected
        correct += hit
        abstentions += result.outcome == "abstained"
        if result.outcome == "decided":
            score = result.outputs[0].value
            assert isinstance(score, float)
            index = min(4, int(score * 5))
            bucket_counts[index] += 1
            bucket_correct[index] += hit
    count = len(selected)
    return DecisionMetrics(
        count=count, correct=correct, accuracy=correct / count if count else 0,
        abstentions=abstentions, abstention_rate=abstentions / count if count else 0,
        buckets=tuple(
            CalibrationBucket(i / 5, (i + 1) / 5, bucket_counts[i],
                              bucket_correct[i] / bucket_counts[i] if bucket_counts[i] else None)
            for i in range(5)
        ),
    )
