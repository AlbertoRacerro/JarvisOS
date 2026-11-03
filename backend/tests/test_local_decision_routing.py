from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.modules.ai.contracts import RoutingDecision
from app.modules.ai.execution import AiTaskOutcome
from app.modules.ai.provider_registry import load_default_provider_registry
from app.modules.local_ai import local_router
from app.modules.local_ai.decision_contracts import (
    DecisionContractError,
    DecisionOutputSpec,
    DecisionRequest,
    DecisionResult,
)
from app.modules.local_ai.decision_service import GlinerDecisionModel, LocalCandidate, RuleDecisionModel
from app.modules.local_ai.local_router import local_candidates, run_local_selected_task
from app.modules.local_ai.resource_arbiter import InProcessResourceArbiter, ResourceCapacityError
from app.modules.local_ai.resource_contracts import (
    CpuState,
    GpuState,
    LoadedModelState,
    MemoryState,
    ResourceAmounts,
    ResourceLeaseError,
    ResourceReservationRequest,
    RuntimeResourceSnapshot,
)
from app.modules.local_ai.runtime import resource_observation
from app.modules.local_ai.runtime.resource_observation import parse_linux_meminfo, parse_nvidia_smi
from app.modules.local_ai_eval.decision_routing import score_rule_ranker

NOW = datetime.now(UTC)


def observed(*, cores: int | None = 1, loaded: tuple[str, ...] = ()) -> RuntimeResourceSnapshot:
    return RuntimeResourceSnapshot(
        generation=0,
        observed_at=datetime.now(UTC),
        cpu=CpuState(logical_cores=cores, utilization=0.0 if cores is not None else None),
        memory=MemoryState(available_bytes=1024),
        loaded_models=tuple(LoadedModelState(name=name) for name in loaded),
    )


def reservation(generation: int, *, model: str = "qwen3:8b") -> ResourceReservationRequest:
    now = datetime.now(UTC)
    return ResourceReservationRequest(
        request_id="r-1",
        owner_kind="inference_envelope",
        owner_id="env-1",
        correlation_id="d-1",
        resources=ResourceAmounts(cpu_cores=1, model_name=model),
        snapshot_generation=generation,
        requested_at=now,
        deadline_at=now + timedelta(seconds=30),
        max_hold_seconds=20,
    )


def decision_request(snapshot: RuntimeResourceSnapshot, ids: tuple[str, ...]) -> DecisionRequest:
    now = datetime.now(UTC)
    return DecisionRequest(
        decision_id="d-1",
        decision_type="local_model.select",
        candidate_set=ids,
        output_specs=(DecisionOutputSpec(name="fit", kind="score"),),
        constraints={"context_tokens": 100},
        resource_snapshot=snapshot,
        requested_at=now,
        deadline_at=now + timedelta(seconds=30),
    )


def test_unknown_capacity_is_not_zero_or_assumed_available() -> None:
    arbiter = InProcessResourceArbiter(lambda: observed(cores=None, loaded=("qwen3:8b",)))
    snapshot = arbiter.snapshot()
    assert snapshot.cpu.logical_cores is None
    with pytest.raises(ResourceCapacityError):
        arbiter.reserve(reservation(snapshot.generation))
    no_util = observed(loaded=("qwen3:8b",)).model_copy(update={"cpu": CpuState(logical_cores=1)})
    arbiter = InProcessResourceArbiter(lambda: no_util)
    with pytest.raises(ResourceCapacityError):
        arbiter.reserve(reservation(arbiter.snapshot().generation))


def test_stale_generation_and_competing_reservations() -> None:
    arbiter = InProcessResourceArbiter(lambda: observed(loaded=("qwen3:8b",)))
    stale = arbiter.snapshot().generation
    lease = arbiter.reserve(reservation(stale))
    with pytest.raises(ResourceLeaseError, match="generation") as exc:
        arbiter.reserve(reservation(stale))
    assert exc.value.code == "stale_snapshot"
    arbiter.end(lease.lease_id, expected_version=1, reason="completed")

    generation = arbiter.snapshot().generation
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(arbiter.reserve, reservation(generation)) for _ in range(8)]
    results = [future.exception() if future.exception() else future.result() for future in futures]
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert all(isinstance(result, ResourceLeaseError) for result in results if isinstance(result, Exception))


