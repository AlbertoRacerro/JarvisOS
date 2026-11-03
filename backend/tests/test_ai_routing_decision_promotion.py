from __future__ import annotations

import copy
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.modules.ai.routing.invariants import validate_router_decision_for_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "backend" / "tests" / "fixtures" / "router_policy" / "base_router_policy_fixture.json"
NOW = "2026-06-24T10:00:00+00:00"


def _fixture_input() -> dict[str, Any]:
    return copy.deepcopy(json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["input"])


def _enable_external_policy(input_obj: dict[str, Any]) -> dict[str, Any]:
    input_obj["user_policy"]["external_routing_enabled"] = True
    input_obj["user_policy"]["external_requires_confirmation"] = True
    input_obj["provider_policy"] = {
        "allowed_provider_tiers": ["LOCAL_ONLY", "LOCAL_FAST", "SCIENTIFIC_MEDIUM"],
        "blocked_provider_tiers": ["FRONTIER"],
    }
    input_obj["budget_policy"]["max_tier"] = "SCIENTIFIC_MEDIUM"
    input_obj["phase_a_signals"]["external_provider_allowed"] = True
    return input_obj


def _high_complexity_public_input() -> dict[str, Any]:
    input_obj = _enable_external_policy(_fixture_input())
    input_obj["phase_a_signals"]["sensitivity_bucket_proposal"] = "public"
    input_obj["router_hint"].update(
        {
            "task_type": "analysis",
            "complexity": "high",
            "domain": "scientific",
            "needs_scientific_depth": True,
            "needs_reasoning": True,
        }
    )
    return input_obj


def _with_sensitivity(decision: dict[str, Any], input_obj: dict[str, Any]) -> dict[str, Any]:
    enriched = copy.deepcopy(decision)
    enriched["sensitivity_bucket"] = input_obj.get("phase_a_signals", {}).get("sensitivity_bucket_proposal")
    return enriched


@pytest.mark.parametrize(
    ("case_name", "input_builder"),
    [
        (
            "secret_api_key_blocked",
            lambda: _mutate(
                _fixture_input(),
                message_text="Do not expose API key sk-test-secret-12345678.",
                phase_a_signals={
                    "contains_secret_or_credential": True,
                    "sensitivity_bucket_proposal": "secret",
                },
            ),
        ),
        (
            "private_zephyr_local_only",
            lambda: _mutate(
                _fixture_input(),
                message_text="Keep this proprietary Zephyr growth calculation local.",
                phase_a_signals={
                    "contains_raw_private_or_ip_sensitive_context": True,
                    "sensitivity_bucket_proposal": "sensitive",
                },
            ),
        ),
        (
            "clarification_needed",
            lambda: _mutate(
                _fixture_input(),
                phase_b_soft_proposal={"soft_reason_code": "clarification_context"},
            ),
        ),
        (
            "unknown_external_pressure",
            lambda: _mutate(
                _enable_external_policy(_fixture_input()),
                phase_a_signals={"sensitivity_bucket_proposal": "unknown"},
                action_hint={"needs_provider_call": True},
            ),
        ),
        (
            "external_candidate_proposal",
            _high_complexity_public_input,
        ),
        (
            "cheap_external_policy_falls_back_without_execution",
            lambda: _mutate(
                _high_complexity_public_input(),
                provider_policy={
                    "allowed_provider_tiers": ["LOCAL_ONLY", "LOCAL_FAST", "CHEAP_EXTERNAL"],
                    "blocked_provider_tiers": ["SCIENTIFIC_MEDIUM", "FRONTIER"],
                },
                budget_policy={"max_tier": "CHEAP_EXTERNAL"},
            ),
        ),
        (
            "simple_public_local_task",
            lambda: _mutate(
                _fixture_input(),
                router_hint={"task_type": "general_question", "complexity": "low"},
            ),
        ),
        (
            "default_fallback",
            _fixture_input,
        ),
        (
            "italian_zephyr_sensitive_external_wording",
            lambda: _mutate(
                _fixture_input(),
                message_text="Analizza questi dati proprietari Zephyr e mandali a un provider esterno.",
                phase_a_signals={
                    "contains_raw_private_or_ip_sensitive_context": True,
                    "mentions_external_provider_or_upload_intent": True,
                    "sensitivity_bucket_proposal": "sensitive",
                },
            ),
        ),
    ],
)
def test_promoted_router_decision_passes_runtime_invariants(case_name: str, input_builder) -> None:
    backend_decision = importlib.import_module("app.modules.ai.routing.decision")
    input_obj = input_builder()

    promoted_result = backend_decision.decide_router_policy(copy.deepcopy(input_obj), now=NOW)

    invariant = validate_router_decision_for_runtime(_with_sensitivity(promoted_result, input_obj))
    assert invariant.ok, invariant.errors


def test_runtime_invariants_fail_closed_for_invalid_synthetic_decisions() -> None:
    invalid_cases = [
        {"route_action": "blocked", "allowed_execution_mode": "blocked"},
        {
            "route_action": "blocked",
            "allowed_execution_mode": "blocked",
            "provider_call_allowed_now": True,
            "external_network_allowed_now": False,
            "state_change_allowed_now": False,
        },
        {
            "route_action": "ask_clarification",
            "allowed_execution_mode": "propose_only",
            "provider_call_allowed_now": True,
            "external_network_allowed_now": False,
            "state_change_allowed_now": False,
        },
        {
            "route_action": "route_local",
            "allowed_execution_mode": "propose_only",
            "provider_call_allowed_now": False,
            "external_network_allowed_now": True,
            "state_change_allowed_now": False,
            "sensitivity_bucket": "secret",
        },
        {
            "route_action": "ask_user_confirm",
            "allowed_execution_mode": "propose_only",
            "provider_call_allowed_now": False,
            "external_network_allowed_now": True,
            "state_change_allowed_now": False,
            "proposed_external_target": "external:scientific_medium",
        },
    ]

    for decision in invalid_cases:
        result = validate_router_decision_for_runtime(decision)
        assert result.ok is False
        assert result.errors


def test_routing_decision_import_has_no_runtime_side_effect_imports(monkeypatch) -> None:
    forbidden_modules = {
        "app.main",
        "app.modules.ai.providers.local_ollama_adapter",
        "app.modules.local_ai.runtime.lifecycle",
        "router_policy_semantic_validator",
        "scripts.router_policy_semantic_validator",
    }
    for module_name in {
        "app.modules.ai.routing.decision",
        "router_policy_semantic_validator",
        "scripts.router_policy_semantic_validator",
    }:
        sys.modules.pop(module_name, None)

    before = set(sys.modules)
    importlib.import_module("app.modules.ai.routing.decision")
    newly_imported = set(sys.modules) - before

    assert forbidden_modules.isdisjoint(newly_imported)
    assert "router_policy_semantic_validator" not in sys.modules
    assert "scripts.router_policy_semantic_validator" not in sys.modules


def test_routing_decision_source_has_no_network_subprocess_or_provider_imports() -> None:
    backend_decision = importlib.import_module("app.modules.ai.routing.decision")
    source = inspect.getsource(backend_decision)

    forbidden_tokens = (
        "subprocess",
        "httpx",
        "requests",
        "urllib.request",
        "FastAPI",
        "local_ollama_adapter",
        "router_policy_semantic_validator",
    )
    for token in forbidden_tokens:
        assert token not in source


def _mutate(input_obj: dict[str, Any], **updates: Any) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(input_obj.get(key), dict):
            input_obj[key].update(value)
        else:
            input_obj[key] = value
    return input_obj
