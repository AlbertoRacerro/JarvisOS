from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.modules.ai.contracts import AIResponse, AIUsage, AIUsageSource, RoutingDecision
from app.modules.ai.execution import AiTaskOutcome
from app.modules.ai.models import AITaskRunRequest
from app.modules.ai.routing.bridge import (
    AUTO_CLASSIFICATION_MODEL,
    CONTEXT_LEVEL_DEEP,
    CONTEXT_LEVEL_LIGHT,
    CONTEXT_LEVEL_NONE,
    CONTEXT_LEVEL_STANDARD,
    CONTROL_BLOCKED,
    CONTROL_NEEDS_CLARIFICATION,
    CONTROL_PROPOSED_EXTERNAL,
    build_auto_decision_bundle,
    build_auto_router_input,
    capability_from_classification,
    context_decision_from_classification,
    resolve_bridge_outcome_from_decision,
    run_auto_task,
)
from app.modules.ai.routing.capability_route_matrix import local_route_for_capability
from app.modules.local_ai.classification.contracts import (
    AllowedNextStep,
    ClassificationAttemptDiagnostics,
    ClassificationBudgetPolicy,
    ClassificationResultSource,
    ClassificationServiceResult,
    ComplexityHint,
    ProjectArea,
    SensitivityHint,
    TaskType,
    make_advisory_hints,
    make_output,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_MODEL", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)

    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()


def _all_ai_jobs() -> list[dict[str, object]]:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        rows = connection.execute("SELECT * FROM ai_jobs ORDER BY created_at ASC").fetchall()
    return [dict(row) for row in rows]


def _safe_decision(**overrides) -> dict:
    decision = {
        "route_action": "answer_local",
        "route_tier": "LOCAL_FAST",
        "provider_candidate": "local:qwen",
        "response_allowed_now": True,
        "external_allowed": False,
        "provider_call_allowed_now": False,
        "external_network_allowed_now": False,
        "tool_execution_allowed_now": False,
        "state_change_allowed_now": False,
        "allowed_execution_mode": "answer_only",
        "modifies_state": False,
        "side_effect_level": "none",
        "environment_type": "chat",
        "proposed_external_target": None,
    }
    decision.update(overrides)
    return decision


def _fake_success_outcome(route_class: str) -> AiTaskOutcome:
    response = AIResponse(
        provider_id="fake-runner",
        model_id="fake-runner-model",
        request_id="test-request",
        text=f"fake auto response for {route_class}",
        usage=AIUsage(
            provider_id="fake-runner",
            model_id="fake-runner-model",
            input_tokens=1,
            output_tokens=1,
            usage_source=AIUsageSource.estimated,
        ),
    )
    return AiTaskOutcome(
        status="success",
        ledger_id=f"ledger-{route_class}",
        selected_route_class=route_class,
        decision=RoutingDecision(
            provider_id="fake-runner",
            model_id="fake-runner-model",
            decision_reason=f"test:{route_class}",
        ),
        response=response,
    )


def _diagnostics(model_name: str = AUTO_CLASSIFICATION_MODEL) -> ClassificationAttemptDiagnostics:
    return ClassificationAttemptDiagnostics(
        model_name=model_name,
        endpoint="http://localhost:11434/api/chat",
        prompt_chars=12,
        input_chars=12,
        max_output_tokens=256,
        temperature=0,
        timeout_seconds=15,
        latency_ms=1,
        raw_content_empty=False,
        thinking_present=False,
        done_reason="stop",
        schema_valid=True,
        fallback_used=False,
        fallback_reason=None,
    )


def _classification_result(
    *,
    task_type: TaskType = TaskType.engineering_question,
    project_area: ProjectArea = ProjectArea.general_engineering,
    complexity_hint: ComplexityHint = ComplexityHint.medium,
    needs_context: bool = False,
    sensitivity_hint: SensitivityHint = SensitivityHint.internal,
    allowed_next_step: AllowedNextStep = AllowedNextStep.answer_locally,
    confidence: float = 0.9,
    source: ClassificationResultSource = ClassificationResultSource.model,
) -> ClassificationServiceResult:
    output = make_output(
        task_type=task_type,
        project_area=project_area,
        complexity_hint=complexity_hint,
        needs_context=needs_context,
        sensitivity_hint=sensitivity_hint,
        allowed_next_step=allowed_next_step,
        confidence=confidence,
    )
    return ClassificationServiceResult(
        classification=output,
        advisory_hints=make_advisory_hints(output),
        source=source,
        model_output_accepted=source == ClassificationResultSource.model,
        deterministic_reasons=["test"],
        diagnostics=_diagnostics(),
    )


def test_auto_classifier_uses_qwen_override_without_changing_global_default() -> None:
    adapters = []

    def fake_classifier(request, *, adapter=None):
        adapters.append(adapter)
        return _classification_result(task_type=TaskType.documentation, complexity_hint=ComplexityHint.low)

    bundle = build_auto_decision_bundle(
        AITaskRunRequest(prompt="Classify this locally", route_class="auto"),
        classifier_func=fake_classifier,
    )

    assert adapters
    assert adapters[0].config.model_name == AUTO_CLASSIFICATION_MODEL
    assert ClassificationBudgetPolicy().model_name == "gemma4:12b-it-qat"
    assert bundle.classification_result.diagnostics.model_name == AUTO_CLASSIFICATION_MODEL


def test_task_endpoint_auto_executes_classified_local_route_through_injected_runner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.ai.execution as execution
    import app.modules.ai.routing.bridge as bridge

    calls: list[dict[str, object]] = []

    def fake_classifier(request, *, adapter=None):
        return _classification_result(task_type=TaskType.code_change, complexity_hint=ComplexityHint.medium)

    def fake_run_ai_task(**kwargs):
        calls.append(kwargs)
        return _fake_success_outcome(str(kwargs["route_class"]))

    monkeypatch.setattr(bridge, "classify_text", fake_classifier)
    monkeypatch.setattr(execution, "run_ai_task", fake_run_ai_task)

    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "review this code locally", "route_class": "auto", "task_kind": "general", "max_tokens": 64},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["selected_route_class"] == "local:coder"
    assert body["provider_id"] == "fake-runner"
    assert body["model_id"] == "fake-runner-model"
    assert body["response_text"] == "fake auto response for local:coder"
    assert body["auto_metadata"]["classification"]["task_type"] == "code_change"
    assert body["auto_metadata"]["capability"]["row"] == "coding"
    assert body["auto_metadata"]["capability"]["local_route_class"] == "local:coder"
    assert len(calls) == 1
    assert calls[0]["route_class"] == "local:coder"


