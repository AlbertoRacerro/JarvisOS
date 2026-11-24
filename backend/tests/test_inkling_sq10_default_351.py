from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.modules.ai.contracts import AIRequest, AITaskType
from app.modules.ai.providers import local_llamacpp_adapter as adapter_module
from app.modules.ai.providers.local_llamacpp_adapter import LocalLlamaCppAdapter
from app.modules.local_ai.runtime import llama_cpp, model_registry
from app.modules.local_ai.runtime.llama_cpp import LlamaCppRuntimeConfig
from app.modules.local_ai.runtime.model_registry import (
    ModelEntry,
    read_registry,
)


def _setup_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models: list[dict[str, Any]]) -> Path:
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    registry_file = settings_dir / "local_models.json"
    choice_file = settings_dir / "local_model_choice.json"
    registry_file.write_text(json.dumps({"models": models}), encoding="utf-8")
    paths = SimpleNamespace(
        data_root=tmp_path,
        local_models_file=registry_file,
        local_model_choice_file=choice_file,
    )
    monkeypatch.setattr(model_registry, "build_paths", lambda: paths)
    monkeypatch.setattr(llama_cpp, "build_paths", lambda: paths)
    return registry_file


def _inkling_sq10_entries(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "id": "inkling-sq10",
            "label": "Inkling 2 Mini SQ-10",
            "runtime": "strata",
            "qualification": "default",
            "unit": "inkling.service",
            "port": 8080,
            "served_model": "inkling-2-mini-sq10",
            "temperature": 1.0,
            "top_p": 1.0,
            "top_k": 0,
            "min_p": 0.0,
            "ctx_size": 32768,
            "reasoning_effort_analysis": "high",
            "reasoning_effort_final": "low",
        },
        {
            "id": "gemma",
            "label": "Gemma",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(tmp_path / "gemma.gguf"),
            "model_sha256": "a" * 64,
        },
    ]


# -----------------------------------------------------------------------------
# Acceptance Criterion 1: Registry schema extension and validation
# -----------------------------------------------------------------------------


def test_registry_inkling_sq10_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _inkling_sq10_entries(tmp_path)
    _setup_registry(tmp_path, monkeypatch, entries)
    models, present = read_registry()
    assert present is True
    assert len(models) == 2
    inkling = models[0]
    assert inkling.id == "inkling-sq10"
    assert inkling.runtime == "strata"
    assert inkling.qualification == "default"
    assert inkling.temperature == 1.0
    assert inkling.top_p == 1.0
    assert inkling.top_k == 0
    assert inkling.min_p == 0.0
    assert inkling.ctx_size == 32768
    assert inkling.reasoning_effort_analysis == "high"
    assert inkling.reasoning_effort_final == "low"

    gemma = models[1]
    assert gemma.id == "gemma"
    assert gemma.temperature is None
    assert gemma.top_p is None
    assert gemma.top_k is None
    assert gemma.min_p is None
    assert gemma.reasoning_effort_analysis is None
    assert gemma.reasoning_effort_final is None


def test_registry_omitted_sampling_fields_backward_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = [
        {
            "id": "legacy-strata",
            "label": "Legacy Strata",
            "runtime": "strata",
            "qualification": "default",
            "unit": "legacy.service",
            "port": 8080,
            "served_model": "legacy-model",
        }
    ]
    _setup_registry(tmp_path, monkeypatch, legacy)
    models, present = read_registry()
    assert present is True
    entry = models[0]
    assert entry.temperature is None
    assert entry.top_p is None
    assert entry.top_k is None
    assert entry.min_p is None
    assert entry.reasoning_effort_analysis is None
    assert entry.reasoning_effort_final is None
    assert entry.ctx_size is None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        # temperature: finite float/int (not bool), 0.0 <= v <= 2.0
        ("temperature", True),
        ("temperature", False),
        ("temperature", -0.01),
        ("temperature", 2.01),
        ("temperature", "1.0"),
        ("temperature", float("inf")),
        ("temperature", float("nan")),
        # top_p: finite float/int (not bool), 0.0 < v <= 1.0
        ("top_p", True),
        ("top_p", 0.0),
        ("top_p", -0.1),
        ("top_p", 1.05),
        ("top_p", "0.9"),
        ("top_p", float("inf")),
        # top_k: int (not bool), v >= 0
        ("top_k", True),
        ("top_k", False),
        ("top_k", -1),
        ("top_k", 5.5),
        ("top_k", "10"),
        # min_p: finite float/int (not bool), 0.0 <= v <= 1.0
        ("min_p", True),
        ("min_p", False),
        ("min_p", -0.01),
        ("min_p", 1.01),
        ("min_p", "0.05"),
        ("min_p", float("nan")),
        # reasoning_effort_analysis: "low", "medium", or "high"
        ("reasoning_effort_analysis", "extreme"),
        ("reasoning_effort_analysis", "LOW"),
        ("reasoning_effort_analysis", ""),
        ("reasoning_effort_analysis", 1),
        ("reasoning_effort_analysis", True),
        # reasoning_effort_final: "low", "medium", or "high"
        ("reasoning_effort_final", "fast"),
        ("reasoning_effort_final", 42),
        ("reasoning_effort_final", False),
        # ctx_size on strata: positive int (not bool)
        ("ctx_size", True),
        ("ctx_size", 0),
        ("ctx_size", -1024),
        ("ctx_size", 32768.5),
        ("ctx_size", "32768"),
    ],
)
def test_registry_sampling_validation_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, bad_value: Any
) -> None:
    entries = _inkling_sq10_entries(tmp_path)
    entries[0][field] = bad_value
    _setup_registry(tmp_path, monkeypatch, entries)
    with pytest.raises(HTTPException) as exc:
        read_registry()
    assert exc.value.status_code == 503


