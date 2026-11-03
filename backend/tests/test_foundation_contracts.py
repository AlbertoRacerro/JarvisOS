"""145 FOUNDATION-CONTRACTS-1: frozen shared contract semantics and schema snapshot.

Regenerate the frozen schema snapshot only for an accepted K-owned contract change:
    cd backend && python -m tests.test_foundation_contracts --write-snapshot
"""

from __future__ import annotations

import inspect
import json
import sys
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.modules.ai import agent_contracts as agent
from app.modules.ai import retrieval_contracts as retrieval
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.execution import run_ai_task
from app.modules.ai.jarvis_context_models import SourceLocation, SourceRef
from app.modules.ai.routing.invariants import CRITICAL_PERMISSION_FIELDS
from app.modules.engineering import refs as eng
from app.modules.local_ai import decision_contracts as decision
from app.modules.local_ai import resource_contracts as resource
from app.modules.local_ai.classification.contracts import MODEL_NON_AUTHORITY_BOUNDARIES
from app.modules.local_ai.runtime.status import _parse_loaded_models

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "foundation_contracts_v1.schema.json"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64

FROZEN_MODELS: dict[str, type[BaseModel]] = {
    "SourceRef": SourceRef,
    "AgentSessionRef": agent.AgentSessionRef,
    "AgentControlCommand": agent.AgentControlCommand,
    "AgentEvent": agent.AgentEvent,
    "CapabilityGrantRef": agent.CapabilityGrantRef,
    "InferenceEnvelope": agent.InferenceEnvelope,
    "StructuredToolCall": agent.StructuredToolCall,
    "StructuredToolResult": agent.StructuredToolResult,
    "DecisionRequest": decision.DecisionRequest,
    "DecisionResult": decision.DecisionResult,
    "RuntimeResourceSnapshot": resource.RuntimeResourceSnapshot,
    "ResourceReservationRequest": resource.ResourceReservationRequest,
    "ResourceLease": resource.ResourceLease,
    "RetrievalHit": retrieval.RetrievalHit,
    "IndexDocument": retrieval.IndexDocument,
    "AuthoritativeResolution": retrieval.AuthoritativeResolution,
    "ContextBundle": retrieval.ContextBundle,
    "EngineeringRef": eng.EngineeringRef,
    "EngineeringProjectRef": eng.EngineeringProjectRef,
    "EngineeringRevisionRef": eng.EngineeringRevisionRef,
    "ComponentRegistryRef": eng.ComponentRegistryRef,
    "PropertyBasisRef": eng.PropertyBasisRef,
    "MaterialStateRef": eng.MaterialStateRef,
    "ProcessModelIRRef": eng.ProcessModelIRRef,
    "DynamicModelRef": eng.DynamicModelRef,
    "EnvironmentalScenarioRef": eng.EnvironmentalScenarioRef,
    "ProcessDesignEnvelopeRef": eng.ProcessDesignEnvelopeRef,
    "GeometryAssetRef": eng.GeometryAssetRef,
    "MeshArtifactRef": eng.MeshArtifactRef,
    "PhysicsCaseRef": eng.PhysicsCaseRef,
    "EvaluationRequestRef": eng.EvaluationRequestRef,
    "EvaluationResultRef": eng.EvaluationResultRef,
    "StudyRef": eng.StudyRef,
    "ValidityEnvelopeRef": eng.ValidityEnvelopeRef,
    "Quantity": eng.Quantity,
}


def current_snapshot() -> dict[str, object]:
    return {
        "versions": {
            "agent_contracts": agent.AGENT_CONTRACTS_VERSION,
            "decision_contracts": decision.DECISION_CONTRACTS_VERSION,
            "resource_contracts": resource.RESOURCE_CONTRACTS_VERSION,
            "retrieval_contracts": retrieval.RETRIEVAL_CONTRACTS_VERSION,
            "engineering_refs": eng.ENGINEERING_REFS_VERSION,
        },
        "schemas": {name: model.model_json_schema() for name, model in sorted(FROZEN_MODELS.items())},
    }


def _ref(**overrides: object) -> SourceRef:
    fields: dict[str, object] = {
        "authority_owner": "modeling",
        "object_type": "decision",
        "object_id": "dec-1",
        "workspace_id": "main",
        "revision": "7",
    }
    fields.update(overrides)
    return SourceRef.model_validate(fields)