def test_observation_change_and_gpu_parser() -> None:
    current = [observed()]
    arbiter = InProcessResourceArbiter(lambda: current[0])
    first = arbiter.snapshot()
    assert arbiter.snapshot().generation == first.generation
    current[0] = observed(cores=2)
    assert arbiter.snapshot().generation == first.generation + 1
    with pytest.raises(ResourceLeaseError) as stale:
        arbiter.reserve(reservation(first.generation))
    assert stale.value.code == "stale_snapshot"
    parsed = parse_nvidia_smi("0, RTX 5070, 12288, 1024\ninvalid\n1, Bad, 10, 20")
    assert len(parsed) == 1 and parsed[0].vram_total_bytes == 12288 * 1024**2


def test_capacity_and_keep_alive_churn_does_not_stale_identity() -> None:
    calls = 0

    def observe() -> RuntimeResourceSnapshot:
        nonlocal calls
        calls += 1
        return observed(loaded=("qwen3:8b",)).model_copy(
            update={
                "memory": MemoryState(total_bytes=2048, available_bytes=1024 - calls),
                "gpus": (GpuState(index=0, name="RTX", vram_total_bytes=4096, vram_used_bytes=calls),),
                "loaded_models": (LoadedModelState(name="qwen3:8b", keep_alive_until=NOW + timedelta(seconds=calls)),),
            }
        )

    arbiter = InProcessResourceArbiter(observe)
    first = arbiter.snapshot()
    lease = arbiter.reserve(reservation(first.generation))
    assert lease.granted_generation == first.generation
    arbiter.end(lease.lease_id, expected_version=1, reason="completed")

    low_capacity = [observed(loaded=("qwen3:8b",))]
    capacity_arbiter = InProcessResourceArbiter(lambda: low_capacity[0])
    generation = capacity_arbiter.snapshot().generation
    low_capacity[0] = low_capacity[0].model_copy(update={"memory": MemoryState(available_bytes=0)})
    request = reservation(generation).model_copy(
        update={"resources": ResourceAmounts(model_name="qwen3:8b", ram_bytes=1)}
    )
    with pytest.raises(ResourceCapacityError):
        capacity_arbiter.reserve(request)
    assert capacity_arbiter.snapshot().generation == generation

    loaded = ["qwen3:8b"]
    changed = InProcessResourceArbiter(lambda: observed(loaded=tuple(loaded)))
    generation = changed.snapshot().generation
    loaded.append("other:1b")
    with pytest.raises(ResourceLeaseError) as stale:
        changed.reserve(reservation(generation))
    assert stale.value.code == "stale_snapshot"


@pytest.mark.parametrize(
    ("fixture", "total", "available"),
    [
        ("MemTotal: 16384 kB\nMemAvailable: 8192 kB\n", 16384 * 1024, 8192 * 1024),
        ("MemTotal: bad kB\nMemAvailable: 5 kB\n", None, 5 * 1024),
        ("MemTotal: 4 kB\nMemAvailable: 5 kB\n", 4 * 1024, None),
        ("MemTotal: 4 MB\n", None, None),
    ],
)
def test_linux_meminfo_fixtures(fixture: str, total: int | None, available: int | None) -> None:
    memory = parse_linux_meminfo(fixture)
    assert (memory.total_bytes, memory.available_bytes) == (total, available)


def test_cpu_and_memory_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resource_observation.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        resource_observation, "query_memory", lambda: parse_linux_meminfo("MemTotal: 16384 kB\nMemAvailable: 8192 kB\n")
    )
    monkeypatch.setattr(resource_observation, "query_gpus", lambda: ())
    monkeypatch.setattr(resource_observation, "query_cpu_utilization", lambda: 0.25)
    monkeypatch.setattr(resource_observation, "get_local_ai_runtime_status", lambda: {})
    snapshot = resource_observation.observe_resources()
    assert snapshot.cpu.logical_cores == 8
    assert snapshot.cpu.utilization == 0.25
    assert snapshot.memory.available_bytes == 8192 * 1024


