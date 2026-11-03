"""Typed advisory local model ranking. Admission remains in the resource arbiter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.modules.local_ai.decision_contracts import (
    BoolDecisionOutput,
    DecisionRequest,
    DecisionResult,
    EnumDecisionOutput,
    ScoreDecisionOutput,
    validate_decision_result,
)
from app.modules.local_ai.resource_contracts import RuntimeResourceSnapshot


@dataclass(frozen=True)
class LocalCandidate:
    candidate_id: str
    model_name: str
    context_window_tokens: int
    capabilities: frozenset[str]
    latency_ms: int | None = None
    ram_bytes: int = 0
    vram_bytes: int = 0
    gpu_index: int | None = None


class DecisionModel(Protocol):
    def decide(self, request: DecisionRequest, candidates: tuple[LocalCandidate, ...]) -> DecisionResult: ...


def _fits(candidate: LocalCandidate, request: DecisionRequest, snapshot: RuntimeResourceSnapshot) -> bool:
    required = request.constraints.get("context_tokens", 0)
    capability = request.constraints.get("capability")
    if isinstance(required, bool) or not isinstance(required, int) or required < 0:
        return False
    if required > candidate.context_window_tokens or (capability and capability not in candidate.capabilities):
        return False
    if candidate.ram_bytes and (snapshot.memory.available_bytes is None or candidate.ram_bytes > snapshot.memory.available_bytes):
        return False
    if candidate.vram_bytes:
        gpu = next((gpu for gpu in snapshot.gpus if gpu.index == candidate.gpu_index), None)
        if gpu is None or gpu.vram_total_bytes is None or gpu.vram_used_bytes is None:
            return False
        if candidate.vram_bytes > gpu.vram_total_bytes - gpu.vram_used_bytes:
            return False
    loaded = any(model.name == candidate.model_name for model in snapshot.loaded_models)
    return loaded or bool(candidate.ram_bytes or candidate.vram_bytes)


class RuleDecisionModel:
    model_ref = "jarvis.rule-local-ranker.v1"

    def decide(self, request: DecisionRequest, candidates: tuple[LocalCandidate, ...]) -> DecisionResult:
        now = datetime.now(UTC)
        snapshot = request.resource_snapshot
        allowed = set(request.candidate_set)
        if request.is_expired(now) or snapshot is None or set(c.candidate_id for c in candidates) != allowed:
            reason = "invalid_input"
            ranked: list[tuple[float, LocalCandidate]] = []
        else:
            ranked = []
            for candidate in candidates:
                if not _fits(candidate, request, snapshot):
                    continue
                loaded = any(model.name == candidate.model_name for model in snapshot.loaded_models)
                latency_score = 0.0 if candidate.latency_ms is None else 1.0 / (1.0 + candidate.latency_ms / 1000)
                score = 0.55 + (0.25 if loaded else 0.0) + 0.15 * latency_score
                ranked.append((score, candidate))
            ranked.sort(key=lambda row: (-row[0], row[1].candidate_id))
            reason = "no_candidate_fits" if not ranked else "ranked"
        if not ranked or ranked[0][0] < 0.55:
            result = DecisionResult(
                decision_id=request.decision_id, outcome="abstained", model_ref=self.model_ref,
                reason_code=reason, decided_at=now,
            )
        else:
            score, candidate = ranked[0]
            # This ranker answers the local routing score only. Other typed tasks
            # require a configured decision model and cannot be guessed here.
            if tuple((spec.name, spec.kind) for spec in request.output_specs) not in (
                (), (("fit", "score"),)
            ):
                result = DecisionResult(
                    decision_id=request.decision_id, outcome="abstained", model_ref=self.model_ref,
                    reason_code="unsupported_outputs", decided_at=now,
                )
            else:
                outputs = (
                    (ScoreDecisionOutput(name="fit", value=score),)
                    if request.output_specs
                    else ()
                )
                result = DecisionResult(
                    decision_id=request.decision_id, outcome="decided", selected_candidate=candidate.candidate_id,
                    outputs=outputs, model_ref=self.model_ref,
                    reason_code="ranked", decided_at=now,
                )
        validate_decision_result(request, result)
        return result


class GlinerDecisionModel:
    """Optional local classifier. Caller explicitly loads a pinned local model.

    This adapter is not the default until a real corpus benchmark qualifies it.
    """

    def __init__(self, model: Any, revision: str, *, threshold: float = 0.65) -> None:
        if not revision:
            raise ValueError("an immutable model revision is required")
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        self.model = model
        self.revision = revision
        self.threshold = threshold

    @classmethod
    def from_local_path(cls, path: str, revision: str, *, threshold: float = 0.65) -> GlinerDecisionModel:
        from pathlib import Path

        if not Path(path).is_dir():
            raise ValueError("model path must be an existing local directory")
        from gliner2 import GLiNER2

        return cls(GLiNER2.from_pretrained(path), revision, threshold=threshold)

    def decide(self, request: DecisionRequest, candidates: tuple[LocalCandidate, ...]) -> DecisionResult:
        now = datetime.now(UTC)
        fit = {candidate.candidate_id: candidate for candidate in candidates if request.resource_snapshot and _fits(candidate, request, request.resource_snapshot)}
        task = request.constraints.get("task_text")
        label: str | None = None
        confidence: float | None = None
        schema: dict[str, list[str]] = {}
        if request.candidate_set:
            schema["local_model"] = list(fit)
        for spec in request.output_specs:
            if spec.kind == "bool":
                schema[spec.name] = ["true", "false"]
            elif spec.kind == "enum":
                schema[spec.name] = list(spec.enum_values)
        predictions: dict[str, object] = {}
        if (fit or schema) and isinstance(task, str) and task and not request.is_expired(now):
            raw: object = self.model.classify_text(task, schema, include_confidence=True)
            if isinstance(raw, dict):
                for name in schema:
                    choice = raw.get(name)
                    if isinstance(choice, dict):
                        predictions[name] = choice
        choice = predictions.get("local_model")
        if isinstance(choice, dict):
            candidate_label = choice.get("label")
            candidate_confidence = choice.get("confidence")
            if isinstance(candidate_label, str) and isinstance(candidate_confidence, (float, int)) and not isinstance(candidate_confidence, bool):
                label, confidence = candidate_label, float(candidate_confidence)
        selected = (
            not request.candidate_set or (label in fit and confidence is not None and self.threshold <= confidence <= 1)
        )
        outputs: list[BoolDecisionOutput | EnumDecisionOutput | ScoreDecisionOutput] = []
        for spec in request.output_specs:
            prediction = predictions.get(spec.name)
            value = prediction.get("label", prediction.get("value")) if isinstance(prediction, dict) else None
            score = prediction.get("confidence") if isinstance(prediction, dict) else None
            if spec.kind == "score":
                score = confidence if spec.name == "fit" and request.candidate_set else score
                if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 1:
                    outputs.append(ScoreDecisionOutput(name=spec.name, value=float(score)))
            elif spec.kind == "bool" and isinstance(value, str) and value.lower() in {"true", "false"}:
                outputs.append(BoolDecisionOutput(name=spec.name, value=value.lower() == "true"))
            elif spec.kind == "enum" and isinstance(value, str) and value in spec.enum_values:
                outputs.append(EnumDecisionOutput(name=spec.name, value=value))
        complete = len(outputs) == len(request.output_specs)
        selected = selected and complete and (confidence is None or self.threshold <= confidence <= 1)
        result = DecisionResult(
            decision_id=request.decision_id,
            outcome="decided" if selected else "abstained",
            selected_candidate=label if selected else None,
            outputs=tuple(outputs) if selected else (),
            model_ref=f"fastino/gliner2.5-multi-v1@{self.revision}",
            reason_code="classified" if selected else "low_confidence",
            decided_at=now,
        )
        validate_decision_result(request, result)
        return result