def _session(**overrides: object) -> agent.AgentSessionRef:
    fields: dict[str, object] = {
        "jarvis_thread_id": "thread-1",
        "hermes_session_id": "hs-1",
        "profile_id": "default",
        "workspace_id": "main",
        "generation": 2,
        "upstream_revision": "v0.21.4",
    }
    fields.update(overrides)
    return agent.AgentSessionRef.model_validate(fields)


def _command(kind: str = "interrupt", **overrides: object) -> agent.AgentControlCommand:
    fields: dict[str, object] = {
        "command_id": "cmd-1",
        "kind": kind,
        "correlation_id": "corr-1",
        "jarvis_thread_id": "thread-1",
        "workspace_id": "main",
        "profile_id": "default",
        "hermes_session_id": None if kind == "start" else "hs-1",
        "expected_generation": 0 if kind == "start" else 2,
        "requested_at": NOW,
        "deadline_at": LATER,
    }
    fields.update(overrides)
    return agent.AgentControlCommand.model_validate(fields)


def _reservation(**overrides: object) -> resource.ResourceReservationRequest:
    fields: dict[str, object] = {
        "request_id": "req-1",
        "owner_kind": "ai_flow",
        "owner_id": "flow-1",
        "correlation_id": "corr-1",
        "resources": {"vram_bytes": 4_000_000_000, "model_name": "qwen3:8b", "gpu_index": 0},
        "snapshot_generation": 5,
        "requested_at": NOW,
        "deadline_at": LATER,
        "max_hold_seconds": 600,
    }
    fields.update(overrides)
    return resource.ResourceReservationRequest.model_validate(fields)


# --- serialization boundary --------------------------------------------------


def test_frozen_schema_snapshot_has_not_drifted() -> None:
    assert SNAPSHOT_PATH.exists(), "run: python -m tests.test_foundation_contracts --write-snapshot"
    frozen = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert frozen == json.loads(json.dumps(current_snapshot())), (
        "frozen 145 contract changed; a frozen-field change needs K-owned coordination and a snapshot update"
    )


@pytest.mark.parametrize("name", sorted(FROZEN_MODELS))
def test_every_frozen_contract_forbids_unknown_fields_and_is_immutable(name: str) -> None:
    model = FROZEN_MODELS[name]
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("frozen") is True


def test_versioned_contracts_reject_other_schema_versions() -> None:
    payload = _command().model_dump(mode="json")
    assert agent.AgentControlCommand.model_validate(payload) == _command()
    payload["schema_version"] = "agent_contracts.v2"
    with pytest.raises(ValidationError):
        agent.AgentControlCommand.model_validate(payload)
    lease_payload = _reservation().model_dump(mode="json") | {"schema_version": "resource_contracts.v0"}
    with pytest.raises(ValidationError):
        resource.ResourceReservationRequest.model_validate(lease_payload)


def test_unknown_fields_are_refused_at_nested_levels() -> None:
    payload = _command().model_dump(mode="json") | {"permission": "granted"}
    with pytest.raises(ValidationError):
        agent.AgentControlCommand.model_validate(payload)
    session = _session().model_dump(mode="json") | {"authority": "owner"}
    with pytest.raises(ValidationError):
        agent.AgentEvent.model_validate(
            {"event_id": "e1", "session_ref": session, "sequence": 0, "kind": "session.started", "occurred_at": NOW}
        )


def test_datetimes_must_be_aware_and_normalize_to_utc() -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        _command(requested_at=naive)
    shifted = _command(requested_at=NOW.astimezone(timezone(timedelta(hours=2))))
    assert shifted.requested_at.utcoffset() == timedelta(0)
    assert shifted == _command()
    assert shifted.model_dump_json() == _command().model_dump_json()


def test_json_round_trip_preserves_every_contract_family() -> None:
    samples: list[BaseModel] = [
        _command(),
        _command("start"),
        _reservation(),
        resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=5, now=NOW),
        eng.StudyRef(authority_owner="process_kernel", object_id="study-1", workspace_id="main", revision="3"),
        eng.Quantity(value=298.15, unit="K", basis_ref=_ref()),
        _ref(location={"start_line": 3, "end_line": 9}),
    ]
    for sample in samples:
        assert type(sample).model_validate_json(sample.model_dump_json()) == sample