@pytest.mark.parametrize(
    ("task_type", "complexity_hint", "expected_capability", "expected_route_class"),
    [
        (TaskType.documentation, ComplexityHint.low, "simple", "local:fast"),
        (TaskType.engineering_question, ComplexityHint.medium, "general_reasoning", "local:general"),
        (TaskType.project_planning, ComplexityHint.high, "general_reasoning", "local:general"),
        (TaskType.code_change, ComplexityHint.medium, "coding", "local:coder"),
        (TaskType.bug_report, ComplexityHint.low, "coding", "local:coder"),
        (TaskType.code_change, ComplexityHint.high, "heavy_coding", "local:coder_heavy"),
    ],
)
def test_auto_capability_matrix_maps_semantic_classification_to_local_route(
    task_type: TaskType,
    complexity_hint: ComplexityHint,
    expected_capability: str,
    expected_route_class: str,
) -> None:
    result = _classification_result(task_type=task_type, complexity_hint=complexity_hint)
    capability = capability_from_classification(result.classification)

    assert capability == expected_capability
    assert local_route_for_capability(capability) == expected_route_class


def test_auto_router_input_keeps_external_policy_blocked() -> None:
    request = AITaskRunRequest(prompt="local task", route_class="auto")
    router_input = build_auto_router_input(request, _classification_result(sensitivity_hint=SensitivityHint.secret))

    assert router_input["provider_policy"]["allowed_provider_tiers"] == ["LOCAL_ONLY", "LOCAL_FAST"]
    assert router_input["provider_policy"]["blocked_provider_tiers"] == [
        "CHEAP_EXTERNAL",
        "SCIENTIFIC_MEDIUM",
        "FRONTIER",
    ]
    assert router_input["phase_a_signals"]["contains_secret_or_credential"] is True
    assert router_input["phase_a_signals"]["sensitivity_bucket_proposal"] == "sensitive"


