"""Laya: bounded, non-authoritative decision advice for local Jarvis callers."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.provider_registry import ProviderRegistry, load_default_provider_registry
from app.modules.local_ai.decision_contracts import (
    DecisionOutputSpec,
    DecisionRequest,
    DecisionResult,
    EnumDecisionOutput,
    ScoreDecisionOutput,
    validate_decision_result,
)
from app.modules.local_ai.decision_service import DecisionModel, LocalCandidate, RuleDecisionModel
from app.modules.local_ai.local_router import _safe_binding, local_candidates
from app.modules.local_ai.resource_arbiter import get_resource_arbiter

DecisionKind = Literal["route_class", "retry_or_stop", "escalate", "model_select"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RouteClassRequest(_Request):
    summary: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    read_tool_ids: Annotated[list[Literal["jarvis_context_preview", "jarvis_retrieval_query"]],
                             Field(max_length=2)] = Field(default_factory=list)
    required_capability_available: StrictBool


class RetryOrStopRequest(_Request):
    previous_outcome: Literal["failed", "empty", "success"]
    attempt_count: Annotated[StrictInt, Field(ge=0, le=8)]
    retryable: StrictBool


class EscalateRequest(_Request):
    summary: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    stronger_route_ids: Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=80)]],
                                  Field(max_length=8)] = Field(default_factory=list)
    local_route_available: StrictBool


class ModelSelectRequest(_Request):
    task_kind: Literal["general", "coding", "vision", "compression"]
    candidate_ids: Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=80)]],
                             Field(min_length=1, max_length=8)]
    context_tokens: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    capability: Annotated[StrictStr, Field(max_length=64)] = ""

    @field_validator("candidate_ids")
    @classmethod
    def candidate_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("candidate_ids must be unique")
        return value


RequestByKind = RouteClassRequest | RetryOrStopRequest | EscalateRequest | ModelSelectRequest
_REQUEST_MODELS: dict[str, type[_Request]] = {
    "route_class": RouteClassRequest,
    "retry_or_stop": RetryOrStopRequest,
    "escalate": EscalateRequest,
    "model_select": ModelSelectRequest,
}
_ENUMS: dict[str, tuple[str, ...]] = {
    "route_class": ("answer_directly", "use_read_tool", "needs_ungranted_capability"),
    "retry_or_stop": ("retry", "stop"),
    "escalate": ("stay_local", "ask_human", "suggest_stronger_route"),
}
_CONFIDENCE_THRESHOLD = 0.5
_WORD_RE = re.compile(r"\w+")
_REQUEST_DEADLINE = timedelta(seconds=10)


class DecisionGatewayBackend(Protocol):
    model_ref: str

    def decide(self, kind: DecisionKind, request: DecisionRequest,
               payload: RequestByKind, candidates: tuple[LocalCandidate, ...]) -> DecisionResult: ...


class RulesDecisionBackend:
    """Deterministic four-kind advice; the existing candidate ranker is unchanged."""

    model_ref = "jarvis.laya-rules.v1"

    def __init__(self, ranker: DecisionModel | None = None) -> None:
        self.ranker = ranker or RuleDecisionModel()

    def decide(self, kind: DecisionKind, request: DecisionRequest,
               payload: RequestByKind, candidates: tuple[LocalCandidate, ...]) -> DecisionResult:
        now = datetime.now(UTC)
        if kind == "model_select":
            return self.ranker.decide(request, candidates)

        if kind == "route_class":
            assert isinstance(payload, RouteClassRequest)
            recommendation = (
                "needs_ungranted_capability" if not payload.required_capability_available
                else "use_read_tool" if payload.read_tool_ids else "answer_directly"
            )
        elif kind == "retry_or_stop":
            assert isinstance(payload, RetryOrStopRequest)
            recommendation = "retry" if payload.retryable and payload.attempt_count == 0 \
                and payload.previous_outcome in {"failed", "empty"} else "stop"
        else:
            assert isinstance(payload, EscalateRequest)
            recommendation = (
                "suggest_stronger_route" if payload.stronger_route_ids
                else "stay_local" if payload.local_route_available else "ask_human"
            )
        # A route_class summary with no words carries no evidence for any route, so
        # the rule reports zero confidence and the gateway abstains.
        confidence = 0.0 if isinstance(payload, RouteClassRequest) and len(_WORD_RE.findall(payload.summary)) < 2 else 1.0
        return DecisionResult(
            decision_id=request.decision_id, outcome="decided",
            outputs=(EnumDecisionOutput(name="recommendation", value=recommendation),
                     ScoreDecisionOutput(name="confidence", value=confidence)),
            model_ref=self.model_ref, reason_code="rules_advice", decided_at=now,
        )


class UnavailableDecisionBackend:
    def __init__(self, backend_name: str) -> None:
        self.model_ref = f"jarvis.laya-unavailable.{backend_name[:48]}"

    def decide(self, _kind: DecisionKind, _request: DecisionRequest,
               _payload: RequestByKind, _candidates: tuple[LocalCandidate, ...]) -> DecisionResult:
        raise RuntimeError("configured Laya backend is unavailable")


class DecisionGateway:
    """Validate bounded requests and backend results; all failures become abstentions."""

    def __init__(self, backend: DecisionGatewayBackend | None = None, *,
                 registry: ProviderRegistry | None = None,
                 available_routes: Callable[[], set[str]] | None = None) -> None:
        self.backend = backend or RulesDecisionBackend()
        self.registry = registry
        self.available_routes = available_routes or _available_local_routes

    @classmethod
    def from_config(cls, *, backend: DecisionGatewayBackend | None = None) -> DecisionGateway:
        selected = os.getenv("JARVISOS_LAYA_BACKEND", "rules").strip().lower()
        if backend is not None:
            return cls(backend=backend)
        return cls(backend=RulesDecisionBackend() if selected == "rules" else UnavailableDecisionBackend(selected))

    def decide(self, kind: str, raw_payload: object) -> dict[str, object]:
        started = perf_counter()
        parsed: RequestByKind | None = None
        request: DecisionRequest | None = None
        digest = canonical_digest({"kind": kind, "request": raw_payload})
        try:
            if kind not in _REQUEST_MODELS or not isinstance(raw_payload, dict):
                raise ValueError("unknown decision kind or request shape")
            parsed = cast(RequestByKind, _REQUEST_MODELS[kind].model_validate(raw_payload))
            decision_kind = cast(DecisionKind, kind)
            candidates: tuple[LocalCandidate, ...] = ()
            candidate_set: tuple[str, ...] = ()
            snapshot = None
            output_specs: tuple[DecisionOutputSpec, ...]
            if kind == "model_select":
                assert isinstance(parsed, ModelSelectRequest)
                registry = self.registry or load_default_provider_registry()
                safe = {}
                for candidate in local_candidates(registry):
                    try:
                        _safe_binding(candidate, registry)
                    except ValueError:
                        continue
                    safe[candidate.candidate_id] = candidate
                available = set(self.available_routes())
                admitted = set(parsed.candidate_ids)
                candidates = tuple(safe[route] for route in sorted(admitted & available & safe.keys()))
                if not candidates:
                    raise ValueError("no currently available local candidates")
                candidate_set = tuple(candidate.candidate_id for candidate in candidates)
                snapshot = get_resource_arbiter().snapshot()
                output_specs = (DecisionOutputSpec(name="fit", kind="score"),)
            else:
                output_specs = (
                    DecisionOutputSpec(name="recommendation", kind="enum", enum_values=_ENUMS[kind]),
                    DecisionOutputSpec(name="confidence", kind="score"),
                )
            now = datetime.now(UTC)
            request = DecisionRequest(
                decision_id=str(uuid4()), decision_type=f"laya.{kind}",
                candidate_set=candidate_set, output_specs=output_specs,
                constraints=_constraints(parsed), resource_snapshot=snapshot,
                requested_at=now, deadline_at=now + _REQUEST_DEADLINE,
            )
            result = self.backend.decide(decision_kind, request, parsed, candidates)
            if request.is_expired(datetime.now(UTC)):
                return self._abstention(kind, digest, result.model_ref, started, "expired_request")
            validate_decision_result(request, result)
            confidence_name = "fit" if kind == "model_select" else "confidence"
            confidence = next((item.value for item in result.outputs if item.name == confidence_name), None)
            if (result.outcome != "decided" or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float)) or confidence < _CONFIDENCE_THRESHOLD):
                return self._abstention(kind, digest, result.model_ref, started, "low_confidence")
            recommendation = result.selected_candidate if kind == "model_select" else next(
                (item.value for item in result.outputs if item.name == "recommendation"), None
            )
            return {
                "kind": kind, "request_digest": digest, "model_ref": result.model_ref,
                "outcome": "decided", "latency_ms": max(0, round((perf_counter() - started) * 1000)),
                "recommendation": recommendation, "confidence": confidence,
            }
        except Exception:
            model_ref = getattr(self.backend, "model_ref", "unknown")
            return self._abstention(kind if kind in _REQUEST_MODELS else "unknown", digest,
                                    str(model_ref), started, "invalid_or_backend_error")

    @staticmethod
    def _abstention(kind: str, digest: str, model_ref: str, started: float, reason: str) -> dict[str, object]:
        return {"kind": kind, "request_digest": digest, "model_ref": model_ref,
                "outcome": "abstained", "latency_ms": max(0, round((perf_counter() - started) * 1000)),
                "recommendation": None, "confidence": None, "reason_code": reason}


def _constraints(payload: RequestByKind) -> dict[str, str | int | float | bool]:
    if isinstance(payload, ModelSelectRequest):
        return {"task_kind": payload.task_kind, "context_tokens": payload.context_tokens,
                "capability": payload.capability}
    return {"request_kind": type(payload).__name__}


def _available_local_routes() -> set[str]:
    """Project fresh runtime availability without starting or loading any route."""
    from app.modules.ai.thread_routes import read_conversation_options

    options = read_conversation_options(cast(Any, None))
    return {
        route.route_class for route in options.routes
        if route.execution_class == "local_compute"
        and route.availability.runtime_reachable is True
        and route.availability.model_installed is True
    }