@pytest.mark.parametrize(
    "field, value",
    [
        ("command_id", ""),
        ("command_id", "x" * 129),
        ("command_id", "-leading"),
        ("command_id", "has space"),
        ("command_id", "a/b"),
        ("command_id", 7),
        ("requested_at", "2026-09-23T12:00:00"),
        ("requested_at", "yesterday"),
        ("expected_generation", -1),
        ("kind", "restart"),
        ("workspace_id", ["main"]),
    ],
)
def test_hostile_control_command_fields_are_refused(field: str, value: object) -> None:
    payload = _command().model_dump(mode="json") | {field: value}
    with pytest.raises(ValidationError):
        agent.AgentControlCommand.model_validate(payload)


@pytest.mark.parametrize(
    "digest",
    ["sha256:" + "A" * 64, "sha256:" + "a" * 63, "a" * 64, "md5:" + "a" * 32, "sha256:" + "g" * 64],
)
def test_malformed_digests_are_refused_everywhere(digest: str) -> None:
    with pytest.raises(ValidationError):
        _ref(content_digest=digest)
    with pytest.raises(ValidationError):
        _envelope(context_bundle_id="bundle-1", context_bundle_digest=digest)
    with pytest.raises(ValidationError):
        retrieval.ContextBundleItem(source_ref=_ref(), content_digest=digest, token_estimate=0, expansion_level=0)


def test_collection_bounds_are_enforced_at_max_plus_one() -> None:
    refs = [_ref(object_id=f"dec-{i}") for i in range(agent.MAX_REFS + 1)]
    _envelope(provenance_refs=refs[: agent.MAX_REFS])
    with pytest.raises(ValidationError):
        _envelope(provenance_refs=refs)
    candidates = [f"c{i}" for i in range(decision.MAX_CANDIDATES + 1)]
    _decision_request(candidate_set=candidates[: decision.MAX_CANDIDATES])
    with pytest.raises(ValidationError):
        _decision_request(candidate_set=candidates)
    with pytest.raises(ValidationError):
        _decision_request(candidate_set=["a", "a"])


# --- A. session / control / events / grants ----------------------------------


def test_control_command_target_is_explicit_per_kind() -> None:
    with pytest.raises(ValidationError):
        _command("start", hermes_session_id="hs-1")
    with pytest.raises(ValidationError):
        _command("interrupt", hermes_session_id=None)
    with pytest.raises(ValidationError):
        _command("close", expected_generation=0)
    with pytest.raises(ValidationError):
        _command(deadline_at=NOW)


def test_control_command_refuses_stale_generation_and_superseded_sessions() -> None:
    current = _session(generation=2)
    agent.check_control_target(_command(), current, now=NOW)
    with pytest.raises(agent.StaleAgentGenerationError):
        agent.check_control_target(_command(expected_generation=1), current, now=NOW)
    with pytest.raises(agent.AgentControlTargetError):
        agent.check_control_target(_command(hermes_session_id="hs-old"), current, now=NOW)
    with pytest.raises(agent.AgentControlTargetError):
        agent.check_control_target(_command(jarvis_thread_id="thread-2"), current, now=NOW)
    with pytest.raises(agent.AgentControlTargetError):
        agent.check_control_target(_command(profile_id="other-profile"), current, now=NOW)
    with pytest.raises(agent.AgentControlTargetError):
        agent.check_control_target(_command(), None, now=NOW)
    with pytest.raises(agent.AgentControlTargetError):
        agent.check_control_target(_command(), current, now=LATER)


def test_start_is_a_cas_against_the_thread_mapping_generation() -> None:
    agent.check_control_target(_command("start"), None, now=NOW)
    agent.check_control_target(_command("start", expected_generation=2), _session(generation=2), now=NOW)
    with pytest.raises(agent.StaleAgentGenerationError):
        agent.check_control_target(_command("start"), _session(generation=2), now=NOW)
    with pytest.raises(agent.StaleAgentGenerationError):
        agent.check_control_target(_command("start", expected_generation=1), None, now=NOW)