def test_auto_builder_uses_internal_or_unknown_sensitivity_without_claiming_public() -> None:
    without_context = build_auto_router_input(
        AITaskRunRequest(prompt="local task", route_class="auto"),
        _classification_result(sensitivity_hint=SensitivityHint.public),
    )
    with_context = build_auto_router_input(
        AITaskRunRequest(prompt="local task", route_class="auto", include_project_context=True),
        _classification_result(sensitivity_hint=SensitivityHint.public),
    )

    assert without_context["phase_a_signals"]["sensitivity_bucket_proposal"] == "unknown"
    assert with_context["phase_a_signals"]["sensitivity_bucket_proposal"] == "internal"


def test_auto_context_decision_requires_user_permission_and_zephyr_need() -> None:
    request_without_permission = AITaskRunRequest(prompt="Zephyr context", route_class="auto")
    request_with_permission = AITaskRunRequest(
        prompt="Zephyr context",
        route_class="auto",
        include_project_context=True,
    )
    workspace_context = _classification_result(
        project_area=ProjectArea.project,
        needs_context=True,
        sensitivity_hint=SensitivityHint.internal,
    )
    general_context = _classification_result(
        project_area=ProjectArea.general_engineering,
        needs_context=True,
        sensitivity_hint=SensitivityHint.internal,
    )

    denied = context_decision_from_classification(
        request_without_permission,
        workspace_context,
        local_route_class="local:general",
    )
    allowed = context_decision_from_classification(
        request_with_permission,
        workspace_context,
        local_route_class="local:general",
    )
    not_project_context = context_decision_from_classification(
        request_with_permission,
        general_context,
        local_route_class="local:general",
    )

    assert denied["final_include_project_context"] is False
    assert denied["context_decision_reason"] == "user_context_permission_off"
    assert denied["context_level"] == CONTEXT_LEVEL_NONE
    assert allowed["final_include_project_context"] is True
    assert allowed["context_level"] == CONTEXT_LEVEL_STANDARD
    assert allowed["context_decision_reason"] == "classifier_requested_workspace_standard_context"
    assert allowed["source_selection_status"] == "budget_only"
    assert not_project_context["final_include_project_context"] is False
    assert not_project_context["context_level"] == CONTEXT_LEVEL_NONE
    assert not_project_context["context_decision_reason"] == "classifier_context_not_workspace_relevant"


def test_auto_context_fallback_uses_user_permission_cap() -> None:
    fallback_result = _classification_result(
        needs_context=True,
        source=ClassificationResultSource.fallback,
        confidence=0,
    )

    off = context_decision_from_classification(
        AITaskRunRequest(prompt="fallback", route_class="auto"),
        fallback_result,
        local_route_class="local:general",
    )
    on = context_decision_from_classification(
        AITaskRunRequest(prompt="fallback", route_class="auto", include_project_context=True),
        fallback_result,
        local_route_class="local:general",
    )

    assert off["final_include_project_context"] is False
    assert off["context_level"] == CONTEXT_LEVEL_NONE
    assert off["requested_context_level"] == CONTEXT_LEVEL_LIGHT
    assert on["final_include_project_context"] is True
    assert on["context_level"] == CONTEXT_LEVEL_LIGHT
    assert on["context_decision_reason"] == "classification_fallback_uses_conservative_context"


def test_auto_preserves_manual_context_blocks_when_workspace_context_level_is_none() -> None:
    manual_blocks = [{"source": "manual", "text": "Manual context must stay attached."}]
    calls: list[dict[str, object]] = []

    def fake_classifier(request, *, adapter=None):
        return _classification_result(
            task_type=TaskType.documentation,
            complexity_hint=ComplexityHint.low,
            needs_context=False,
        )

    def fake_run_ai_task(**kwargs):
        calls.append(kwargs)
        return _fake_success_outcome(str(kwargs["route_class"]))

    response = run_auto_task(
        AITaskRunRequest(
            prompt="Summarize only the attached note.",
            route_class="auto",
            include_project_context=True,
            context_blocks=manual_blocks,
        ),
        run_ai_task_func=fake_run_ai_task,
        classifier_func=fake_classifier,
    )

    assert response.status == "success"
    assert calls[0]["context_blocks"] == manual_blocks
    assert response.auto_metadata["context_decision"]["context_level"] == CONTEXT_LEVEL_NONE
    assert response.auto_metadata["context_decision"]["final_include_project_context"] is False
    assert response.auto_metadata["context_decision"]["manual_context_blocks_count"] == 1
    assert response.auto_metadata["context_decision"]["workspace_context_blocks_count"] == 0