def test_cpu_utilization_uses_observed_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter(((100, 60), (200, 120)))
    monkeypatch.setattr(resource_observation.sys, "platform", "linux")
    monkeypatch.setattr(resource_observation, "_linux_cpu_ticks", lambda: next(readings))
    monkeypatch.setattr(resource_observation.time, "sleep", lambda _seconds: None)
    assert resource_observation.query_cpu_utilization() == 0.4


def test_windows_memory_api_and_unavailable_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    class Kernel32:
        def GlobalMemoryStatusEx(self, pointer: object) -> int:
            status = pointer._obj  # type: ignore[attr-defined]
            status.ullTotalPhys = 4096
            status.ullAvailPhys = 2048
            return 1

    monkeypatch.setattr(resource_observation.ctypes, "WinDLL", lambda *args, **kwargs: Kernel32(), raising=False)
    monkeypatch.setattr(resource_observation.sys, "platform", "win32")
    assert resource_observation.query_memory() == MemoryState(total_bytes=4096, available_bytes=2048)
    monkeypatch.setattr(resource_observation.sys, "platform", "unsupported")
    assert resource_observation.query_memory() == MemoryState()


def test_auto_and_network_bindings_never_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = load_default_provider_registry()
    candidate = LocalCandidate("local:fast", "qwen3:8b", 8192, frozenset({"fast"}))
    arbiter = InProcessResourceArbiter(lambda: observed(loaded=("qwen3:8b",)))

    def forbidden(**kwargs: object) -> AiTaskOutcome:
        pytest.fail("unsafe binding reached run_ai_task")

    monkeypatch.setattr(local_router, "run_ai_task", forbidden)
    binding = registry.bindings[candidate.candidate_id]
    for unsafe in (
        replace(binding, route_class="auto"),
        replace(binding, requires_network=True),
        replace(binding, execution_class="external_provider"),
    ):
        altered = replace(registry, bindings={**registry.bindings, candidate.candidate_id: unsafe})
        with pytest.raises(ValueError, match="local-only"):
            run_local_selected_task(
        installed_sizes={},
                user_prompt="x",
                task_kind="general",
                owner_id="env-1",
                arbiter=arbiter,
                registry=altered,
                candidates=(candidate,),
            )


def test_rule_ranking_abstention_and_result_gate() -> None:
    candidate = LocalCandidate("local:fast", "qwen3:8b", 8192, frozenset({"fast"}))
    request = decision_request(observed(loaded=("qwen3:8b",)), (candidate.candidate_id,))
    result = RuleDecisionModel().decide(request, (candidate,))
    assert result.selected_candidate == "local:fast"
    no_fit = decision_request(observed(), (candidate.candidate_id,))
    assert RuleDecisionModel().decide(no_fit, (candidate,)).reason_code == "no_candidate_fits"
    with pytest.raises(ValidationError):
        DecisionResult(
            decision_id="d-1",
            outcome="decided",
            selected_candidate="local:fast",
            model_ref="x",
            reason_code="x",
            decided_at=NOW,
            outputs=({"name": "fit", "kind": "score", "value": 1.1},),
        )  # type: ignore[arg-type]

    class InvalidModel:
        def decide(self, request: DecisionRequest, candidates: tuple[LocalCandidate, ...]) -> DecisionResult:
            return DecisionResult(
                decision_id=request.decision_id,
                outcome="decided",
                selected_candidate="external:cheap",
                outputs=(),
                model_ref="bad",
                reason_code="ranked",
                decided_at=NOW,
            )

    arbiter = InProcessResourceArbiter(lambda: observed(loaded=("qwen3:8b",)))
    with pytest.raises(DecisionContractError):
        run_local_selected_task(
        installed_sizes={},
            user_prompt="x",
            task_kind="general",
            owner_id="env-1",
            arbiter=arbiter,
            candidates=(candidate,),
            decision_model=InvalidModel(),
        )
    admitted = run_local_selected_task(
        installed_sizes={},
        user_prompt="x",
        task_kind="general",
        owner_id="env-1",
        arbiter=arbiter,
        candidates=(candidate,),
        context_tokens=8192,
        decision_model=InvalidModel(),
    )
    assert admitted.status == "abstained" and admitted.attempts[0].decision.reason_code == "no_candidate_fits"