def test_agent_event_payload_is_ref_plus_digest_only() -> None:
    base = {"event_id": "e1", "session_ref": _session(), "sequence": 0, "kind": "tool.call", "occurred_at": NOW}
    agent.AgentEvent.model_validate(base | {"payload_ref": _ref(), "payload_digest": DIGEST})
    with pytest.raises(ValidationError):
        agent.AgentEvent.model_validate(base | {"payload_ref": _ref()})
    with pytest.raises(ValidationError):
        agent.AgentEvent.model_validate(base | {"payload": {"text": "inline"}})
    with pytest.raises(ValidationError):
        agent.AgentEvent.model_validate(base | {"kind": "Tool Call!"})


def test_capability_grant_is_server_issued_and_expires_or_revokes() -> None:
    grant = {
        "grant_id": "g-1",
        "capability_id": "coding.read_file",
        "issuer": "jarvis_policy",
        "scope": {"workspace_id": "main", "jarvis_thread_id": "thread-1"},
        "constraints": {"max_bytes": 65536},
        "issued_at": NOW,
        "expires_at": LATER,
    }
    parsed = agent.CapabilityGrantRef.model_validate(grant)
    assert parsed.is_active(NOW) and not parsed.is_active(LATER)
    with pytest.raises(ValidationError):
        agent.CapabilityGrantRef.model_validate(grant | {"issuer": "model"})
    with pytest.raises(ValidationError):
        agent.CapabilityGrantRef.model_validate(grant | {"revoked_at": NOW})
    revoked = agent.CapabilityGrantRef.model_validate(grant | {"revoked_at": NOW, "revocation_reason": "operator_revoked"})
    assert not revoked.is_active(NOW)
    with pytest.raises(ValidationError):
        agent.CapabilityGrantRef.model_validate(grant | {"constraints": {"ratio": float("nan")}})


# --- B. governed inference / tool exchange -----------------------------------


def _envelope(**overrides: object) -> agent.InferenceEnvelope:
    fields: dict[str, object] = {
        "envelope_id": "env-1",
        "correlation_id": "corr-1",
        "task_kind": "synthesis",
        "workspace_id": "main",
        "prompt": "summarize",
        "route_class": "local:fast",
        "requested_at": NOW,
        "deadline_at": LATER,
        "agent_session": _session(),
    }
    fields.update(overrides)
    return agent.InferenceEnvelope.model_validate(fields)


def test_inference_envelope_maps_only_onto_run_ai_task() -> None:
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    kwargs = agent.run_ai_task_kwargs(
        _envelope(flow_id="flow-1"),
        context_blocks=[{"source": "s", "content": "c"}],
        structured_output_schema=schema,
    )
    parameters = inspect.signature(run_ai_task).parameters
    assert set(kwargs) <= set(parameters)
    assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in kwargs)
    assert kwargs["user_prompt"] == "summarize" and kwargs["existing_flow_id"] == "flow-1"
    assert kwargs["structured_output_schema"] == schema
    assert "model_candidate" not in kwargs, "a model candidate is advisory; bindings are resolved by run_ai_task"


def test_inference_envelope_bounds_and_pairs() -> None:
    with pytest.raises(ValidationError):
        _envelope(route_class="openai")
    with pytest.raises(ValidationError):
        _envelope(context_bundle_id="bundle-1")
    _envelope(context_bundle_id="bundle-1", context_bundle_digest=DIGEST)
    with pytest.raises(ValidationError):
        _envelope(prompt="x" * (agent.MAX_PROMPT_CHARS + 1))
    with pytest.raises(ValidationError):
        _envelope(deadline_at=NOW - timedelta(seconds=1))
    assert _envelope().is_expired(LATER) and not _envelope().is_expired(NOW)


def test_tool_call_and_result_are_typed_and_bounded() -> None:
    call = {
        "call_id": "call-1",
        "capability_id": "coding.read_file",
        "grant_id": "g-1",
        "correlation_id": "corr-1",
        "arguments": {"path": "README.md"},
        "requested_at": NOW,
        "deadline_at": LATER,
    }
    agent.StructuredToolCall.model_validate(call)
    with pytest.raises(ValidationError):
        agent.StructuredToolCall.model_validate(call | {"argument_artifact": _ref()})
    with pytest.raises(ValidationError):
        agent.StructuredToolCall.model_validate(call | {"arguments": {"blob": "x" * agent.MAX_JSON_PAYLOAD_CHARS}})
    with pytest.raises(ValidationError):
        agent.StructuredToolCall.model_validate(call | {"arguments": {"v": float("inf")}})
    result = {"call_id": "call-1", "capability_id": "coding.read_file", "completed_at": NOW}
    agent.StructuredToolResult.model_validate(result | {"status": "succeeded", "result": {"ok": True}})
    agent.StructuredToolResult.model_validate(result | {"status": "refused", "error_code": "grant_expired"})
    with pytest.raises(ValidationError):
        agent.StructuredToolResult.model_validate(result | {"status": "failed"})
    with pytest.raises(ValidationError):
        agent.StructuredToolResult.model_validate(
            result | {"status": "cancelled", "error_code": "cancelled", "result": {"partial": 1}}
        )