# -----------------------------------------------------------------------------
# Acceptance Criteria 2 & 3: Adapter payload wiring and reasoning effort
# -----------------------------------------------------------------------------


def _config(**overrides: Any) -> LlamaCppRuntimeConfig:
    values: dict[str, Any] = {
        "binary_path": "/fake/llama-server",
        "model_path": "/fake/model.gguf",
        "host": "127.0.0.1",
        "port": 8080,
        "ctx_size": 4096,
        "n_gpu_layers": 12,
    }
    values.update(overrides)
    return LlamaCppRuntimeConfig(**values)


def _mock_adapter(captured_payloads: list[dict[str, Any]]) -> LocalLlamaCppAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "ok", "reasoning_content": "thoughts"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    transport = httpx.MockTransport(handler)
    return LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport))


def test_adapter_emits_sampling_parameters_from_active_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = ModelEntry(
        id="inkling-sq10",
        label="Inkling 2 Mini SQ-10",
        runtime="strata",
        qualification="default",
        unit="inkling.service",
        port=8080,
        served_model="inkling-2-mini-sq10",
        temperature=1.0,
        top_p=1.0,
        top_k=0,
        min_p=0.0,
        ctx_size=32768,
        reasoning_effort_analysis="high",
        reasoning_effort_final="low",
    )
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(thinking="default"))
    monkeypatch.setattr(
        "app.modules.local_ai.runtime.model_registry.get_active_model_entry", lambda: entry
    )

    captured: list[dict[str, Any]] = []
    adapter = _mock_adapter(captured)

    # 1. request.temperature is None -> uses active_entry.temperature (1.0)
    req = AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    res = adapter.complete(req)
    assert res.finish_reason == "stop"
    assert len(captured) == 1
    payload = captured[0]
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 1.0
    assert payload["top_k"] == 0
    assert payload["min_p"] == 0.0

    # 2. explicit request.temperature overrides active_entry
    captured.clear()
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hello", temperature=0.7))
    assert captured[0]["temperature"] == 0.7

    # 3. explicit request.temperature=0.0 preserved
    captured.clear()
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hello", temperature=0.0))
    assert captured[0]["temperature"] == 0.0


def test_adapter_omits_unconfigured_sampling_keys_for_legacy_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_entry = ModelEntry(
        id="legacy-gemma",
        label="Legacy Gemma",
        runtime="llama_cpp",
        qualification="default",
        model_path="/models/gemma.gguf",
        model_sha256="a" * 64,
    )
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(thinking="default"))
    monkeypatch.setattr(
        "app.modules.local_ai.runtime.model_registry.get_active_model_entry", lambda: legacy_entry
    )

    captured: list[dict[str, Any]] = []
    adapter = _mock_adapter(captured)

    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hello"))
    assert len(captured) == 1
    payload = captured[0]
    assert payload["temperature"] == 0
    assert "top_p" not in payload
    assert "top_k" not in payload
    assert "min_p" not in payload
    assert "chat_template_kwargs" not in payload


def test_adapter_reasoning_effort_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = ModelEntry(
        id="inkling-sq10",
        label="Inkling 2 Mini SQ-10",
        runtime="strata",
        qualification="default",
        unit="inkling.service",
        port=8080,
        served_model="inkling-2-mini-sq10",
        temperature=1.0,
        top_p=1.0,
        top_k=0,
        min_p=0.0,
        reasoning_effort_analysis="high",
        reasoning_effort_final="low",
    )
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(thinking="default"))
    monkeypatch.setattr(
        "app.modules.local_ai.runtime.model_registry.get_active_model_entry", lambda: entry
    )

    captured: list[dict[str, Any]] = []
    adapter = _mock_adapter(captured)

    # Analysis turn: defaults to reasoning_effort_analysis ("high")
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="analyze this code"))
    assert captured[-1]["chat_template_kwargs"]["reasoning_effort"] == "high"

    # Final answer turn indicated by metadata reasoning_phase == "final"
    captured.clear()
    adapter.complete(
        AIRequest(
            task_type=AITaskType.synthesis,
            prompt="give final answer",
            metadata={"reasoning_phase": "final"},
        )
    )
    assert captured[-1]["chat_template_kwargs"]["reasoning_effort"] == "low"

    # Simple routing turns: task_type in simple-routing tasks
    for route_task in (AITaskType.smoke_test, AITaskType.smoke_console_test):
        captured.clear()
        adapter.complete(AIRequest(task_type=route_task, prompt="quick check"))
        assert captured[-1]["chat_template_kwargs"]["reasoning_effort"] == "low"


def test_adapter_preserves_enable_thinking_alongside_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ModelEntry(
        id="inkling-sq10",
        label="Inkling 2 Mini SQ-10",
        runtime="strata",
        qualification="default",
        unit="inkling.service",
        port=8080,
        served_model="inkling-2-mini-sq10",
        reasoning_effort_analysis="high",
        reasoning_effort_final="low",
    )
    monkeypatch.setattr(
        "app.modules.local_ai.runtime.model_registry.get_active_model_entry", lambda: entry
    )

    # Thinking = "on"
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(thinking="on"))
    captured: list[dict[str, Any]] = []
    adapter = _mock_adapter(captured)
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="analyze"))
    assert captured[0]["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_effort": "high",
    }

    # Thinking = "off"
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(thinking="off"))
    captured.clear()
    adapter.complete(
        AIRequest(
            task_type=AITaskType.synthesis,
            prompt="final answer",
            metadata={"reasoning_phase": "final"},
        )
    )
    assert captured[0]["chat_template_kwargs"] == {
        "enable_thinking": False,
        "reasoning_effort": "low",
    }
