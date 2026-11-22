"""Offline unit tests for free models and prepaid balances in provider quota (spec 346)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.modules.coding import provider_quota as pq

NOW = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)


def test_free_models_and_prepaid_balances_fallback_when_files_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty_control = tmp_path / "empty_control"
    empty_control.mkdir()
    monkeypatch.setenv("TEMP_POOLS_FILE", str(empty_control / "nonexistent-temp-pools.json"))
    monkeypatch.setenv("PREPAID_BALANCES_FILE", str(empty_control / "nonexistent-prepaid.json"))

    assert pq.read_free_models(empty_control) == []
    assert pq.read_prepaid_balances(empty_control) == []

    payload = pq.normalize({}, NOW)
    assert payload["free_models"] == []
    assert payload["prepaid_balances"] == []


def test_free_models_and_prepaid_balances_fallback_when_corrupted_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pools = tmp_path / "temp-pools.json"
    pools.write_text("invalid json {", encoding="utf-8")
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools))

    prepaid = tmp_path / "prepaid-balances.json"
    prepaid.write_text("not json [", encoding="utf-8")
    monkeypatch.setenv("PREPAID_BALANCES_FILE", str(prepaid))

    assert pq.read_free_models(tmp_path) == []
    assert pq.read_prepaid_balances(tmp_path) == []


def test_free_models_parsing_and_secret_path_stripping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_key = tmp_path / "secret.key"
    secret_key.write_text("SUPER_SECRET_TOKEN_DO_NOT_LEAK", encoding="utf-8")

    pools_data = {
        "reference": "luna",
        "scores": {"luna": 28, "sol": 40},
        "pools": [
            {
                "name": "high-pool",
                "label": "High Tier Model",
                "provider": "Nous",
                "model": "nousresearch/hermes-3-llama-3.1-8b",
                "key_file": str(secret_key),
                "api_key": "sk-live-1234567890abcdef1234567890",
                "token": "tok_xyz987654321",
                "base_url": "https://api.nous.invalid/v1",
                "index_score": 42,
                "remaining_percent": 85.4,
                "status": "active",
            },
            {
                "name": "zen-pool",
                "label": "OpenCode Zen Pool",
                "provider": "OpenCode Zen",
                "model": "zen-free",
                "index_score": 35,
                "limit_kind": "live_dot",
                "status": "active",
            },
            {
                "name": "exhausted-pool",
                "label": "Exhausted Model",
                "provider": "OpenRouter",
                "model": "deepseek/deepseek-chat:free",
                "index_score": 30,
                "remaining_percent": 0.0,
                "exhausted": True,
            },
            {
                "name": "unqualified-pool",
                "label": "Low Score Model",
                "provider": "Public",
                "model": "low-model",
                "index_score": 20,
            },
        ],
    }

    pools_file = tmp_path / "temp-pools.json"
    pools_file.write_text(json.dumps(pools_data), encoding="utf-8")
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    lists_file = tmp_path / "temp-pool-lists.json"
    lists_file.write_text(
        json.dumps({
            "bridge": {
                "zen-pool": {"state": "throttled", "at": "2026-11-22T00:55:00Z"},
            }
        }),
        encoding="utf-8",
    )

    models = pq.read_free_models(tmp_path)
    assert len(models) == 4

    # 1. High pool
    m1 = models[0]
    assert m1["id"] == "high-pool"
    assert m1["label"] == "High Tier Model"
    assert m1["source"] == "Nous"
    assert m1["model_id"] == "nousresearch/hermes-3-llama-3.1-8b"
    assert m1["intelligence_index"] == 42
    assert m1["status"] == "active"
    assert m1["limit_kind"] == "ring"
    assert m1["remaining_percent"] == 85.4
    assert m1["exhausted"] is False

    # Verify strictly no secrets / keys / host paths in the dict
    for forbidden in ("key_file", "api_key", "token", "base_url", "secret.key"):
        assert forbidden not in m1
    serialized = json.dumps(m1)
    assert "SUPER_SECRET_TOKEN" not in serialized
    assert "sk-live-123456" not in serialized
    assert str(tmp_path) not in serialized

    # 2. Zen pool (throttled by bridge state, live_dot limit kind)
    m2 = models[1]
    assert m2["id"] == "zen-pool"
    assert m2["status"] == "throttled"
    assert m2["limit_kind"] == "live_dot"
    assert m2["remaining_percent"] is None
    assert m2["exhausted"] is False

    # 3. Exhausted pool
    m3 = models[2]
    assert m3["id"] == "exhausted-pool"
    assert m3["status"] == "exhausted"
    assert m3["remaining_percent"] == 0.0
    assert m3["exhausted"] is True

    # 4. Unqualified pool (index_score 20 < reference score 28)
    m4 = models[3]
    assert m4["id"] == "unqualified-pool"
    assert m4["status"] == "unqualified"
    assert m4["intelligence_index"] == 20


def test_prepaid_balances_parsing_and_secret_path_stripping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    balances_data = {
        "balances": [
            {
                "provider": "deepseek",
                "label": "DeepSeek Prepaid",
                "balance_usd": 14.50,
                "api_key": "sk-test-secret-1234abcd",
                "key_file": str(tmp_path / "deepseek.key"),
                "status": "ok",
            },
            {
                "provider": "zai",
                "label": "Z.ai Balance",
                "spent_eur": 0.0,
                "cap_eur": 5.0,
                "status": "ok",
            },
            {
                "provider": "gemini_paid",
                "label": "Gemini Paid",
                "spent_eur": 1.25,
                "cap_eur": 3.0,
                "status": "unavailable",
                "reason": "Quota a pagamento disabilitata",
            },
        ]
    }

    balances_file = tmp_path / "prepaid-balances.json"
    balances_file.write_text(json.dumps(balances_data), encoding="utf-8")
    monkeypatch.setenv("PREPAID_BALANCES_FILE", str(balances_file))

    balances = pq.read_prepaid_balances(tmp_path)
    assert len(balances) == 3

    b1 = balances[0]
    assert b1["provider"] == "deepseek"
    assert b1["label"] == "DeepSeek Prepaid"
    assert b1["mode"] == "read_only"
    assert b1["balance_usd"] == 14.50
    assert b1["spent_eur"] is None
    assert b1["cap_eur"] is None
    assert b1["status"] == "ok"
    assert b1["reason"] is None

    # Verify no secrets or host paths
    for forbidden in ("api_key", "key_file", ".secrets"):
        assert forbidden not in b1
    serialized = json.dumps(b1)
    assert "sk-test-secret" not in serialized
    assert str(tmp_path) not in serialized

    b2 = balances[1]
    assert b2["provider"] == "zai"
    assert b2["spent_eur"] == 0.0
    assert b2["cap_eur"] == 5.0
    assert b2["status"] == "ok"

    b3 = balances[2]
    assert b3["provider"] == "gemini_paid"
    assert b3["status"] == "unavailable"
    assert b3["reason"] == "Quota a pagamento disabilitata"


def test_normalize_payload_includes_free_models_and_prepaid_balances() -> None:
    mock_free = [
        {
            "id": "mock-free-1",
            "label": "Mock Free",
            "source": "OpenCode",
            "model_id": "mock/model",
            "intelligence_index": 50,
            "status": "active",
            "limit_kind": "ring",
            "remaining_percent": 90.0,
            "exhausted": False,
        }
    ]
    mock_prepaid = [
        {
            "provider": "mock-prepaid-1",
            "label": "Mock Prepaid",
            "mode": "read_only",
            "balance_usd": 10.0,
            "spent_eur": None,
            "cap_eur": None,
            "status": "ok",
            "reason": None,
        }
    ]

    readings: dict[str, Any] = {
        "free_models": mock_free,
        "prepaid_balances": mock_prepaid,
    }

    payload = pq.normalize(readings, NOW)
    assert "free_models" in payload
    assert "prepaid_balances" in payload
    assert payload["free_models"] == mock_free
    assert payload["prepaid_balances"] == mock_prepaid
    assert len(payload["providers"]) == 4