# --- C. typed decisions ------------------------------------------------------


def _decision_request(**overrides: object) -> decision.DecisionRequest:
    fields: dict[str, object] = {
        "decision_id": "d-1",
        "decision_type": "local_model.select",
        "candidate_set": ["qwen3:8b", "gemma3:4b"],
        "output_specs": [
            {"name": "needs_long_context", "kind": "bool"},
            {"name": "domain", "kind": "enum", "enum_values": ["coding", "process"]},
            {"name": "fit", "kind": "score"},
        ],
        "requested_at": NOW,
        "deadline_at": LATER,
    }
    fields.update(overrides)
    return decision.DecisionRequest.model_validate(fields)


def _decision_result(**overrides: object) -> decision.DecisionResult:
    fields: dict[str, object] = {
        "decision_id": "d-1",
        "outcome": "decided",
        "selected_candidate": "qwen3:8b",
        "outputs": [
            {"name": "needs_long_context", "kind": "bool", "value": False},
            {"name": "domain", "kind": "enum", "value": "coding"},
            {"name": "fit", "kind": "score", "value": 1},
        ],
        "model_ref": "fastino/gliner2.5-multi-v1@rev",
        "reason_code": "ranked",
        "decided_at": NOW,
    }
    fields.update(overrides)
    return decision.DecisionResult.model_validate(fields)


def test_decision_result_must_answer_exactly_the_bounded_request() -> None:
    request = _decision_request()
    decision.validate_decision_result(request, _decision_result())
    with pytest.raises(decision.DecisionContractError):
        decision.validate_decision_result(request, _decision_result(selected_candidate="llama3:70b"))
    with pytest.raises(decision.DecisionContractError):
        decision.validate_decision_result(request, _decision_result(outputs=[{"name": "fit", "kind": "score", "value": 0.5}]))
    wrong_enum = [
        {"name": "needs_long_context", "kind": "bool", "value": True},
        {"name": "domain", "kind": "enum", "value": "legal"},
        {"name": "fit", "kind": "score", "value": 0.5},
    ]
    with pytest.raises(decision.DecisionContractError):
        decision.validate_decision_result(request, _decision_result(outputs=wrong_enum))
    with pytest.raises(decision.DecisionContractError):
        decision.validate_decision_result(request, _decision_result(decision_id="d-2"))


def test_abstention_is_valid_without_selection_and_carries_nothing() -> None:
    abstained = _decision_result(outcome="abstained", selected_candidate=None, outputs=[], reason_code="low_confidence")
    decision.validate_decision_result(_decision_request(), abstained)
    with pytest.raises(ValidationError):
        _decision_result(outcome="abstained", outputs=[])


def test_decision_outputs_are_strictly_typed() -> None:
    with pytest.raises(ValidationError):
        _decision_result(outputs=[{"name": "fit", "kind": "score", "value": 1.5}])
    with pytest.raises(ValidationError):
        _decision_result(outputs=[{"name": "fit", "kind": "score", "value": float("nan")}])
    with pytest.raises(ValidationError):
        _decision_result(outputs=[{"name": "needs_long_context", "kind": "bool", "value": 1}])
    with pytest.raises(ValidationError):
        _decision_result(outputs=[{"name": "domain", "kind": "json", "value": {"any": "thing"}}])


@pytest.mark.parametrize(
    "name",
    sorted(set(CRITICAL_PERMISSION_FIELDS) | set(MODEL_NON_AUTHORITY_BOUNDARIES) | {"is_allowed", "tool_permission", "approved"}),
)
def test_decisions_cannot_name_permission_or_authority_outputs(name: str) -> None:
    with pytest.raises(ValidationError):
        _decision_request(output_specs=[{"name": name, "kind": "bool"}])
    with pytest.raises(ValidationError):
        _decision_result(outputs=[{"name": name, "kind": "bool", "value": True}])


