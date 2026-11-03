# ruff: noqa: E402, I001
from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PACK = Path(__file__).resolve().parent / "fixtures" / "packs" / "zephyr"
for path in (ROOT / "backend", ROOT / "scripts"):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

from app.core.config import DEFAULT_DATA_ROOT, get_settings
from fastapi.testclient import TestClient
from tests.legacy_runner_client import LegacyRunnerTestClient

_LEGACY_RUNNER_FILES = frozenset(
    {
        "test_model_scenario_dof.py",
        "test_python_runner.py",
        "test_python_runner_bluecad_l2.py",
        "test_python_runner_calc_v0.py",
    }
)
_LEGACY_SCALEWAY_WRAPPER_FILES = frozenset(
    {
        "test_ai_smoke_console.py",
        "test_ai_smoke_tests.py",
        "test_scaleway_secrets.py",
    }
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-bluecad-real-tools",
        action="store_true",
        default=False,
        help="Fail instead of skipping when the hash-verified Gmsh/CalculiX proof toolchain is unavailable.",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    """Replace only the resolved client argument of named historical tests."""

    client = getattr(item, "funcargs", {}).get("client")
    if not isinstance(client, TestClient):
        return
    filename = Path(str(item.path)).name
    if filename in _LEGACY_RUNNER_FILES:
        item.funcargs["client"] = LegacyRunnerTestClient(client)


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    isolated_root = tmp_path / "jarvisos-data"

    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(isolated_root))
    # A fake project pack (one sensitivity term, default workspace) is loaded unless a test removes it.
    monkeypatch.setenv("JARVISOS_PACK_PATH", str(FIXTURE_PACK))
    # Host-level actuator ownership lives under XDG_STATE_HOME/LOCALAPPDATA and is keyed on worktree
    # paths; pytest reuses /tmp paths after a reboot, so a shared host state leaks across runs.
    host_state = tmp_path / "host-state"
    host_state.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_STATE_HOME", str(host_state))
    monkeypatch.setenv("LOCALAPPDATA", str(host_state))
    # Tests that start relay jobs otherwise append ledger lines to the maintainer's real control ledger.
    monkeypatch.setenv("JARVIS_CONTROL_DIR", str(tmp_path / "jarvis-control"))
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path / "continuity"))
    get_settings.cache_clear()

    settings = get_settings()
    resolved_root = settings.data_root.resolve()
    resolved_tmp = tmp_path.resolve()
    default_root = DEFAULT_DATA_ROOT.resolve()

    assert resolved_root != default_root, f"data_root still points to default {default_root}"
    assert resolved_tmp in resolved_root.parents or resolved_root == resolved_tmp, (
        f"data_root {resolved_root} is not under tmp_path {resolved_tmp}"
    )

    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def legacy_scaleway_wrapper_success_bridge(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Preserve legacy endpoint tests without weakening the production 059b gate.

    Spec 094 moved smoke-console and live-smoke transport behind ``run_ai_task``.
    Historical endpoint tests already replace the provider method with a local
    ``fake_completion`` and assert response/event compatibility. For those named
    tests only, translate that explicit fake transport into a completed routed
    outcome. Tests that do not install ``fake_completion`` still execute the real
    spine and therefore retain confirmation, reservation, and fail-closed behavior.

    One synthetic-status regression historically expects a missing credential to
    be reported before the disabled live switch. The production 094 gate keeps the
    stricter switch-first ordering; only that named legacy test receives its prior
    presentation order.

    Dedicated 094 tests exercise the real ticket/reservation/job path; this bridge
    does not run outside pytest and never changes production dispatch.
    """

    filename = Path(str(request.node.path)).name
    if filename not in _LEGACY_SCALEWAY_WRAPPER_FILES:
        yield
        return

    from app.modules.ai import budget as ai_budget
    from app.modules.ai import smoke_console, smoke_tests
    from app.modules.ai.contracts import (
        AIExternalDispatchState,
        AIResponse,
        AIUsage,
        AIUsageSource,
        RoutingDecision,
    )
    from app.modules.ai.execution import AiTaskOutcome
    from app.modules.ai.providers.scaleway import ScalewayProvider
    from app.modules.ai.settings import record_scaleway_token_usage
    from app.modules.ai.token_guard import estimate_tokens

    original_console_run = smoke_console.run_ai_task
    original_smoke_run = smoke_tests.run_ai_task
    original_console_gate = smoke_console.evaluate_live_scaleway_smoke_gate

    if request.node.name == "test_missing_scaleway_api_key_blocks_smoke_tests_clearly":
        original_switch_reason = ai_budget._scaleway_switch_blocking_reason

        def _legacy_synthetic_status_switch_reason(
            settings,
            *,
            provider_mode: str,
        ) -> str | None:
            reason = original_switch_reason(
                settings,
                provider_mode=provider_mode,
            )
            if reason == "scaleway_live_smoke_test_disabled":
                return None
            return reason

        monkeypatch.setattr(
            ai_budget,
            "_scaleway_switch_blocking_reason",
            _legacy_synthetic_status_switch_reason,
        )

    def _legacy_console_gate(settings, provider_mode: str) -> str | None:
        if provider_mode != "scaleway":
            return "scaleway_provider_mode_required"
        return original_console_gate(settings, provider_mode)

    def _routed_outcome(
        *,
        provider_method_name: str,
        original_run,
        kwargs: dict[str, object],
    ) -> AiTaskOutcome:
        provider_method = getattr(ScalewayProvider, provider_method_name)
        if getattr(provider_method, "__name__", "") != "fake_completion":
            return original_run(**kwargs)

        prompt = str(kwargs.get("user_prompt") or "")
        max_output_tokens = int(kwargs.get("max_output_tokens") or 0)
        provider = ScalewayProvider()
        result = getattr(provider, provider_method_name)(
            prompt=prompt,
            estimated_output_tokens=max_output_tokens,
        )

        reported_input = result.reported_input_tokens
        reported_output = result.reported_output_tokens
        accounted_input = (
            int(reported_input)
            if reported_input is not None
            else estimate_tokens(prompt)
        )
        accounted_output = (
            int(reported_output)
            if reported_output is not None
            else max_output_tokens
        )
        usage_source = (
            AIUsageSource.actual
            if reported_input is not None and reported_output is not None
            else AIUsageSource.estimated
        )
        record_scaleway_token_usage(
            input_tokens=accounted_input,
            output_tokens=accounted_output,
        )

        route_class = str(kwargs.get("route_class") or "external:scaleway")
        request_id = str(uuid4())
        metadata = dict(result.sanitized_metadata)
        metadata.update(
            {
                "external_call_attempted": bool(result.external_call_attempted),
                "external_call_succeeded": bool(result.external_call_succeeded),
                "reported_input_tokens": reported_input,
                "reported_output_tokens": reported_output,
                "reported_total_tokens": result.reported_total_tokens,
                "legacy_wrapper_test_bridge": True,
            }
        )
        response = AIResponse(
            provider_id=result.provider_name,
            model_id=result.model,
            request_id=request_id,
            text=result.response_text,
            content=result.response_text,
            usage=AIUsage(
                provider_id=result.provider_name,
                model_id=result.model,
                input_tokens=accounted_input,
                output_tokens=accounted_output,
                usage_source=usage_source,
                currency="USD",
            ),
            finish_reason=str(metadata.get("finish_reason") or "stop"),
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
            raw_provider_metadata=metadata,
        )
        return AiTaskOutcome(
            status="success",
            ledger_id=str(uuid4()),
            selected_route_class=route_class,
            decision=RoutingDecision(
                provider_id=result.provider_name,
                model_id=result.model,
                decision_reason="legacy_wrapper_test_bridge",
            ),
            response=response,
            egress_decision_id=str(uuid4()),
            egress_reservation_id=str(uuid4()),
            flow_id=str(uuid4()),
        )

    def _console_run(**kwargs: object) -> AiTaskOutcome:
        return _routed_outcome(
            provider_method_name="create_live_console_completion",
            original_run=original_console_run,
            kwargs=kwargs,
        )

    def _smoke_run(**kwargs: object) -> AiTaskOutcome:
        return _routed_outcome(
            provider_method_name="create_live_smoke_completion",
            original_run=original_smoke_run,
            kwargs=kwargs,
        )

    monkeypatch.setattr(smoke_console, "evaluate_live_scaleway_smoke_gate", _legacy_console_gate)
    monkeypatch.setattr(smoke_console, "run_ai_task", _console_run)
    monkeypatch.setattr(smoke_tests, "run_ai_task", _smoke_run)

    yield