@pytest.mark.parametrize(
    ("classification", "prompt", "expected_level", "expected_budget"),
    [
        (
            _classification_result(
                task_type=TaskType.documentation,
                project_area=ProjectArea.project,
                complexity_hint=ComplexityHint.low,
                needs_context=False,
            ),
            "Explain this self-contained prompt.",
            CONTEXT_LEVEL_NONE,
            0,
        ),
        (
            _classification_result(
                task_type=TaskType.engineering_question,
                project_area=ProjectArea.project,
                complexity_hint=ComplexityHint.low,
                needs_context=True,
            ),
            "Check this bounded Zephyr nitrate note.",
            CONTEXT_LEVEL_LIGHT,
            6000,
        ),
        (
            _classification_result(
                task_type=TaskType.engineering_question,
                project_area=ProjectArea.project,
                complexity_hint=ComplexityHint.medium,
                needs_context=True,
            ),
            "Compare Zephyr reactor tradeoffs.",
            CONTEXT_LEVEL_STANDARD,
            16000,
        ),
    ],
)
def test_auto_context_levels_are_conservative_budget_posture(
    classification: ClassificationServiceResult,
    prompt: str,
    expected_level: str,
    expected_budget: int,
) -> None:
    decision = context_decision_from_classification(
        AITaskRunRequest(prompt=prompt, route_class="auto", include_project_context=True),
        classification,
        local_route_class="local:general",
    )

    assert decision["context_level"] == expected_level
    assert decision["context_budget_chars"] == expected_budget
    assert decision["source_selection_status"] == (
        "budget_only" if expected_level != CONTEXT_LEVEL_NONE else "not_requested"
    )
    assert decision["source_selection_note"] == "budget_only_no_retrieval_intelligence"


def test_auto_context_deep_requires_explicit_project_history_need_not_complexity_alone() -> None:
    high_complexity = _classification_result(
        task_type=TaskType.project_planning,
        project_area=ProjectArea.project,
        complexity_hint=ComplexityHint.high,
        needs_context=True,
    )
    semantic_deep_need = _classification_result(
        task_type=TaskType.project_planning,
        project_area=ProjectArea.project,
        complexity_hint=ComplexityHint.high,
        needs_context=True,
        allowed_next_step=AllowedNextStep.request_bounded_context,
    )

    complexity_only = context_decision_from_classification(
        AITaskRunRequest(
            prompt="Plan Zephyr next engineering task.",
            route_class="auto",
            include_project_context=True,
        ),
        high_complexity,
        local_route_class="local:general",
    )
    explicit_history = context_decision_from_classification(
        AITaskRunRequest(
            prompt="Use project history and project documents to audit the Zephyr architecture plan.",
            route_class="auto",
            include_project_context=True,
        ),
        semantic_deep_need,
        local_route_class="local:general",
    )

    assert complexity_only["requested_context_level"] == CONTEXT_LEVEL_STANDARD
    assert complexity_only["context_level"] == CONTEXT_LEVEL_STANDARD
    assert explicit_history["requested_context_level"] == CONTEXT_LEVEL_DEEP
    assert explicit_history["context_level"] == CONTEXT_LEVEL_DEEP
    assert explicit_history["context_budget_chars"] == 24000


def test_auto_context_deep_downgrades_when_selected_route_cannot_handle_deep() -> None:
    result = _classification_result(
        task_type=TaskType.project_planning,
        project_area=ProjectArea.project,
        complexity_hint=ComplexityHint.high,
        needs_context=True,
        allowed_next_step=AllowedNextStep.request_bounded_context,
    )

    decision = context_decision_from_classification(
        AITaskRunRequest(
            prompt="Use project history and project documents for the Zephyr planning audit.",
            route_class="auto",
            include_project_context=True,
        ),
        result,
        local_route_class="local:fast",
    )

    assert decision["requested_context_level"] == CONTEXT_LEVEL_DEEP
    assert decision["context_level"] == CONTEXT_LEVEL_STANDARD
    assert decision["context_budget_reason"] == "deep_downgraded_for_selected_local_route"
    assert decision["context_budget_chars"] == 6000