def test_optional_classifier_output_stays_inside_typed_candidate_gate() -> None:
    class LocalClassifier:
        def __init__(self, label: str, confidence: float) -> None:
            self.label, self.confidence = label, confidence

        def classify_text(
            self, task: str, schema: dict[str, list[str]], *, include_confidence: bool
        ) -> dict[str, object]:
            assert include_confidence and task == "code"
            assert schema == {"local_model": ["local:fast"]}
            return {"local_model": {"label": self.label, "confidence": self.confidence}}

    candidate = LocalCandidate("local:fast", "qwen3:8b", 8192, frozenset({"fast"}))
    request = decision_request(observed(loaded=("qwen3:8b",)), (candidate.candidate_id,))
    request = request.model_copy(update={"constraints": {**request.constraints, "task_text": "code"}})
    assert (
        GlinerDecisionModel(LocalClassifier("local:fast", 0.9), "abc").decide(request, (candidate,)).selected_candidate
        == "local:fast"
    )
    assert (
        GlinerDecisionModel(LocalClassifier("external:cheap", 0.99), "abc").decide(request, (candidate,)).outcome
        == "abstained"
    )
    assert (
        GlinerDecisionModel(LocalClassifier("local:fast", 0.4), "abc").decide(request, (candidate,)).outcome
        == "abstained"
    )
    candidate_only = request.model_copy(update={"output_specs": ()})
    result = GlinerDecisionModel(LocalClassifier("local:fast", 0.9), "abc").decide(candidate_only, (candidate,))
    assert result.outcome == "decided" and result.selected_candidate == "local:fast" and result.outputs == ()
    class TypedClassifier:
        def classify_text(self, task: str, schema: dict[str, list[str]], *, include_confidence: bool) -> dict[str, object]:
            assert schema == {"answer": ["true", "false"], "category": ["bug", "question"]}
            return {"answer": {"label": "true"}, "category": {"label": "bug"}}

    from app.modules.local_ai.decision_contracts import BoolDecisionOutput, EnumDecisionOutput

    typed = request.model_copy(
        update={
            "candidate_set": (),
            "output_specs": (
                DecisionOutputSpec(name="answer", kind="bool"),
                DecisionOutputSpec(name="category", kind="enum", enum_values=("bug", "question")),
            ),
        }
    )
    result = GlinerDecisionModel(TypedClassifier(), "abc").decide(typed, ())
    assert result.outcome == "decided"
    assert result.outputs == (
        BoolDecisionOutput(name="answer", value=True),
        EnumDecisionOutput(name="category", value="bug"),
    )


def test_typed_decision_corpus_scorer() -> None:
    metrics = score_rule_ranker()
    assert (metrics.count, metrics.correct, metrics.abstentions) == (5, 5, 3)
    assert sum(bucket.count for bucket in metrics.buckets) == 2


