from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.modules.ai import execution, gateway, thread_service
from app.modules.ai.contracts import RoutingDecision
from app.modules.ai.execution import TASK_KIND_DEFAULT_ROUTE, resolve_effective_route_class
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.jarvis_context_models import JarvisContextRequest, JarvisExactRef, JarvisRouteDescriptor
from app.modules.ai.models import AITaskRunRequest
from app.modules.ai.thread_models import AIThreadSubmit

EXPECTED_TASK_KIND_DEFAULT_ROUTES = {
    "general": "local:fake",
    "test": "local:fake",
    "synthesis": "local:fake",
    "code_review": "local:fake",
    "architecture_review": "local:fake",
}


def test_task_kind_default_route_table_matches_canonical_contract() -> None:
    assert TASK_KIND_DEFAULT_ROUTE == EXPECTED_TASK_KIND_DEFAULT_ROUTES


@pytest.mark.parametrize("task_kind, expected", list(EXPECTED_TASK_KIND_DEFAULT_ROUTES.items()))
def test_resolve_effective_route_uses_every_task_kind_default_when_omitted(
    task_kind: str,
    expected: str,
) -> None:
    assert resolve_effective_route_class(task_kind=task_kind, route_class=None) == expected


def test_resolve_effective_route_uses_unknown_task_fallback() -> None:
    assert resolve_effective_route_class(task_kind="unknown-task", route_class=None) == "local:fake"


def test_resolve_effective_route_treats_blank_route_as_omitted() -> None:
    assert resolve_effective_route_class(task_kind="general", route_class="") == "local:fake"


@pytest.mark.parametrize("route_class", ["local:fake", "external:scaleway"])
def test_resolve_effective_route_preserves_explicit_route(route_class: str) -> None:
    assert resolve_effective_route_class(task_kind="general", route_class=route_class) == route_class


def test_resolve_effective_route_preserves_explicit_auto() -> None:
    assert resolve_effective_route_class(task_kind="general", route_class="auto") == "auto"


def test_direct_execution_and_gateway_share_omitted_canonical_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_route = "external:test"
    monkeypatch.setitem(TASK_KIND_DEFAULT_ROUTE, "general", canonical_route)
    binding = ProviderBinding(
        route_class=canonical_route,
        provider_id="test-provider",
        model_id="test-model",
        requires_network=True,
        max_output_tokens=128,
        execution_class="external",
        context_window_tokens=4096,
    )
    decision = RoutingDecision(
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        decision_reason=f"bound:{canonical_route}",
    )
    monkeypatch.setattr(execution, "resolve_binding", lambda *_args, **_kwargs: (binding, decision))

    direct_seen: list[str] = []

    def fake_external(**kwargs):
        direct_seen.append(kwargs["selected_route_class"])
        return SimpleNamespace(selected_route_class=kwargs["selected_route_class"])

    monkeypatch.setattr(execution, "_run_external_network_task", fake_external)
    direct = execution.run_ai_task(user_prompt="route me", task_kind="general", route_class=None)
    assert direct.selected_route_class == canonical_route
    assert direct_seen == [canonical_route]

    gateway_seen: list[str] = []
    monkeypatch.setattr(gateway, "resolve_binding", lambda *_args, **_kwargs: (binding, decision))
    monkeypatch.setattr(gateway, "get_ai_settings", lambda: object())
    monkeypatch.setattr(
        gateway,
        "evaluate_ai_status",
        lambda *_args, **_kwargs: SimpleNamespace(external_calls_allowed=True, blocking_reason=None),
    )

    def fake_run_ai_task(**kwargs):
        gateway_seen.append(kwargs["route_class"])
        return SimpleNamespace(
            status="success",
            ledger_id="ledger",
            selected_route_class=kwargs["route_class"],
            decision=decision,
            response=None,
            error_type=None,
            context_digest=None,
            context_sources_count=0,
            records_parse_error=None,
            proposed_record_ids=[],
        )

    monkeypatch.setattr(execution, "run_ai_task", fake_run_ai_task)
    response = gateway.AIGateway().run_task(AITaskRunRequest(prompt="route me"))
    assert response.selected_route_class == canonical_route
    assert gateway_seen == [canonical_route]