@pytest.mark.parametrize(
    ("sensitivity_hint", "decision", "expected_executed", "expected_status"),
    [
        (
            SensitivityHint.public,
            _safe_decision(route_action="answer_local", route_tier="LOCAL_FAST", allowed_execution_mode="answer_only"),
            True,
            "success",
        ),
        (
            SensitivityHint.internal,
            _safe_decision(route_action="answer_local", route_tier="LOCAL_FAST", allowed_execution_mode="answer_only"),
            True,
            "success",
        ),
        (
            SensitivityHint.unknown,
            _safe_decision(route_action="answer_local", route_tier="LOCAL_FAST", allowed_execution_mode="answer_only"),
            True,
            "success",
        ),
        (
            SensitivityHint.confidential,
            _safe_decision(route_action="route_local", route_tier="LOCAL_ONLY", allowed_execution_mode="propose_only"),
            True,
            "success",
        ),
        (
            SensitivityHint.sensitive_ip,
            _safe_decision(route_action="route_local", route_tier="LOCAL_ONLY", allowed_execution_mode="propose_only"),
            True,
            "success",
        ),
        (
            SensitivityHint.secret,
            _safe_decision(
                route_action="blocked",
                route_tier="BLOCKED",
                response_allowed_now=False,
                allowed_execution_mode="blocked",
            ),
            False,
            CONTROL_BLOCKED,
        ),
    ],
)
def test_auto_executes_safe_local_sensitivity_spectrum(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    sensitivity_hint: SensitivityHint,
    decision: dict,
    expected_executed: bool,
    expected_status: str,
) -> None:
    import app.modules.ai.routing.bridge as bridge

    calls: list[dict[str, object]] = []

    def fake_classifier(request, *, adapter=None):
        return _classification_result(
            task_type=TaskType.engineering_question,
            project_area=ProjectArea.project,
            complexity_hint=ComplexityHint.medium,
            needs_context=False,
            sensitivity_hint=sensitivity_hint,
        )

    def fake_router_policy(*args, **kwargs):
        return decision

    def fake_run_ai_task(**kwargs):
        calls.append(kwargs)
        return _fake_success_outcome(str(kwargs["route_class"]))

    monkeypatch.setattr(bridge, "decide_router_policy", fake_router_policy)

    response = run_auto_task(
        AITaskRunRequest(prompt=f"sensitivity {sensitivity_hint.value}", route_class="auto", max_tokens=64),
        run_ai_task_func=fake_run_ai_task,
        classifier_func=fake_classifier,
    )

    assert response.status == expected_status
    assert bool(calls) is expected_executed
    if expected_executed:
        assert len(calls) == 1
        assert response.selected_route_class == "local:general"
        assert response.provider_id == "fake-runner"
        assert response.model_id == "fake-runner-model"
        assert calls[0]["route_class"] == "local:general"
        assert response.confirmation_payload is None
    else:
        assert response.selected_route_class == "auto"
        assert response.provider_id is None
        assert response.response_text is None


@pytest.mark.parametrize(
    ("task_type", "expected_status"),
    [
        (TaskType.external_api_request, CONTROL_PROPOSED_EXTERNAL),
        (TaskType.ambiguous, CONTROL_NEEDS_CLARIFICATION),
    ],
)
def test_auto_non_executing_classification_controls_cover_external_and_ambiguous(
    client: TestClient,
    task_type: TaskType,
    expected_status: str,
) -> None:
    calls = {"run_ai_task": 0}

    def fake_classifier(request, *, adapter=None):
        return _classification_result(task_type=task_type)

    def fail_run_ai_task(**kwargs):
        calls["run_ai_task"] += 1
        raise AssertionError("control states must not execute")

    response = run_auto_task(
        AITaskRunRequest(prompt=f"control {task_type.value}", route_class="auto", max_tokens=64),
        run_ai_task_func=fail_run_ai_task,
        classifier_func=fake_classifier,
    )

    assert response.status == expected_status
    assert response.provider_id is None
    assert response.response_text is None
    assert calls["run_ai_task"] == 0