# --- F. resource snapshot / reservation lease --------------------------------


def test_runtime_snapshot_types_the_ollama_runtime_status() -> None:
    loaded = _parse_loaded_models(
        {
            "models": [
                {
                    "name": "qwen3:8b",
                    "model": "qwen3:8b",
                    "size": 6_000_000_000,
                    "size_vram": 5_000_000_000,
                    "processor": "100% GPU",
                    "until": "2026-09-23T12:05:00.123456789-07:00",
                }
            ]
        }
    )
    models = [
        resource.LoadedModelState(
            name=item["name"],
            size_bytes=item["size"],
            vram_bytes=item["size_vram"],
            processor=item["processor"],
            keep_alive_until=item["until"],
        )
        for item in loaded
    ]
    snapshot = resource.RuntimeResourceSnapshot(generation=5, observed_at=NOW, loaded_models=tuple(models))
    assert snapshot.memory.total_bytes is None, "unobserved resources are unknown, not zero"
    assert snapshot.loaded_models[0].keep_alive_until == datetime(2026, 9, 23, 19, 5, 0, 123456, tzinfo=UTC)


def test_grant_revalidates_generation_and_deadline() -> None:
    lease = resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=5, now=NOW)
    assert lease.state == "active" and lease.version == 1 and lease.expires_at == NOW + timedelta(seconds=600)
    with pytest.raises(resource.ResourceLeaseError) as stale:
        resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=6, now=NOW)
    assert stale.value.code == "stale_snapshot"
    with pytest.raises(resource.ResourceLeaseError) as late:
        resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=5, now=LATER)
    assert late.value.code == "deadline_exceeded"


def test_lease_transitions_are_version_cas_and_terminal() -> None:
    lease = resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=5, now=NOW)
    released = resource.end_lease(lease, expected_version=1, reason="completed", now=NOW + timedelta(seconds=30))
    assert (released.state, released.version, released.release_reason) == ("released", 2, "completed")
    with pytest.raises(resource.ResourceLeaseError) as stale:
        resource.end_lease(lease.model_copy(update={"version": 2}), expected_version=1, reason="failed", now=NOW)
    assert stale.value.code == "stale_version"
    with pytest.raises(resource.ResourceLeaseError) as terminal:
        resource.end_lease(released, expected_version=2, reason="cancelled", now=NOW)
    assert terminal.value.code == "lease_not_active"
    with pytest.raises(resource.ResourceLeaseError) as early:
        resource.end_lease(lease, expected_version=1, reason="expired", now=NOW)
    assert early.value.code == "invalid_transition"


def test_release_after_expiry_is_recorded_as_expired() -> None:
    lease = resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=5, now=NOW)
    after = lease.expires_at + timedelta(seconds=1)
    assert lease.is_live(NOW) and not lease.is_live(after)
    ended = resource.end_lease(lease, expected_version=1, reason="completed", now=after)
    assert (ended.state, ended.release_reason) == ("expired", "expired")


def test_lease_invariants_are_validated_on_the_wire() -> None:
    lease = resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=5, now=NOW)
    payload = lease.model_dump(mode="json")
    with pytest.raises(ValidationError):
        resource.ResourceLease.model_validate(payload | {"expires_at": (NOW + timedelta(seconds=601)).isoformat()})
    with pytest.raises(ValidationError):
        resource.ResourceLease.model_validate(payload | {"state": "released"})
    with pytest.raises(ValidationError):
        resource.ResourceLease.model_validate(payload | {"release_reason": "completed"})
    with pytest.raises(ValidationError):
        resource.ResourceAmounts()