def test_gateway_preserves_auto_routing_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    from app.modules.ai.routing import bridge

    monkeypatch.setattr(bridge, "run_auto_task", lambda _request: sentinel)

    assert (
        gateway.AIGateway().run_task(AITaskRunRequest(prompt="route me", route_class="auto"))
        is sentinel
    )


def _exact_ref_payload(*, route_class: str | None = None) -> AIThreadSubmit:
    exact_ref = JarvisExactRef(
        workspace_id="w",
        owner="memory",
        kind="parameter",
        id="p1",
        revision="1",
    )
    jarvis_context = JarvisContextRequest(
        workspace_id="w",
        route=JarvisRouteDescriptor(route_id="ai-threads", canonical_path="/ai-threads"),
        added_context_refs=[exact_ref],
    )
    return AIThreadSubmit(
        request_id="r1",
        prompt="use exact ref",
        task_kind="general",
        route_class=route_class,
        jarvis_context=jarvis_context,
        expected_jarvis_context_digest="sha256:" + "0" * 64,
    )


def test_explicit_external_exact_ref_rejected_before_reservation_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _exact_ref_payload(route_class="external:scaleway")
    monkeypatch.setattr(thread_service, "_find_existing_interaction", lambda **_kwargs: None)
    monkeypatch.setattr(
        thread_service,
        "_reserve_interaction",
        lambda **_kwargs: pytest.fail("reservation must not occur before exact-ref route rejection"),
    )
    monkeypatch.setattr(
        thread_service,
        "run_ai_task",
        lambda **_kwargs: pytest.fail("dispatch/spend must not occur before exact-ref route rejection"),
    )

    with pytest.raises(
        thread_service.AIThreadConflictError,
        match="exact-ref Jarvis context is unavailable for external routes",
    ):
        thread_service.submit_interaction(workspace_id="w", thread_id="t", payload=payload)


def test_omitted_external_canonical_default_rejects_exact_ref_before_reservation_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(TASK_KIND_DEFAULT_ROUTE, "general", "external:scaleway")
    payload = _exact_ref_payload(route_class=None)
    monkeypatch.setattr(thread_service, "_find_existing_interaction", lambda **_kwargs: None)
    monkeypatch.setattr(
        thread_service,
        "_reserve_interaction",
        lambda **_kwargs: pytest.fail("reservation must not occur before exact-ref route rejection"),
    )
    monkeypatch.setattr(
        thread_service,
        "run_ai_task",
        lambda **_kwargs: pytest.fail("dispatch/spend must not occur before exact-ref route rejection"),
    )

    with pytest.raises(
        thread_service.AIThreadConflictError,
        match="exact-ref Jarvis context is unavailable for external routes",
    ):
        thread_service.submit_interaction(workspace_id="w", thread_id="t", payload=payload)


def test_omitted_local_default_allows_exact_ref_through_existing_preview_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(TASK_KIND_DEFAULT_ROUTE, "general", "local:fake")
    payload = _exact_ref_payload(route_class=None)
    expected_blocks = [{"source": "jarvis", "content": "validated"}]
    seen: list[tuple[JarvisContextRequest, str]] = []

    def fake_require_dispatchable_preview(context, expected_digest):
        seen.append((context, expected_digest))
        return SimpleNamespace(blocks=expected_blocks)

    monkeypatch.setattr(thread_service, "require_dispatchable_preview", fake_require_dispatchable_preview)

    assert thread_service._context_blocks_for_new_submit("w", payload) == expected_blocks
    assert seen == [(payload.jarvis_context, payload.expected_jarvis_context_digest)]