@pytest.mark.parametrize(
    "unsafe_override",
    [
        {"proposed_external_target": "external:scientific_medium"},
        {"provider_call_allowed_now": True},
        {"external_network_allowed_now": True},
        {"tool_execution_allowed_now": True},
        {"state_change_allowed_now": True},
    ],
)
def test_auto_local_only_gate_rejects_adversarial_local_decisions(
    client: TestClient,
    unsafe_override: dict[str, object],
) -> None:
    decision = _safe_decision(
        route_action="route_local",
        route_tier="LOCAL_ONLY",
        allowed_execution_mode="propose_only",
        **unsafe_override,
    )
    calls = {"run_ai_task": 0}

    def fail_run_ai_task(**kwargs):
        calls["run_ai_task"] += 1
        raise AssertionError("unsafe Auto local decision must not execute")

    response = resolve_bridge_outcome_from_decision(
        request=AITaskRunRequest(prompt="adversarial local", route_class="auto", max_tokens=64),
        decision=decision,
        run_ai_task_func=fail_run_ai_task,
    )

    assert response.status != "success"
    assert response.provider_id is None
    assert response.response_text is None
    assert calls["run_ai_task"] == 0


def test_auto_metadata_marks_capability_exceeds_local_for_deep_reasoning(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.ai.routing.bridge as bridge
    from app.modules.ai.routing.capability_route_matrix import CAPABILITY_DEEP_REASONING

    def fake_classifier(request, *, adapter=None):
        return _classification_result(
            task_type=TaskType.project_planning,
            project_area=ProjectArea.project,
            complexity_hint=ComplexityHint.high,
            needs_context=False,
        )

    def fake_run_ai_task(**kwargs):
        return _fake_success_outcome(str(kwargs["route_class"]))

    monkeypatch.setattr(bridge, "capability_from_classification", lambda classification: CAPABILITY_DEEP_REASONING)

    response = run_auto_task(
        AITaskRunRequest(prompt="deep local best effort", route_class="auto", max_tokens=64),
        run_ai_task_func=fake_run_ai_task,
        classifier_func=fake_classifier,
    )

    assert response.status == CONTROL_PROPOSED_EXTERNAL
    assert response.response_text is None
    assert response.escalation_proposal is not None
    assert response.escalation_proposal["proposed_route_class"] == "external:reasoning"
    assert response.escalation_proposal["outbound_text"] == "deep local best effort"
    assert response.escalation_proposal["context_excluded"] is True
    assert response.auto_metadata["capability_exceeds_local"] is True
    assert response.auto_metadata["capability"]["row"] == CAPABILITY_DEEP_REASONING
    assert response.auto_metadata["capability"]["capability_exceeds_local"] is True


def test_auto_does_not_build_workspace_context_when_user_permission_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.ai.context_builder as context_builder

    calls: list[dict[str, object]] = []

    def fake_classifier(request, *, adapter=None):
        return _classification_result(
            task_type=TaskType.project_planning,
            project_area=ProjectArea.project,
            complexity_hint=ComplexityHint.high,
            needs_context=True,
            allowed_next_step=AllowedNextStep.request_bounded_context,
        )

    def fake_run_ai_task(**kwargs):
        calls.append(kwargs)
        return _fake_success_outcome(str(kwargs["route_class"]))

    def fail_context_builder(*args, **kwargs):
        raise AssertionError("Auto must not build workspace context without user permission")

    monkeypatch.setattr(context_builder, "build_workspace_context_bundle", fail_context_builder)

    response = run_auto_task(
        AITaskRunRequest(
            prompt="Use project history and project documents to plan Zephyr.",
            route_class="auto",
            include_project_context=False,
        ),
        run_ai_task_func=fake_run_ai_task,
        classifier_func=fake_classifier,
    )

    assert response.status == "success"
    assert response.include_project_context is False
    assert response.workspace_id is None
    assert calls[0]["context_blocks"] == []
    assert response.auto_metadata["context_decision"]["requested_context_level"] == CONTEXT_LEVEL_DEEP
    assert response.auto_metadata["context_decision"]["context_level"] == CONTEXT_LEVEL_NONE


def test_task_endpoint_explicit_route_bypasses_auto_bridge(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.routing import bridge

    def fail_auto_builder(*args, **kwargs):
        raise AssertionError("explicit route must not use auto bridge")

    monkeypatch.setattr(bridge, "build_auto_decision_bundle", fail_auto_builder)

    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "explicit fake route", "route_class": "local:fake", "task_kind": "general"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["selected_route_class"] == "local:fake"


def test_auto_resolver_refuses_injected_external_candidate_without_run_ai_task(client: TestClient) -> None:
    request = AITaskRunRequest(prompt="should not execute external", route_class="auto", max_tokens=64)
    external_decision = _safe_decision(
        route_action="route_external_candidate",
        route_tier="SCIENTIFIC_MEDIUM",
        provider_candidate="external:scientific_medium",
        proposed_external_target="external:scientific_medium",
        allowed_execution_mode="propose_only",
        reason_codes=["adversarial_external_candidate"],
    )
    calls = {"run_ai_task": 0}

    def fail_run_ai_task(**kwargs):
        calls["run_ai_task"] += 1
        raise AssertionError("Auto resolver must not execute external proposals")

    response = resolve_bridge_outcome_from_decision(
        request=request,
        decision=external_decision,
        run_ai_task_func=fail_run_ai_task,
    )

    assert response.status == CONTROL_PROPOSED_EXTERNAL
    assert response.selected_route_class == "auto"
    assert response.provider_id is None
    assert response.model_id is None
    assert response.response_text is None
    assert response.blocked_reason == "auto_external_proposal_refused"
    assert calls["run_ai_task"] == 0

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == response.ledger_id
    assert rows[0]["status"] == "proposed_external"
    assert rows[0]["provider_id"] is None
    assert rows[0]["model_id"] is None
    reason = json.loads(rows[0]["route_reason_json"])
    assert reason["route_action"] == "route_external_candidate"
    assert reason["permissions"]["external_network_allowed_now"] is False


def test_auto_bridge_refuses_router_external_candidate_before_context_or_runner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.ai.context_builder as context_builder
    import app.modules.ai.routing.bridge as bridge

    calls = {"run_ai_task": 0, "context_builder": 0}

    def fake_classifier(request, *, adapter=None):
        return _classification_result(
            task_type=TaskType.project_planning,
            project_area=ProjectArea.project,
            complexity_hint=ComplexityHint.high,
            needs_context=True,
            allowed_next_step=AllowedNextStep.request_bounded_context,
        )

    def fake_router_policy(*args, **kwargs):
        return _safe_decision(
            route_action="route_external_candidate",
            route_tier="SCIENTIFIC_MEDIUM",
            provider_candidate="external:scientific_medium",
            proposed_external_target="external:scientific_medium",
            allowed_execution_mode="propose_only",
            reason_codes=["adversarial_external_candidate"],
        )

    def fail_context_builder(*args, **kwargs):
        calls["context_builder"] += 1
        raise AssertionError("Auto must not build workspace context for external proposals")

    def fail_run_ai_task(**kwargs):
        calls["run_ai_task"] += 1
        raise AssertionError("Auto must not execute external proposals")

    monkeypatch.setattr(bridge, "decide_router_policy", fake_router_policy)
    monkeypatch.setattr(context_builder, "build_workspace_context_bundle", fail_context_builder)

    response = run_auto_task(
        AITaskRunRequest(
            prompt="Use project history and project documents for Zephyr planning.",
            route_class="auto",
            include_project_context=True,
            max_tokens=64,
        ),
        run_ai_task_func=fail_run_ai_task,
        classifier_func=fake_classifier,
    )

    assert response.status == CONTROL_PROPOSED_EXTERNAL
    assert response.provider_id is None
    assert response.model_id is None
    assert response.response_text is None
    assert response.include_project_context is False
    assert response.auto_metadata["context_decision"]["requested_context_level"] == CONTEXT_LEVEL_DEEP
    assert response.auto_metadata["context_decision"]["workspace_context_skipped_reason"] == (
        "auto_execution_not_local_safe"
    )
    assert calls == {"run_ai_task": 0, "context_builder": 0}


@pytest.mark.parametrize(
    ("task_type", "expected_status"),
    [
        (TaskType.external_api_request, CONTROL_PROPOSED_EXTERNAL),
        (TaskType.ambiguous, CONTROL_NEEDS_CLARIFICATION),
        (TaskType.unsafe_tool_request, CONTROL_BLOCKED),
    ],
)
def test_auto_classification_control_states_never_call_run_ai_task(
    client: TestClient,
    task_type: TaskType,
    expected_status: str,
) -> None:
    request = AITaskRunRequest(prompt=f"control {task_type.value}", route_class="auto", max_tokens=64)
    calls = {"run_ai_task": 0}

    def fake_classifier(request, *, adapter=None):
        return _classification_result(task_type=task_type)

    def fail_run_ai_task(**kwargs):
        calls["run_ai_task"] += 1
        raise AssertionError("control states must not call run_ai_task")

    response = run_auto_task(request, run_ai_task_func=fail_run_ai_task, classifier_func=fake_classifier)

    assert response.status == expected_status
    assert response.selected_route_class == "auto"
    assert response.provider_id is None
    assert response.model_id is None
    assert response.response_text is None
    assert response.error_type == expected_status
    assert calls["run_ai_task"] == 0

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == response.ledger_id
    assert rows[0]["status"] == expected_status
    assert rows[0]["input_tokens"] is None
    assert rows[0]["output_tokens"] is None


def test_auto_external_api_request_returns_confirmation_payload(client: TestClient) -> None:
    def fake_classifier(request, *, adapter=None):
        return _classification_result(task_type=TaskType.external_api_request)

    response = run_auto_task(
        AITaskRunRequest(prompt="Call an external provider", route_class="auto", max_tokens=64),
        run_ai_task_func=lambda **kwargs: pytest.fail("external proposal must not execute"),
        classifier_func=fake_classifier,
    )

    assert response.status == CONTROL_PROPOSED_EXTERNAL
    assert response.confirmation_payload is not None
    assert response.confirmation_payload["scope"] == "external_provider_request_detected"
    assert response.confirmation_payload["target"] == "external:scientific_medium"


def test_auto_escalation_excludes_context_and_records_proposal(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.ai.routing.bridge as bridge
    from app.modules.ai.routing.capability_route_matrix import CAPABILITY_DEEP_REASONING

    def fake_classifier(request, *, adapter=None):
        return _classification_result(
            task_type=TaskType.project_planning,
            project_area=ProjectArea.project,
            complexity_hint=ComplexityHint.high,
            needs_context=True,
            sensitivity_hint=SensitivityHint.confidential,
        )

    monkeypatch.setattr(bridge, "capability_from_classification", lambda classification: CAPABILITY_DEEP_REASONING)
    response = run_auto_task(
        AITaskRunRequest(
            prompt="raw prompt only",
            route_class="auto",
            max_tokens=64,
            include_project_context=True,
            context_blocks=[{"source": "manual", "content": "do not send"}],
        ),
        run_ai_task_func=lambda **kwargs: pytest.fail("proposal must not execute"),
        classifier_func=fake_classifier,
    )

    assert response.status == CONTROL_PROPOSED_EXTERNAL
    assert response.escalation_proposal is not None
    assert response.escalation_proposal["outbound_text"] == "raw prompt only"
    assert response.escalation_proposal["context_excluded"] is True
    assert response.escalation_proposal["sensitivity_warning"] is not None
    # Response payload keeps the full outbound text for the UI confirmation card.
    assert response.escalation_proposal["outbound_text"] == "raw prompt only"
    rows = _all_ai_jobs()
    assert len(rows) == 1
    route_reason = json.loads(rows[0]["route_reason_json"])
    # Ledger must NOT persist the raw prompt (spine contract: digests only) — the
    # escalation path is exactly the sensitive case (confidential/IP prompts).
    ledger_proposal = route_reason["escalation_proposal"]
    assert "outbound_text" not in ledger_proposal
    assert ledger_proposal["outbound_text_digest"]
    assert ledger_proposal["outbound_text_digest"] != "raw prompt only"
    # Non-executing proposal row must not be stamped as a real external call:
    # provider/model/cost columns stay unset; the estimate lives only in the JSON.
    assert rows[0]["provider_id"] is None
    assert rows[0]["model_id"] is None
    assert rows[0]["cost_estimate"] is None


def test_auto_secret_deep_reasoning_produces_no_escalation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.ai.routing.bridge as bridge
    from app.modules.ai.routing.capability_route_matrix import CAPABILITY_DEEP_REASONING

    def fake_classifier(request, *, adapter=None):
        return _classification_result(
            task_type=TaskType.project_planning,
            complexity_hint=ComplexityHint.high,
            sensitivity_hint=SensitivityHint.secret,
        )

    monkeypatch.setattr(bridge, "capability_from_classification", lambda classification: CAPABILITY_DEEP_REASONING)
    response = run_auto_task(
        AITaskRunRequest(prompt="secret token", route_class="auto", max_tokens=64),
        run_ai_task_func=lambda **kwargs: pytest.fail("secret control state must not execute"),
        classifier_func=fake_classifier,
    )

    assert response.status == CONTROL_BLOCKED
    assert response.escalation_proposal is None
