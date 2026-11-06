from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.modules.ai.contracts import AIProviderHealth, AIRequest, AITaskType
from app.modules.ai.providers import local_llamacpp_adapter
from app.modules.local_ai.runtime import model_registry
from app.modules.local_ai.runtime.llama_cpp import LlamaCppRuntimeConfig, LlamaCppRuntimeOwner


def _entries(tmp_path: Path) -> list[dict[str, object]]:
    return [
        {"id": "default", "label": "Default", "runtime": "llama_cpp", "qualification": "default", "model_path": str(tmp_path / "default.gguf"), "model_sha256": "a" * 64},
        {"id": "candidate", "label": "Candidate", "runtime": "llama_cpp", "qualification": "qualified", "model_path": str(tmp_path / "candidate.gguf"), "model_sha256": "b" * 64, "ctx_size": 2048, "n_gpu_layers": 3, "extra_args": ["--foo"], "thinking": "off"},
        {"id": "strata", "label": "Strata", "runtime": "strata", "qualification": "under_qualification", "model_path": str(tmp_path / "strata.gguf"), "model_sha256": "c" * 64},
    ]


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, object]]) -> None:
    registry = tmp_path / "local_models.json"
    registry.write_text(json.dumps({"models": entries}), encoding="utf-8")
    monkeypatch.setattr(model_registry, "build_paths", lambda: SimpleNamespace(local_models_file=registry))


def test_registry_read_only_projection_and_invalid_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _entries(tmp_path)
    _registry(tmp_path, monkeypatch, entries)
    (tmp_path / "default.gguf").write_bytes(b"default")
    (tmp_path / "candidate.gguf").write_bytes(b"candidate")
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: SimpleNamespace(active_model_id=None))
    result = model_registry.list_models()
    assert result["active_id"] == "default"
    assert [row["qualification"] for row in result["models"]] == ["default", "qualified", "under_qualification"]
    assert result["models"][0]["model_file"] == "default.gguf"
    assert str(tmp_path) not in str(result)
    assert result["models"][2]["digest_state"] == "missing"
    for bad in ([], entries[1:], [entries[0], entries[0]], [{**entries[0], "model_sha256": "bad"}], "invalid"):
        (tmp_path / "local_models.json").write_text(json.dumps({"models": bad}), encoding="utf-8")
        with pytest.raises(HTTPException) as exc:
            model_registry.read_registry()
        assert exc.value.status_code == 503
    (tmp_path / "local_models.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(HTTPException):
        model_registry.read_registry()
    (tmp_path / "local_models.json").unlink()
    assert model_registry.list_models() == {"registry_present": False, "models": [], "active_id": None}


def test_refusals_leave_running_owner_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _entries(tmp_path)
    _registry(tmp_path, monkeypatch, entries)
    (tmp_path / "candidate.gguf").write_bytes(b"candidate")
    owner = SimpleNamespace(switch_model=lambda *_: pytest.fail("process touched"))
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    for identifier in ("unknown", "default", "strata"):
        with pytest.raises(HTTPException):
            model_registry.activate_model(identifier)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda _: ("0" * 64, False))
    with pytest.raises(HTTPException, match="digest"):
        model_registry.activate_model("candidate")


class _Process:
    pid = 101
    returncode = None
    def poll(self):
        return None
    def wait(self, timeout):
        return 0
    def kill(self):
        pass


def test_switch_stops_before_start_and_restores_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _entries(tmp_path)
    _registry(tmp_path, monkeypatch, entries)
    for name in ("default.gguf", "candidate.gguf"):
        (tmp_path / name).write_bytes(b"fixture")
    binary = tmp_path / "server"
    binary.write_bytes(b"fixture")
    events = []
    shared = LlamaCppRuntimeConfig(binary_path=str(binary), model_path=str(tmp_path / "default.gguf"), startup_wait_s=0)
    owner = LlamaCppRuntimeOwner(
        shared,
        popen=lambda args, **_: (events.append(("start", args)) or _Process()),
        client_factory=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)), **kwargs),
        kill_group=lambda *_: events.append(("stop", None)),
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: shared)
    owner.start()
    model_registry.activate_model("candidate")
    assert [event[0] for event in events] == ["start", "stop", "start"]
    assert owner.config.model_id == "candidate"
    assert owner.config.ctx_size == 2048 and owner.config.n_gpu_layers == 3
    assert owner.config.extra_args == ("--foo",) and owner.config.thinking == "off"
    assert owner.non_default_active
    assert owner.status()["model_id"] == "candidate"
    model_registry.restore_default()
    assert [event[0] for event in events] == ["start", "stop", "start", "stop", "start"]
    assert owner.active_model_id == "default" and not owner.non_default_active
    assert LlamaCppRuntimeOwner(owner.config).active_model_id is None


def test_product_adapter_refuses_without_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_llamacpp_adapter, "get_llama_cpp_runtime_owner", lambda: SimpleNamespace(non_default_active=True))
    monkeypatch.setattr(local_llamacpp_adapter, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(model_id="default"))
    adapter = local_llamacpp_adapter.LocalLlamaCppAdapter(client_factory=lambda: pytest.fail("HTTP client created"))
    assert adapter.health() == AIProviderHealth.unavailable
    result = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hello"))
    assert result.error is not None and result.error.code.value == "provider_unavailable"


def test_availability_hides_non_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai import thread_routes
    owner = SimpleNamespace(non_default_active=True, status=lambda: {
        "configured": True, "runtime_reachable": True, "model_installed": True,
        "model_loaded": True, "reason_code": None, "message": "ready",
    })
    monkeypatch.setattr("app.modules.local_ai.runtime.llama_cpp.get_llama_cpp_runtime_owner", lambda: owner)
    availability = thread_routes._llamacpp_route_availability("local:llamacpp", "default")
    assert availability.model_loaded is False
    assert availability.reason_code == "LLAMACPP_DEFAULT_NOT_LOADED"


def test_model_routes_accept_id_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.local_ai.runtime import routes

    _registry(tmp_path, monkeypatch, _entries(tmp_path))
    (tmp_path / "default.gguf").write_bytes(b"fixture")
    owner = SimpleNamespace(active_model_id="default", status=lambda: {"runtime_reachable": False},
                            switch_model=lambda config, model_id, default_id: {"model_id": model_id})
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", LlamaCppRuntimeConfig)
    paths = {route.path for route in routes.models_router.routes}
    assert paths == {"/local-ai/models", "/local-ai/models/restore-default", "/local-ai/models/{model_id}/activate"}
    assert routes.read_models()["active_id"] == "default"
    assert routes.activate_registered_model("default") == {"model_id": "default"}
    with pytest.raises(HTTPException):
        routes.activate_registered_model("unknown")


def test_backend_owner_reset_selects_registry_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.local_ai.runtime import llama_cpp

    entries = _entries(tmp_path)
    _registry(tmp_path, monkeypatch, entries)
    monkeypatch.setattr(llama_cpp, "_OWNER", None)
    fresh = llama_cpp.get_llama_cpp_runtime_owner()
    assert fresh.active_model_id == "default"
    assert fresh.default_model_id == "default"
    # The default keeps the shared llama_cpp.json alias that product calls and job records use.
    assert fresh.config.model_id == llama_cpp.llama_cpp_runtime_config().model_id
    assert fresh.config.model_path == str(tmp_path / "default.gguf")
