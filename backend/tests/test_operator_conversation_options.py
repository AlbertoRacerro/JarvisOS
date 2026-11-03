from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.ai import provider_registry
from app.modules.ai.thread_models import AIConversationRouteAvailability
from app.modules.ai.thread_routes import _hermes_agent_availability, router


@pytest.mark.parametrize(
    ("status", "reachable", "installed", "loaded", "reason"),
    [
        ({"ollama_reachable": False}, False, None, None, "RUNTIME_UNREACHABLE"),
        ({"ollama_reachable": True, "installed_models": [], "loaded_models": [], "configured_route_models": {"local:general": "gemma4:12b-it-qat"}}, True, False, False, "MODEL_NOT_INSTALLED"),
        ({"ollama_reachable": True, "installed_models": ["gemma4:12b-it-qat"], "loaded_models": [], "configured_route_models": {"local:general": "gemma4:12b-it-qat"}}, True, True, False, None),
        ({"ollama_reachable": True, "installed_models": ["gemma4:12b-it-qat"], "loaded_models": [{"name": "gemma4:12b-it-qat"}], "configured_route_models": {"local:general": "gemma4:12b-it-qat"}}, True, True, True, None),
    ],
)
def test_conversation_options_report_truthful_ollama_availability(monkeypatch, status, reachable, installed, loaded, reason):
    monkeypatch.setattr("app.modules.local_ai.runtime.status.get_local_ai_runtime_status", lambda: status)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get('/threads/conversation-options')
    route = next(route for route in response.json()['routes'] if route['route_class'] == 'local:general')
    state = route['availability']
    assert state['runtime_reachable'] is reachable
    assert state['model_installed'] is installed
    assert state['model_loaded'] is loaded
    assert state['qualified'] == 'unknown'
    assert state['reason_code'] == reason


def test_local_conversation_options_are_read_only_configuration_not_health():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get('/threads/conversation-options')
    assert response.status_code == 200
    data = response.json()
    assert data['availability'] == 'configured'
    general = next(route for route in data['routes'] if route['route_class'] == 'local:general')
    assert {'configured', 'runtime_reachable', 'model_installed', 'model_loaded', 'qualified', 'reason_code', 'message'} <= set(general['availability'])
    assert general['availability']['qualified'] == 'unknown'
    synthetic = next(route for route in data['routes'] if route['route_class'] == 'local:fake')
    assert synthetic['execution_class'] == 'synthetic'
    agent = next(route for route in data['routes'] if route['route_class'] == 'hermes:agent')
    assert agent['execution_class'] == 'agent'
    assert agent['label'] == 'Jarvis agent (Hermes)'
    assert all(route['execution_class'] in {'local_compute', 'synthetic', 'agent'} for route in data['routes'])
    assert 'api_key' not in response.text and 'base_url' not in response.text


def test_conversation_options_never_offer_a_network_or_external_binding(monkeypatch):
    bindings = provider_registry.registry_bindings()
    bindings['local:general'] = replace(bindings['local:general'], requires_network=True)
    bindings['local:coder'] = replace(bindings['local:coder'], execution_class='external_provider')
    monkeypatch.setattr(provider_registry, 'registry_bindings', lambda: bindings)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get('/threads/conversation-options')
    assert [route['route_class'] for route in response.json()['routes']] == [
        'local:fast', 'local:llamacpp', 'local:fake', 'hermes:agent'
    ]


def test_hermes_availability_reports_prerequisite_reason_without_starting_worker(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_HERMES_VENV", raising=False)
    unavailable = _hermes_agent_availability(None)
    assert unavailable.reason_code == "HERMES_VENV_MISSING"
    assert unavailable.runtime_reachable is False

    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.touch()
    monkeypatch.setenv("JARVIS_HERMES_VENV", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    monkeypatch.setattr("app.modules.ai.thread_routes._hermes_revision_qualified", lambda *_args: True)
    monkeypatch.setattr(
        "app.modules.ai.thread_routes._llamacpp_route_availability",
        lambda *_args: AIConversationRouteAvailability(
            configured=True, runtime_reachable=True, model_installed=True, model_loaded=False,
            qualified="unknown", message="ready",
        ),
    )
    ready = _hermes_agent_availability(None)
    assert ready.reason_code is None and ready.runtime_reachable is True
    assert ready.model_loaded is False

    lost_pool = SimpleNamespace(status=lambda: {"state": "stopped", "threads": {
        "thread-a": {"state": "stopped", "last_error": "worker_lost"}}})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(_state={"hermes_supervisor": lost_pool})))
    lost = _hermes_agent_availability(request)
    assert lost.reason_code == "HERMES_WORKER_LOST"
    assert lost.runtime_reachable is True  # the next turn recovers the session
