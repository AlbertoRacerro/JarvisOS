from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.contracts import AIResponse
from app.modules.ai.egress_policy import (
    EXTERNAL_PROVIDER_OPERATION,
    EgressPolicyConfig,
    load_default_egress_policy,
)
from app.modules.ai.egress_service import EgressContractError, canonical_json, sha256_text

_ALLOWED_PREPACKET_RESULTS = frozenset({"deny", "pause"})
_ALLOWED_PREPACKET_REASONS = frozenset(
    {
        "adapter_unavailable",
        "canonical_context_not_authorized",
        "context_budget_exceeded",
        "context_build_error",
        "context_malformed",
        "egress_policy_error",
        "manual_context_not_authorized",
        "max_output_tokens_required",
        "prompt_classification_required",
        "prompt_sanitization_required",
        "prompt_secret_detected",
        "provider_gate_blocked",
        "unsupported_egress_operation",
    }
)
_ALLOWED_LEVELS = frozenset({"S0", "S1", "S2", "S3", "S4", "unknown"})
_LEVEL_RANK = {"S0": 0, "S1": 1, "S2": 2, "S3": 3, "S4": 4}
_ALLOWED_TERMINAL_JOB_STATUSES = frozenset(
    {
        "config_error",
        "provider_error",
        "route_unavailable",
        "success",
        "validation_error",
    }
)
_HEX_DIGEST_LENGTH = 64
_CANONICAL_DIGEST_PREFIX = "sha256:"
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "body",
        "content",
        "derivative_content",
        "packet_json",
        "prompt",
        "raw_payload",
        "secret",
        "text",
    }
)


class EgressSpineStateError(RuntimeError):
    """A shared-spine persistence transition failed closed."""


@dataclass(frozen=True)
class PrepacketEgressDecision:
    decision_id: str
    result: str
    reason_code: str
    safe_input_digest: str
    route_class: str
    provider_id: str
    model_id: str
    fallback_index: int
    prompt_level: str
    context_level: str
    final_level: str
    source_count: int
    included_count: int
    withheld_count: int
    policy_version: str
    trigger_version: str
    config_digest: str


@dataclass(frozen=True)
class QueuedAIJob:
    ai_job_id: str
    task_kind: str
    requested_route_class: str | None
    selected_route_class: str
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class FinalizedAIJob:
    ai_job_id: str
    status: str
    output_digest: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_estimate: float | None
    usage_source: str | None
    error_type: str | None