def test_competing_cas_transitions_admit_exactly_one_winner() -> None:
    """Any arbiter applying end_lease under its own atomic section yields one winner per version."""
    lock = threading.Lock()
    store = {"lease-1": resource.grant_lease(_reservation(), lease_id="lease-1", current_generation=5, now=NOW)}
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def contender(reason: str) -> None:
        barrier.wait()
        with lock:
            try:
                store["lease-1"] = resource.end_lease(store["lease-1"], expected_version=1, reason=reason, now=NOW)
                outcomes.append("won")
            except resource.ResourceLeaseError as exc:
                outcomes.append(exc.code)

    threads = [threading.Thread(target=contender, args=("completed" if i % 2 else "cancelled",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("won") == 1
    assert set(outcomes) == {"won", "stale_version"}
    assert store["lease-1"].version == 2


# --- D. retrieval ------------------------------------------------------------


def test_source_ref_requires_exact_identity_and_maps_to_the_111_exact_ref() -> None:
    with pytest.raises(ValidationError):
        _ref(revision=None)
    git_ref = SourceRef(
        authority_owner="repository",
        object_type="file",
        object_id="backend/app/main.py",
        revision="b623fa863b0e58739a4664f7aadad0202f3f8126",
        location=SourceLocation(start_line=10, end_line=20),
    )
    with pytest.raises(ValueError):
        git_ref.to_jarvis_exact_ref()
    exact = _ref(content_digest=DIGEST, location={"fragment": "rationale"}).to_jarvis_exact_ref()
    assert (exact.owner, exact.kind, exact.id, exact.revision, exact.content_digest) == (
        "modeling",
        "decision",
        "dec-1",
        "7",
        DIGEST,
    )
    with pytest.raises(ValidationError):
        SourceLocation(start_line=5, end_line=4)
    with pytest.raises(ValidationError):
        SourceLocation()


def test_retrieval_hit_is_scored_bounded_candidate() -> None:
    hit = {"source_ref": _ref(), "fused_score": 0.4, "index_revision": "idx-1"}
    retrieval.RetrievalHit.model_validate(hit | {"lexical_score": -3.2, "excerpt": "..."})
    with pytest.raises(ValidationError):
        retrieval.RetrievalHit.model_validate(hit)
    with pytest.raises(ValidationError):
        retrieval.RetrievalHit.model_validate(hit | {"vector_score": float("nan")})
    with pytest.raises(ValidationError):
        retrieval.RetrievalHit.model_validate(hit | {"graph_score": 1.0, "excerpt": "x" * (retrieval.MAX_EXCERPT_CHARS + 1)})


def test_authoritative_resolution_refuses_revision_or_digest_mismatch() -> None:
    ref = _ref(content_digest=DIGEST)
    retrieval.AuthoritativeResolution(ref=ref, state="current", current_revision="7", current_content_digest=DIGEST, content="x")
    with pytest.raises(ValidationError):
        retrieval.AuthoritativeResolution(ref=ref, state="current", current_revision="8", current_content_digest=DIGEST)
    with pytest.raises(ValidationError):
        retrieval.AuthoritativeResolution(ref=ref, state="current", current_revision="7", current_content_digest=OTHER_DIGEST)
    with pytest.raises(ValidationError):
        retrieval.AuthoritativeResolution(ref=ref, state="stale", current_revision="8", content="old")


def _bundle(items: list[retrieval.ContextBundleItem], **overrides: object) -> retrieval.ContextBundle:
    manifest = [retrieval.ContextManifestEntry(source_ref=item.source_ref, outcome="included") for item in items]
    fields: dict[str, object] = {
        "bundle_id": "bundle-1",
        "workspace_id": "main",
        "items": items,
        "evidence_manifest": manifest
        + [retrieval.ContextManifestEntry(source_ref=_ref(object_id="dec-9"), outcome="stale")],
        "token_estimate": sum(item.token_estimate for item in items),
        "token_budget": 2000,
        "expansion_level": 1,
        "bundle_digest": retrieval.context_bundle_digest(items),
        "created_at": NOW,
    }
    fields.update(overrides)
    return retrieval.ContextBundle.model_validate(fields)


def test_context_bundle_digest_pins_ordered_resolved_items() -> None:
    first = retrieval.ContextBundleItem(source_ref=_ref(), content_digest=DIGEST, token_estimate=120, expansion_level=0)
    second = retrieval.ContextBundleItem(
        source_ref=_ref(object_id="dec-2"), content_digest=OTHER_DIGEST, token_estimate=80, expansion_level=1
    )
    bundle = _bundle([first, second])
    assert bundle.bundle_digest == canonical_digest([first.model_dump(mode="json"), second.model_dump(mode="json")])
    assert retrieval.context_bundle_digest([second, first]) != bundle.bundle_digest
    with pytest.raises(ValidationError):
        _bundle([first, second], bundle_digest=retrieval.context_bundle_digest([second, first]))
    with pytest.raises(ValidationError):
        _bundle([first, second], token_estimate=10)
    with pytest.raises(ValidationError):
        _bundle([first, second], token_budget=100)
    with pytest.raises(ValidationError):
        _bundle([first, second], expansion_level=0)
    with pytest.raises(ValidationError):
        _bundle([first, second], evidence_manifest=[])
    with pytest.raises(ValidationError):
        retrieval.ContextBundleItem(
            source_ref=_ref(content_digest=DIGEST), content_digest=OTHER_DIGEST, token_estimate=1, expansion_level=0
        )
    envelope = _envelope(context_bundle_id=bundle.bundle_id, context_bundle_digest=bundle.bundle_digest)
    assert envelope.context_bundle_digest == bundle.bundle_digest


# --- E. engineering envelopes ------------------------------------------------


@pytest.mark.parametrize(
    "unit", ["K", "degC", "Pa", "m/s", "mol/L", "J/(mol*K)", "W/(m**2*K)", "1/h", "kg/m3", "m2", "gDW", "Pa*s", "1"]
)
def test_quantity_accepts_units_from_the_single_pint_owner(unit: str) -> None:
    assert eng.Quantity(value=1.0, unit=unit).unit == unit


@pytest.mark.parametrize("value, unit", [(float("nan"), "K"), (float("inf"), "K"), (True, "K"), (1.0, "furlongz"), (1.0, " K"), (1.0, "2*m"), (1.0, "m2/s")])
def test_quantity_rejects_non_finite_boolean_or_unknown_units(value: object, unit: str) -> None:
    with pytest.raises(ValidationError):
        eng.Quantity.model_validate({"value": value, "unit": unit})


def test_engineering_refs_fix_kind_and_require_the_project_workspace() -> None:
    study = eng.StudyRef(authority_owner="process_kernel", object_id="study-1", workspace_id="main", revision="3")
    assert study.object_type == "study"
    with pytest.raises(ValidationError):
        eng.StudyRef(authority_owner="process_kernel", object_type="mesh_artifact", object_id="s", workspace_id="w", revision="1")
    with pytest.raises(ValidationError):
        eng.MeshArtifactRef(authority_owner="bluecad", object_id="mesh-1", content_digest=DIGEST)
    with pytest.raises(ValidationError):
        eng.EngineeringProjectRef(authority_owner="workspaces", object_id="other", workspace_id="main", revision="1")
    with pytest.raises(ValidationError):
        eng.GeometryAssetRef(
            authority_owner="bluecad", object_id="g", workspace_id="w", revision="1", location={"fragment": "face-3"}
        )
    generic = TypeAdapter(eng.EngineeringRef).validate_python(study.model_dump())
    assert generic.object_type == "study"
    assert isinstance(study, SourceRef)


def test_validity_envelope_has_no_default_qualification_and_needs_evidence() -> None:
    base: dict[str, object] = {
        "authority_owner": "evidence",
        "object_id": "venv-1",
        "workspace_id": "main",
        "revision": "1",
        "domain": [{"variable": "T", "lower": {"value": 280, "unit": "K"}, "upper": {"value": 320, "unit": "K"}}],
        "uncertainty": [{"variable": "yield", "relative": 0.1, "confidence_level": 0.95}],
    }
    with pytest.raises(ValidationError):
        eng.ValidityEnvelopeRef.model_validate(base)
    assert eng.ValidityEnvelopeRef.model_validate(base | {"qualification_status": "candidate"})
    with pytest.raises(ValidationError):
        eng.ValidityEnvelopeRef.model_validate(base | {"qualification_status": "qualified"})
    assert eng.ValidityEnvelopeRef.model_validate(base | {"qualification_status": "benchmarked", "evidence_refs": [_ref()]})
    inverted = [{"variable": "T", "lower": {"value": 330, "unit": "K"}, "upper": {"value": 320, "unit": "K"}}]
    with pytest.raises(ValidationError):
        eng.ValidityEnvelopeRef.model_validate(base | {"qualification_status": "candidate", "domain": inverted})


if __name__ == "__main__" and "--write-snapshot" in sys.argv:
    SNAPSHOT_PATH.write_text(json.dumps(current_snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH}")