def test_fallback_after_provider_failure_and_local_only_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = load_default_provider_registry()
    aliases = [candidate for candidate in local_candidates(registry) if candidate.model_name == "gemma4:12b-it-qat"]
    assert {candidate.candidate_id for candidate in aliases} == {"local:general", "local:gemma"}
    assert all("general" not in candidate.capabilities for candidate in aliases if candidate.candidate_id == "local:gemma")
    candidates = (
        LocalCandidate("local:fast", "qwen3:8b", 8192, frozenset({"fast"})),
        LocalCandidate("local:general", "gemma4:12b-it-qat", 8192, frozenset({"general"})),
    )
    arbiter = InProcessResourceArbiter(lambda: observed(loaded=("qwen3:8b", "gemma4:12b-it-qat")))
    seen: list[str] = []

    def execute(**kwargs: object) -> AiTaskOutcome:
        route = kwargs["route_class"]
        assert isinstance(route, str)
        bindings = kwargs["bindings"]
        assert isinstance(bindings, dict)
        binding = bindings[route]
        assert binding.execution_class == "local_compute" and not binding.requires_network
        seen.append(route)
        return AiTaskOutcome(
            "provider_error" if len(seen) == 1 else "success", f"job-{len(seen)}", route, RoutingDecision()
        )

    monkeypatch.setattr(local_router, "run_ai_task", execute)
    outcome = run_local_selected_task(
        installed_sizes={},
        user_prompt="x",
        task_kind="general",
        owner_id="env-1",
        arbiter=arbiter,
        registry=registry,
        candidates=candidates,
    )
    assert outcome.status == "success" and seen == ["local:fast", "local:general"]
    assert len(outcome.attempts) == 2 and all(attempt.lease_id and attempt.ledger_id for attempt in outcome.attempts)
    assert arbiter.snapshot().active_lease_ids == ()
    external = LocalCandidate("external:cheap", "deepseek-v4-pro", 8192, frozenset())
    with pytest.raises(ValueError, match="local-only"):
        run_local_selected_task(
        installed_sizes={},
            user_prompt="x", task_kind="general", owner_id="env-1", arbiter=arbiter, candidates=(external,)
        )


def test_route_alias_keeps_requested_capability_in_execution_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = load_default_provider_registry()
    arbiter = InProcessResourceArbiter(lambda: observed(loaded=("gemma4:12b-it-qat",)))
    seen: list[str] = []

    def execute(**kwargs: object) -> AiTaskOutcome:
        route = kwargs["route_class"]
        assert isinstance(route, str)
        seen.append(route)
        return AiTaskOutcome("success", "job-gemma", route, RoutingDecision())

    monkeypatch.setattr(local_router, "run_ai_task", execute)
    outcome = run_local_selected_task(
        user_prompt="hello", task_kind="general", owner_id="env-gemma", arbiter=arbiter,
        registry=registry, candidates=local_candidates(registry), capability="gemma",
    )
    assert outcome.status == "success" and seen == ["local:gemma"]


def test_cold_models_claim_their_installed_size_instead_of_being_refused() -> None:
    from app.modules.local_ai.local_router import with_cold_load_footprint
    from app.modules.local_ai.resource_contracts import GpuState, LoadedModelState, RuntimeResourceSnapshot
    from app.modules.local_ai.runtime.status import _parse_installed_model_sizes

    sizes = _parse_installed_model_sizes(
        {"models": [{"name": "qwen3:8b", "size": 5_200_000_000}, {"name": "bad", "size": True}, "junk"]}
    )
    assert sizes == {"qwen3:8b": 5_200_000_000}
    cold = LocalCandidate("local:fast", "qwen3:8b", 8192, frozenset({"fast"}))
    now = datetime.now(UTC)
    gpu = RuntimeResourceSnapshot(
        generation=1, observed_at=now,
        gpus=(GpuState(index=0, vram_total_bytes=12 * 1024**3, vram_used_bytes=0),),
    )
    claimed = with_cold_load_footprint(cold, sizes, gpu)
    assert (claimed.vram_bytes, claimed.gpu_index, claimed.ram_bytes) == (5_200_000_000, 0, 0)
    cpu_only = RuntimeResourceSnapshot(generation=1, observed_at=now)
    assert with_cold_load_footprint(cold, sizes, cpu_only).ram_bytes == 5_200_000_000
    loaded = RuntimeResourceSnapshot(generation=1, observed_at=now, loaded_models=(LoadedModelState(name="qwen3:8b"),))
    assert with_cold_load_footprint(cold, sizes, loaded) == cold
    assert with_cold_load_footprint(cold, {}, gpu) == cold