def record_prepacket_egress_decision(
    *,
    result: str,
    reason_code: str,
    route_class: str,
    provider_id: str,
    model_id: str,
    fallback_index: int,
    prompt_digest: str,
    context_digest: str | None,
    prompt_level: str,
    context_level: str,
    final_level: str,
    source_count: int = 0,
    included_count: int = 0,
    withheld_count: int = 0,
    workspace_id: str | None = None,
    operation: str = EXTERNAL_PROVIDER_OPERATION,
    policy: EgressPolicyConfig | None = None,
    now: datetime | None = None,
) -> PrepacketEgressDecision:
    """Persist one body-free deny/pause decision before packet eligibility.

    Only safe digests, levels, counts, and concrete binding metadata are accepted. The
    API intentionally has no prompt, context, packet, or arbitrary metadata parameter.
    """

    policy = policy or load_default_egress_policy()
    if result not in _ALLOWED_PREPACKET_RESULTS:
        raise EgressContractError("pre-packet result must be deny or pause")
    if reason_code not in _ALLOWED_PREPACKET_REASONS:
        raise EgressContractError("unsupported pre-packet reason code")
    if operation != EXTERNAL_PROVIDER_OPERATION or operation not in policy.supported_operations:
        raise EgressContractError("unsupported pre-packet egress operation")
    route_class = _required_text(route_class, "route_class")
    provider_id = _required_text(provider_id, "provider_id")
    model_id = _required_text(model_id, "model_id")
    fallback_index = _non_negative_integer(fallback_index, "fallback_index")
    prompt_digest = _bare_digest(prompt_digest, "prompt_digest")
    if context_digest is not None:
        context_digest = _canonical_digest_text(context_digest, "context_digest")
    prompt_level = _level(prompt_level, "prompt_level")
    context_level = _level(context_level, "context_level")
    final_level = _level(final_level, "final_level")
    expected_final = _prepacket_final_level(prompt_level, context_level)
    if final_level != expected_final:
        raise EgressContractError("final_level must equal the maximum pre-packet level")
    source_count = _non_negative_integer(source_count, "source_count")
    included_count = _non_negative_integer(included_count, "included_count")
    withheld_count = _non_negative_integer(withheld_count, "withheld_count")
    if included_count > source_count:
        raise EgressContractError("included_count cannot exceed source_count")
    if workspace_id is not None:
        workspace_id = _required_text(workspace_id, "workspace_id")

    safe_input = {
        "context_digest": context_digest,
        "context_level": context_level,
        "fallback_index": fallback_index,
        "final_level": final_level,
        "included_count": included_count,
        "model_id": model_id,
        "operation": operation,
        "prompt_digest": prompt_digest,
        "prompt_level": prompt_level,
        "provider_id": provider_id,
        "reason_code": reason_code,
        "result": result,
        "route_class": route_class,
        "source_count": source_count,
        "withheld_count": withheld_count,
        "workspace_id": workspace_id,
    }
    safe_input_digest = sha256_text(canonical_json(safe_input))
    decision_id = str(uuid4())
    created_at = _normalized_now(now).isoformat()

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if workspace_id is not None:
                row = connection.execute(
                    "SELECT 1 FROM workspaces WHERE id = ?",
                    (workspace_id,),
                ).fetchone()
                if row is None:
                    raise EgressSpineStateError("pre-packet workspace was not found")
            connection.execute(
                """
                INSERT INTO egress_decisions (
                    id, workspace_id, created_at, result, reason_code, operation,
                    route_class, provider_id, model_id, fallback_index, packet_id,
                    packet_digest, safe_input_digest, prompt_level, context_level,
                    final_level, source_count, included_count, withheld_count,
                    trigger_ids_json, confirmation_required, projected_input_tokens,
                    projected_output_tokens, projected_cost_upper_usd, pricing_version,
                    pricing_effective_at, reservation_id, ticket_id, policy_version,
                    trigger_version, config_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?,
                          '[]', 0, 0, 0, 0, NULL, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    decision_id,
                    workspace_id,
                    created_at,
                    result,
                    reason_code,
                    operation,
                    route_class,
                    provider_id,
                    model_id,
                    fallback_index,
                    safe_input_digest,
                    prompt_level,
                    context_level,
                    final_level,
                    source_count,
                    included_count,
                    withheld_count,
                    policy.policy_version,
                    policy.trigger_version,
                    policy.config_digest,
                ),
            )
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    return PrepacketEgressDecision(
        decision_id=decision_id,
        result=result,
        reason_code=reason_code,
        safe_input_digest=safe_input_digest,
        route_class=route_class,
        provider_id=provider_id,
        model_id=model_id,
        fallback_index=fallback_index,
        prompt_level=prompt_level,
        context_level=context_level,
        final_level=final_level,
        source_count=source_count,
        included_count=included_count,
        withheld_count=withheld_count,
        policy_version=policy.policy_version,
        trigger_version=policy.trigger_version,
        config_digest=policy.config_digest,
    )


def create_queued_ai_job(
    *,
    task_kind: str,
    requested_route_class: str | None,
    selected_route_class: str,
    provider_id: str,
    model_id: str,
    decision_reason: str,
    blocked_reason: str | None = None,
    prompt_digest: str | None = None,
    context_digest: str | None = None,
    context_sources: list[dict[str, object]] | None = None,
    route_metadata: dict[str, object] | None = None,
    now: datetime | None = None,
) -> QueuedAIJob:
    """Create the one authoritative ai_jobs row before a provider attempt starts."""

    task_kind = _required_text(task_kind, "task_kind")
    if requested_route_class is not None:
        requested_route_class = _required_text(
            requested_route_class,
            "requested_route_class",
        )
    selected_route_class = _required_text(selected_route_class, "selected_route_class")
    provider_id = _required_text(provider_id, "provider_id")
    model_id = _required_text(model_id, "model_id")
    decision_reason = _required_text(decision_reason, "decision_reason")
    if blocked_reason is not None:
        blocked_reason = _required_text(blocked_reason, "blocked_reason")
    if prompt_digest is not None:
        prompt_digest = _canonical_digest_text(prompt_digest, "prompt_digest")
    if context_digest is not None:
        context_digest = _canonical_digest_text(context_digest, "context_digest")
    context_sources_json = _safe_context_sources_json(context_sources)
    route_reason = {
        "blocked_reason": blocked_reason,
        "decision_reason": decision_reason,
    }
    if route_metadata:
        _validate_safe_metadata(route_metadata, path="route_metadata")
        route_reason.update(route_metadata)
    route_reason_json = canonical_json(route_reason)
    ai_job_id = str(uuid4())

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, route_reason_json,
                prompt_digest, context_digest, context_sources_json
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ai_job_id,
                _normalized_now(now).isoformat(),
                task_kind,
                requested_route_class,
                selected_route_class,
                provider_id,
                model_id,
                route_reason_json,
                prompt_digest,
                context_digest,
                context_sources_json,
            ),
        )
        connection.commit()
    return QueuedAIJob(
        ai_job_id=ai_job_id,
        task_kind=task_kind,
        requested_route_class=requested_route_class,
        selected_route_class=selected_route_class,
        provider_id=provider_id,
        model_id=model_id,
    )


def finalize_queued_ai_job(
    ai_job_id: str,
    *,
    status: str,
    response: AIResponse | None,
    latency_ms: int,
    error_type: str | None = None,
) -> FinalizedAIJob:
    """CAS-finalize one queued ai_jobs row without creating a second attempt row."""

    ai_job_id = _required_text(ai_job_id, "ai_job_id")
    if status not in _ALLOWED_TERMINAL_JOB_STATUSES:
        raise EgressContractError("unsupported terminal ai_job status")
    latency_ms = _non_negative_integer(latency_ms, "latency_ms")
    if error_type is not None:
        error_type = _required_text(error_type, "error_type")
    if status == "success" and (response is None or response.text is None):
        raise EgressContractError("successful ai_job requires a text response")

    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT status, provider_id, model_id
            FROM ai_jobs WHERE id = ?
            """,
            (ai_job_id,),
        ).fetchone()
        if row is None or row["status"] != "queued":
            raise EgressSpineStateError("ai_job is not queued or was already finalized")
        expected_binding = (row["provider_id"], row["model_id"])
        if response is not None and (
            response.provider_id,
            response.model_id,
        ) != expected_binding:
            raise EgressSpineStateError(
                "ai_job response binding does not match the queued attempt"
            )
        if response is not None and (
            response.usage.provider_id,
            response.usage.model_id,
        ) != expected_binding:
            raise EgressSpineStateError(
                "ai_job usage binding does not match the queued attempt"
            )

        if response is not None:
            output_digest = (
                canonical_digest({"text": response.text})
                if response.text is not None
                else None
            )
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost_estimate = response.usage.provider_cost_estimate
            usage_source = response.usage.usage_source.value
        else:
            output_digest = None
            input_tokens = None
            output_tokens = None
            cost_estimate = None
            usage_source = None

        updated = connection.execute(
            """
            UPDATE ai_jobs
            SET status = ?, output_digest = ?, input_tokens = ?, output_tokens = ?,
                cost_estimate = ?, usage_source = ?, latency_ms = ?, error_type = ?
            WHERE id = ? AND status = 'queued'
            """,
            (
                status,
                output_digest,
                input_tokens,
                output_tokens,
                cost_estimate,
                usage_source,
                latency_ms,
                error_type,
                ai_job_id,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise EgressSpineStateError("ai_job finalization CAS conflict")
        connection.commit()
    return FinalizedAIJob(
        ai_job_id=ai_job_id,
        status=status,
        output_digest=output_digest,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
        usage_source=usage_source,
        error_type=error_type,
    )


def _safe_context_sources_json(
    values: list[dict[str, object]] | None,
) -> str | None:
    if not values:
        return None
    if not isinstance(values, list):
        raise EgressContractError("context_sources must be a list")
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise EgressContractError(f"context_sources[{index}] must be an object")
        _validate_safe_metadata(value, path=f"context_sources[{index}]")
    return canonical_json(values)


def _validate_safe_metadata(value: object, *, path: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise EgressContractError(f"{path} contains a non-text key")
            if key.casefold() in _FORBIDDEN_METADATA_KEYS:
                raise EgressContractError(f"{path} contains forbidden body field {key}")
            _validate_safe_metadata(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_safe_metadata(child, path=f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise EgressContractError(f"{path} contains unsupported metadata")


def _prepacket_final_level(prompt_level: str, context_level: str) -> str:
    if "S4" in {prompt_level, context_level}:
        return "S4"
    if "unknown" in {prompt_level, context_level}:
        return "unknown"
    return max((prompt_level, context_level), key=_LEVEL_RANK.__getitem__)


def _level(value: str, field_name: str) -> str:
    value = _required_text(value, field_name)
    if value not in _ALLOWED_LEVELS:
        raise EgressContractError(f"{field_name} is unsupported")
    return value


def _bare_digest(value: str, field_name: str) -> str:
    value = _required_text(value, field_name)
    if len(value) != _HEX_DIGEST_LENGTH or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise EgressContractError(f"{field_name} must be 64 lowercase hex characters")
    return value


def _canonical_digest_text(value: str, field_name: str) -> str:
    value = _required_text(value, field_name)
    if not value.startswith(_CANONICAL_DIGEST_PREFIX):
        raise EgressContractError(f"{field_name} must use sha256:<64 lowercase hex>")
    _bare_digest(value[len(_CANONICAL_DIGEST_PREFIX) :], field_name)
    return value


def _non_negative_integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EgressContractError(f"{field_name} must be a non-negative integer")
    return value


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EgressContractError(f"{field_name} must be non-empty text")
    return value.strip()


def _normalized_now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise EgressContractError("now must include timezone information")
    return result.astimezone(UTC)
