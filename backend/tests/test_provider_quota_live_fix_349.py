"""Spec 349: Offline unit tests for Claude live refresh, Codex resilience, and live-verified free models."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.modules.coding import provider_quota as pq

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worker_routing.py"
SPEC = importlib.util.spec_from_file_location("worker_routing_under_test", SCRIPT)
assert SPEC and SPEC.loader
wr = importlib.util.module_from_spec(SPEC)
sys.modules["worker_routing_under_test"] = wr
SPEC.loader.exec_module(wr)

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sandbox_home = tmp_path / "sandbox_home"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(sandbox_home))
    monkeypatch.delenv("JARVISOS_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("JARVISOS_CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("JARVISOS_WORKER_QUOTA_FILE", str(sandbox_home / "nonexistent-quota.json"))


# ============================================================================
# Criteria 1: Claude Live Refresh
# ============================================================================


def test_claude_refresh_when_token_expired_and_refresh_token_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude_state"
    config_dir.mkdir(parents=True, exist_ok=True)
    cred_file = config_dir / ".credentials.json"
    expired_epoch = int((NOW - timedelta(days=5)).timestamp())
    cred_file.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired-access-token-1234567890",
                "refreshToken": "valid-refresh-token-0987654321",
                "expiresAt": expired_epoch,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_CLAUDE_CONFIG_DIR", str(config_dir))

    relay_calls: list[list[str]] = []
    relay_envs: list[dict[str, str]] = []

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        relay_calls.append(cmd)
        env = kwargs.get("env") or {}
        relay_envs.append(env)
        # Verify CLAUDE_CONFIG_DIR is set to credentials parent directory
        assert env.get("CLAUDE_CONFIG_DIR") == str(config_dir)
        payload = {
            "agents": [
                {
                    "agent_key": "claude",
                    "status": "ok",
                    "as_of": "2026-11-24T10:00:00Z",
                    "windows": [
                        {"id": "5h", "label": "5-hour limit", "used_percent": 15.0, "resets_at": "2026-11-24T15:00:00Z"},
                        {"id": "weekly", "label": "Weekly limit", "used_percent": 30.0, "resets_at": "2026-11-30T00:00:00Z"},
                    ],
                }
            ]
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    res, err = pq.read_claude_limits(refresh=True)
    assert err is None
    assert isinstance(res, dict)
    assert res["status"] == "ok"
    assert len(res["windows"]) == 2
    assert res["windows"][0]["used_percent"] == 15.0

    # Ensure agent-relay was invoked with refresh flag and config dir
    assert len(relay_calls) == 1
    assert "--refresh" in relay_calls[0]
    assert relay_envs[0]["CLAUDE_CONFIG_DIR"] == str(config_dir)

    # Check zero secret leakage in returned data
    serialized = json.dumps(res)
    assert "access-token" not in serialized
    assert "refresh-token" not in serialized


def test_claude_refresh_updates_disk_and_retries_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude_state"
    config_dir.mkdir(parents=True, exist_ok=True)
    cred_file = config_dir / ".credentials.json"
    expired_epoch = int((NOW - timedelta(days=2)).timestamp())
    cred_file.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-expired-token",
                "refreshToken": "my-valid-refresh-token",
                "expiresAt": expired_epoch,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_CLAUDE_CONFIG_DIR", str(config_dir))

    # agent-relay updates disk but prints no json
    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Simulate agent-relay writing updated credentials to disk
        future_epoch = int(time.time() + 36000)
        cred_file.write_text(
            json.dumps({
                "claudeAiOauth": {
                    "accessToken": "freshly-refreshed-access-token",
                    "refreshToken": "my-valid-refresh-token",
                    "expiresAt": future_epoch,
                }
            }),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="Refreshed credentials on disk", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    fetch_tokens: list[str] = []

    def fake_fetch_claude_usage(token: str, **kwargs: Any) -> tuple[dict[str, Any] | None, str | None]:
        fetch_tokens.append(token)
        return {
            "five_hour": {"utilization": 22.0, "resets_at": "2026-11-24T15:00:00Z"},
            "seven_day": {"utilization": 45.0, "resets_at": "2026-11-30T00:00:00Z"},
        }, None

    monkeypatch.setattr(pq, "_fetch_claude_usage", fake_fetch_claude_usage)

    res, err = pq.read_claude_limits(refresh=True)
    assert err is None
    assert isinstance(res, dict)
    assert res["status"] == "ok"
    assert len(res["windows"]) == 2
    assert fetch_tokens == ["freshly-refreshed-access-token"]

    serialized = json.dumps(res)
    assert "access-token" not in serialized
    assert "refresh-token" not in serialized


def test_claude_refresh_triggered_on_401_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude_state"
    config_dir.mkdir(parents=True, exist_ok=True)
    cred_file = config_dir / ".credentials.json"
    future_epoch = int(time.time() + 3600)
    cred_file.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "unexpired-but-revoked-token",
                "refreshToken": "refresh-token-valid",
                "expiresAt": future_epoch,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_CLAUDE_CONFIG_DIR", str(config_dir))

    call_count = [0]

    def fake_fetch(token: str, **kwargs: Any) -> tuple[dict[str, Any] | None, str | None]:
        call_count[0] += 1
        return None, "session_expired"

    monkeypatch.setattr(pq, "_fetch_claude_usage", fake_fetch)

    refreshed_called = [False]

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        refreshed_called[0] = True
        payload = {
            "agents": [
                {
                    "agent_key": "claude",
                    "status": "ok",
                    "as_of": "2026-11-24T10:00:00Z",
                    "windows": [{"id": "5h", "used_percent": 5.0, "resets_at": "2026-11-24T15:00:00Z"}],
                }
            ]
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    res, err = pq.read_claude_limits()
    assert err is None
    assert isinstance(res, dict)
    assert res["status"] == "ok"
    assert refreshed_called[0] is True


def test_claude_expired_without_refresh_token_fails_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude_state"
    config_dir.mkdir(parents=True, exist_ok=True)
    cred_file = config_dir / ".credentials.json"
    expired_epoch = int((NOW - timedelta(days=1)).timestamp())
    cred_file.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired-token",
                "expiresAt": expired_epoch,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_CLAUDE_CONFIG_DIR", str(config_dir))

    subprocess_called = [False]

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        subprocess_called[0] = True

    monkeypatch.setattr(subprocess, "run", fake_run)

    res, err = pq.read_claude_limits()
    assert err == "session_expired"
    assert res == {"status": "unavailable", "reason": "session_expired", "windows": []}
    assert subprocess_called[0] is False


def test_claude_refresh_failure_returns_session_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude_state"
    config_dir.mkdir(parents=True, exist_ok=True)
    cred_file = config_dir / ".credentials.json"
    expired_epoch = int((NOW - timedelta(days=1)).timestamp())
    cred_file.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired-token",
                "refreshToken": "invalid-refresh-token",
                "expiresAt": expired_epoch,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_CLAUDE_CONFIG_DIR", str(config_dir))

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Token refresh failed: invalid grant")

    monkeypatch.setattr(subprocess, "run", fake_run)

    res, err = pq.read_claude_limits(refresh=True)
    assert err == "session_expired"
    assert res == {"status": "unavailable", "reason": "session_expired", "windows": []}


# ============================================================================
# Criteria 2: Codex Reader Resilience
# ============================================================================


def test_codex_recovers_via_fallback_user_home_when_primary_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary_home = tmp_path / "primary_codex"
    primary_home.mkdir()
    user_home = tmp_path / "user_home"
    user_codex = user_home / ".codex"
    user_codex.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("JARVISOS_CODEX_HOME", str(primary_home))

    calls: list[str | None] = []

    def fake_query(codex_home: str | None) -> tuple[dict[str, Any] | None, str | None]:
        calls.append(codex_home)
        if codex_home == str(primary_home):
            return None, "no_json"
        if codex_home == str(user_codex):
            return {
                "status": "ok",
                "as_of": "2026-11-24T10:00:00Z",
                "windows": [{"id": "primary", "used_percent": 10.0, "resets_at": None}],
            }, None
        return None, "not_found"

    monkeypatch.setattr(pq, "_query_codex_app_server", fake_query)

    res, err = pq.read_codex_limits()
    assert err is None
    assert isinstance(res, dict)
    assert res["status"] == "ok"
    assert calls == [str(primary_home), str(user_codex)]


def test_codex_falls_back_to_fresh_supervisor_snapshot_preserving_as_of(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary_home = tmp_path / "primary_codex"
    primary_home.mkdir()
    monkeypatch.setenv("JARVISOS_CODEX_HOME", str(primary_home))

    # Both primary and fallback ~/.codex fail with auth_required
    monkeypatch.setattr(pq, "_query_codex_app_server", lambda _home: (None, "auth_required"))

    exact_as_of = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_path = tmp_path / "worker-quota.json"
    snapshot_path.write_text(
        json.dumps({
            "schema": "worker-quota.v1",
            "as_of": exact_as_of,
            "pools": {
                "codex": {
                    "as_of": exact_as_of,
                    "windows": {"5h": 0.85, "weekly": 0.60},
                }
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_WORKER_QUOTA_FILE", str(snapshot_path))

    res, err = pq.read_codex_limits()
    assert err is None
    assert isinstance(res, dict)
    assert res["status"] == "ok"
    assert res["as_of"] == exact_as_of
    assert len(res["windows"]) == 2
    # 0.85 free fraction = 15.0% used; 0.60 free fraction = 40.0% used
    w_map = {w["id"]: w["used_percent"] for w in res["windows"]}
    assert w_map["5h"] == 15.0
    assert w_map["weekly"] == 40.0


def test_codex_returns_accurate_reason_when_snapshot_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary_home = tmp_path / "primary_codex"
    primary_home.mkdir()
    monkeypatch.setenv("JARVISOS_CODEX_HOME", str(primary_home))
    monkeypatch.setattr(pq, "_query_codex_app_server", lambda _home: (None, "auth_required"))

    # Snapshot is 45 minutes old (stale: SNAPSHOT_MAX_AGE is 1800s / 30m)
    stale_as_of = (datetime.now(UTC) - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_path = tmp_path / "worker-quota.json"
    snapshot_path.write_text(
        json.dumps({
            "schema": "worker-quota.v1",
            "as_of": stale_as_of,
            "pools": {
                "codex": {
                    "as_of": stale_as_of,
                    "windows": {"5h": 0.90},
                }
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_WORKER_QUOTA_FILE", str(snapshot_path))

    res, err = pq.read_codex_limits()
    assert res is None
    assert err == "auth_required"


def test_codex_distinguishes_failure_reasons_accurately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty_snapshot = tmp_path / "empty-quota.json"
    monkeypatch.setenv("JARVISOS_WORKER_QUOTA_FILE", str(empty_snapshot))

    # 1. not_found
    monkeypatch.setattr(pq, "_query_codex_app_server", lambda _home: (None, "not_found"))
    res, err = pq.read_codex_limits()
    assert res is None and err == "not_found"

    # 2. timeout
    monkeypatch.setattr(pq, "_query_codex_app_server", lambda _home: (None, "timeout"))
    res, err = pq.read_codex_limits()
    assert res is None and err == "timeout"

    # 3. auth_required
    monkeypatch.setattr(pq, "_query_codex_app_server", lambda _home: (None, "auth_required"))
    res, err = pq.read_codex_limits()
    assert res is None and err == "auth_required"

    # 4. no_json
    monkeypatch.setattr(pq, "_query_codex_app_server", lambda _home: (None, "no_json"))
    res, err = pq.read_codex_limits()
    assert res is None and err == "no_json"


# ============================================================================
# Criteria 3: Shared Live Free-Model Rule (Panel + Router)
# ============================================================================


def _setup_free_pools_fixture(tmp_path: Path, pools: list[dict[str, Any]], lists_doc: dict[str, Any]) -> tuple[Path, Path]:
    pools_file = tmp_path / "temp-pools.json"
    pools_data = {
        "reference": "luna",
        "reference_score": 28,
        "coding_reference": "luna",
        "reference_coding_index": 35,
        "scores": {"luna": 28, "sol": 40},
        "coding_scores": {"luna": 35, "sol": 45},
        "pools": pools,
    }
    pools_file.write_text(json.dumps(pools_data), encoding="utf-8")

    lists_file = tmp_path / "temp-pool-lists.json"
    lists_file.write_text(json.dumps(lists_doc), encoding="utf-8")
    return pools_file, lists_file


def test_free_model_admitted_when_all_three_criteria_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    pool = {
        "name": "good-pool",
        "provider": "TestProvider",
        "model": "model/good:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 38,
        "coding_index": 42,
        "rate_limits": "free",
        "data_policy": "public",
        "pricing": {"prompt": 0, "completion": "0"},
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": ["model/good:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "model/good:free": {"status": "ok", "live_ok_at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # 1. Quota Panel (read_free_models)
    models = pq.read_free_models(tmp_path, now=NOW)
    assert len(models) == 1
    m = models[0]
    assert m["id"] == "good-pool"
    assert m["model_id"] == "model/good:free"
    assert m["intelligence_index"] == 38
    assert m["coding_index"] == 42
    assert m["status"] == "active"

    # 2. Worker Routing (temp_pool_read)
    entries = wr.load_temp_pools(pools_file)
    assert len(entries) == 1
    p = wr.temp_pool_read(entries[0], now=NOW)
    assert p.free == 1.0
    assert p.reason is None


def test_free_model_omitted_when_probe_is_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    pool = {
        "name": "pool-404",
        "provider": "Nous",
        "model": "nous/ended:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.nous.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "pricing": {"prompt": 0, "completion": 0},
        "live_probe": {"status": "404", "status_code": 404, "live_ok_at": now_str},
    }

    lists_doc = {
        "sources": {
            "https://api.nous.invalid/v1": {
                "ids": ["nous/ended:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "nous/ended:free": {"status": "404", "status_code": 404, "live_ok_at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # Quota panel completely omits 404 models
    models = pq.read_free_models(tmp_path, now=NOW)
    assert len(models) == 0

    # Worker router excludes model with probe_404
    entries = wr.load_temp_pools(pools_file)
    p = wr.temp_pool_read(entries[0], now=NOW)
    assert p.free is None
    assert p.reason == "probe_404"


def test_free_model_omitted_when_price_is_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    pool = {
        "name": "paid-pool",
        "provider": "OpenRouter",
        "model": "paid/model:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.openrouter.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "pricing": {"prompt": "0.001", "completion": 0},
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    lists_doc = {
        "sources": {
            "https://api.openrouter.invalid/v1": {
                "ids": ["paid/model:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "paid/model:free": {"status": "ok", "live_ok_at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    assert pq.read_free_models(tmp_path, now=NOW) == []
    entries = wr.load_temp_pools(pools_file)
    p = wr.temp_pool_read(entries[0], now=NOW)
    assert p.free is None
    assert p.reason == "nonzero_price"


def test_free_model_omitted_when_proof_is_stale_or_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_str = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pool 1: stale proof (2 hours old)
    pool_stale = {
        "name": "stale-pool",
        "provider": "TestProvider",
        "model": "model/stale:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 38,
        "coding_index": 42,
        "rate_limits": "free",
        "data_policy": "public",
        "live_probe": {"status": "ok", "live_ok_at": stale_str},
    }

    # Pool 2: missing proof completely
    pool_missing = {
        "name": "missing-proof-pool",
        "provider": "TestProvider",
        "model": "model/noproof:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 38,
        "coding_index": 42,
        "rate_limits": "free",
        "data_policy": "public",
    }

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": ["model/stale:free", "model/noproof:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "model/stale:free": {"status": "ok", "live_ok_at": stale_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool_stale, pool_missing], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    assert pq.read_free_models(tmp_path, now=NOW) == []
    entries = wr.load_temp_pools(pools_file)
    p_stale = wr.temp_pool_read(entries[0], now=NOW)
    assert p_stale.free is None and p_stale.reason == "stale_proof"
    p_missing = wr.temp_pool_read(entries[1], now=NOW)
    assert p_missing.free is None and p_missing.reason == "missing_proof"


def test_future_expires_at_alone_is_never_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    future_expiry = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Has expires_at in 30 days, but probe returned 404
    pool = {
        "name": "future-exp-404-pool",
        "provider": "Nous",
        "model": "nous/ended-early:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.nous.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "expires_at": future_expiry,
        "rate_limits": "free",
        "data_policy": "public",
        "live_probe": {"status": "404", "status_code": 404, "live_ok_at": now_str},
    }

    lists_doc = {
        "sources": {
            "https://api.nous.invalid/v1": {
                "ids": ["nous/ended-early:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "nous/ended-early:free": {"status": "404", "status_code": 404, "live_ok_at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # Must be omitted completely (never shown as "Attivo" or "Scaduto")
    assert pq.read_free_models(tmp_path, now=NOW) == []
    entries = wr.load_temp_pools(pools_file)
    p = wr.temp_pool_read(entries[0], now=NOW)
    assert p.free is None and p.reason == "probe_404"


def test_free_model_omitted_when_below_index_or_coding_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Reference scores: index=28, coding=35
    pool_low_index = {
        "name": "low-index-pool",
        "provider": "TestProvider",
        "model": "model/low-idx:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 25,  # < 28
        "coding_index": 40,
        "rate_limits": "free",
        "data_policy": "public",
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    pool_low_coding = {
        "name": "low-coding-pool",
        "provider": "TestProvider",
        "model": "model/low-code:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 35,
        "coding_index": 30,  # < 35
        "rate_limits": "free",
        "data_policy": "public",
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    pool_missing_coding = {
        "name": "missing-coding-pool",
        "provider": "TestProvider",
        "model": "model/no-code:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 35,
        "rate_limits": "free",
        "data_policy": "public",
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": ["model/low-idx:free", "model/low-code:free", "model/no-code:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "model/low-idx:free": {"status": "ok", "live_ok_at": now_str},
            "model/low-code:free": {"status": "ok", "live_ok_at": now_str},
            "model/no-code:free": {"status": "ok", "live_ok_at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool_low_index, pool_low_coding, pool_missing_coding], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # All three omitted from quota panel
    assert pq.read_free_models(tmp_path, now=NOW) == []

    # Worker router excludes each with exact reason
    entries = wr.load_temp_pools(pools_file)
    assert wr.temp_pool_read(entries[0], now=NOW).reason == "below_index"
    assert wr.temp_pool_read(entries[1], now=NOW).reason == "below_coding_index"
    assert wr.temp_pool_read(entries[2], now=NOW).reason == "unindexed"


def test_codex_stderr_auth_error_returns_auth_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty_snapshot = tmp_path / "empty-quota.json"
    monkeypatch.setenv("JARVISOS_WORKER_QUOTA_FILE", str(empty_snapshot))

    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stderr.write('not logged in / authentication required\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("JARVISOS_CODEX_BIN", str(fake_codex))

    res, err = pq.read_codex_limits()
    assert res is None
    assert err == "auth_required"


def test_missing_coding_scores_fails_closed_in_panel_and_router(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    pool = {
        "name": "pool-without-coding-scores",
        "provider": "TestProvider",
        "model": "model/good:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    # Top-level doc has "scores" and "reference", but NO "coding_scores"
    pools_data = {
        "reference": "luna",
        "scores": {"luna": 28, "sol": 40},
        "coding_reference": "luna",
        # missing coding_scores, and no reference_coding_index
        "pools": [pool],
    }
    pools_file = tmp_path / "temp-pools.json"
    pools_file.write_text(json.dumps(pools_data), encoding="utf-8")

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": ["model/good:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "model/good:free": {"status": "ok", "live_ok_at": now_str},
        },
    }
    lists_file = tmp_path / "temp-pool-lists.json"
    lists_file.write_text(json.dumps(lists_doc), encoding="utf-8")
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # read_free_models must fail closed and return empty
    assert pq.read_free_models(tmp_path, now=NOW) == []

    # temp_pool_read must fail closed with reason "no_reference"
    entries = wr.load_temp_pools(pools_file)
    assert len(entries) == 1
    p = wr.temp_pool_read(entries[0], now=NOW)
    assert p.free is None
    assert p.reason == "no_reference"


def test_free_model_proof_precedence_and_temp_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    older_str = (NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Pool with "live_status: ok" in temp-pools.json, but "probes" has 404 under "temp-{pool_name}"
    pool_404 = {
        "name": "pool-temp-404",
        "provider": "TestProvider",
        "model": "model/temp-404:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "live_status": "ok",
        "live_ok_at": now_str,
    }

    # 2. Pool with older OK (10m ago) and newer error (now)
    pool_err = {
        "name": "pool-newer-error",
        "provider": "TestProvider",
        "model": "model/newer-err:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "live_status": "ok",
        "live_ok_at": older_str,
    }

    # 3. Pool with older error (10m ago) and newer OK (now)
    pool_recovered = {
        "name": "pool-recovered",
        "provider": "TestProvider",
        "model": "model/recovered:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "live_status": "ok",
        "live_ok_at": now_str,
    }

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": ["model/temp-404:free", "model/newer-err:free", "model/recovered:free"],
                "read_at": now_str,
            }
        },
        "probes": {
            "temp-pool-temp-404": {"status": "404", "status_code": 404, "live_ok_at": now_str},
            "pool-newer-error": {"status": "ok", "live_ok_at": older_str},
            "model/newer-err:free": {"status": "error", "at": now_str},
            "model/recovered:free": {"status": "error", "at": older_str},
            "temp-pool-recovered": {"status": "ok", "code": 200, "at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool_404, pool_err, pool_recovered], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # Only recovered pool is admitted in read_free_models
    admitted = pq.read_free_models(tmp_path, now=NOW)
    assert len(admitted) == 1
    assert admitted[0]["id"] == "pool-recovered"

    # Router checks:
    entries = wr.load_temp_pools(pools_file)
    assert len(entries) == 3
    # 1. 404 fails closed with probe_404 despite live_status: ok
    p_404 = wr.temp_pool_read(entries[0], now=NOW)
    assert p_404.free is None and p_404.reason == "probe_404"

    # 2. Newer error beats older ok
    p_err = wr.temp_pool_read(entries[1], now=NOW)
    assert p_err.free is None and p_err.reason == "error"

    # 3. Newer ok beats older error
    p_rec = wr.temp_pool_read(entries[2], now=NOW)
    assert p_rec.free == 1.0 and p_rec.reason is None


def test_claude_refresh_rejects_stale_relay_entry_and_fails_session_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude_state"
    config_dir.mkdir(parents=True, exist_ok=True)
    cred_file = config_dir / ".credentials.json"
    expired_epoch = int((NOW - timedelta(days=5)).timestamp())
    cred_file.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired-access-token-12345",
                "refreshToken": "valid-refresh-token-67890",
                "expiresAt": expired_epoch,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_CLAUDE_CONFIG_DIR", str(config_dir))

    # agent-relay returns a stale 2020 snapshot
    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = {
            "agents": [
                {
                    "agent_key": "claude",
                    "status": "ok",
                    "as_of": "2020-01-01T00:00:00Z",
                    "windows": [
                        {"id": "5h", "label": "5-hour limit", "used_percent": 10.0, "resets_at": "2020-01-01T05:00:00Z"}
                    ],
                }
            ]
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    res, err = pq.read_claude_limits(refresh=True, now=NOW)
    assert err == "session_expired"
    assert isinstance(res, dict)
    assert res["status"] == "unavailable"
    assert res["reason"] == "session_expired"


def test_claude_refresh_sanitizes_relay_output_stripping_tokens_and_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude_state"
    config_dir.mkdir(parents=True, exist_ok=True)
    cred_file = config_dir / ".credentials.json"
    expired_epoch = int((NOW - timedelta(days=5)).timestamp())
    cred_file.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired-access-token-12345",
                "refreshToken": "valid-refresh-token-67890",
                "expiresAt": expired_epoch,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVISOS_CLAUDE_CONFIG_DIR", str(config_dir))

    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = {
            "agents": [
                {
                    "agent_key": "claude",
                    "status": "ok",
                    "as_of": now_str,
                    "token": "leak-token-12345",
                    "access_token": "leak-access-token-67890",
                    "refreshToken": "leak-refresh-token",
                    "api_key": "leak-key",
                    "headers": {"Authorization": "Bearer leak-secret"},
                    "windows": [
                        {"id": "5h", "label": "5-hour limit", "used_percent": 25.0, "resets_at": now_str, "secret_field": "leak-window"}
                    ],
                }
            ]
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    res, err = pq.read_claude_limits(refresh=True, now=NOW)
    assert err is None
    assert isinstance(res, dict)
    assert res["status"] == "ok"
    assert res["as_of"] == now_str
    # Whitelist verification: only explicit schema fields allowed
    assert "token" not in res
    assert "access_token" not in res
    assert "refreshToken" not in res
    assert "api_key" not in res
    assert "headers" not in res
    assert "secret_field" not in res["windows"][0]
    serialized = json.dumps(res)
    assert "leak-" not in serialized


def test_free_model_disqualified_when_source_publishes_nonzero_price_even_if_pool_claims_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    pool = {
        "name": "pool-claims-zero",
        "provider": "TestProvider",
        "model": "model/published-nonzero:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "pricing": {"prompt": 0, "completion": "0"},  # pool locally claims zero
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": ["model/published-nonzero:free"],
                "read_at": now_str,
                "pricing": {
                    "model/published-nonzero:free": {"prompt": "0.002", "completion": "0.002"}  # source publishes nonzero
                },
            }
        },
        "probes": {
            "model/published-nonzero:free": {"status": "ok", "live_ok_at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # Authoritative source pricing rejects the pool from panel and router
    assert pq.read_free_models(tmp_path, now=NOW) == []
    entries = wr.load_temp_pools(pools_file)
    p = wr.temp_pool_read(entries[0], now=NOW)
    assert p.free is None
    assert p.reason == "nonzero_price"


def test_free_model_excluded_when_unrelated_source_lists_model_id_but_own_source_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pool is configured with source pool-a
    pool = {
        "name": "pool-a",
        "provider": "ProviderA",
        "model": "model/shared:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.pool-a.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "pricing": {"prompt": 0, "completion": 0},
        "live_probe": {"status": "ok", "live_ok_at": now_str},
    }

    # lists_doc has pool-a (which does NOT list model/shared:free)
    # and pool-b (an unrelated source which DOES list model/shared:free)
    lists_doc = {
        "sources": {
            "https://api.pool-a.invalid/v1": {
                "ids": ["other/model:free"],
                "read_at": now_str,
            },
            "https://api.pool-b.invalid/v1": {
                "ids": ["model/shared:free"],
                "read_at": now_str,
            },
        },
        "probes": {
            "model/shared:free": {"status": "ok", "live_ok_at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # Must be excluded from free_models because pool-a does not list model/shared:free
    assert pq.read_free_models(tmp_path, now=NOW) == []


def test_active_throttled_exhausted_alone_do_not_satisfy_live_response_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    pools = [
        {
            "name": f"pool-{status}",
            "provider": "TestProvider",
            "model": f"model/{status}:free",
            "key_file": str(key_file),
            "free_list": "openai_models",
            "base_url": "https://api.test.invalid/v1",
            "index_score": 40,
            "coding_index": 45,
            "rate_limits": "free",
            "data_policy": "public",
            "pricing": {"prompt": 0, "completion": 0},
            "live_status": status,
            "live_ok_at": now_str,
        }
        for status in ("active", "throttled", "exhausted")
    ]

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": [f"model/{s}:free" for s in ("active", "throttled", "exhausted")],
                "read_at": now_str,
            }
        },
        "probes": {
            f"model/{s}:free": {"status": s, "live_ok_at": now_str}
            for s in ("active", "throttled", "exhausted")
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, pools, lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # None admitted to quota panel
    assert pq.read_free_models(tmp_path, now=NOW) == []

    # Worker router excludes each with missing_proof
    entries = wr.load_temp_pools(pools_file)
    assert len(entries) == 3
    for entry in entries:
        p = wr.temp_pool_read(entry, now=NOW)
        assert p.free is None
        assert p.reason == "missing_proof"


def test_models_fixture_with_nonzero_price_not_admitted_by_router_or_panel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")

    pool = {
        "name": "pool-zero-claim",
        "provider": "TestProvider",
        "model": "model/x:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        "pricing": {"prompt": 0, "completion": 0},  # pool claims price 0
    }
    pools_file = tmp_path / "temp-pools.json"
    pools_file.write_text(json.dumps({
        "reference": "luna",
        "scores": {"luna": 28},
        "coding_reference": "luna",
        "coding_scores": {"luna": 35},
        "pools": [pool],
    }), encoding="utf-8")
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # /models fixture returns x:free with published nonzero price
    def fake_opener(url: str, headers: dict[str, str], *args: Any) -> Any:
        if url.endswith("/models"):
            return {
                "data": [
                    {
                        "id": "model/x:free",
                        "pricing": {"prompt": "0.002", "completion": "0.002"},
                    }
                ]
            }
        return {"choices": [{"message": {"content": "OK"}}]}

    entries = wr.load_temp_pools(pools_file)
    p = wr.temp_pool_read(entries[0], now=NOW, opener=fake_opener)
    # Not admitted by router
    assert p.free is None
    assert p.reason in ("not_listed", "nonzero_price")

    # Cache carries published pricing under the source entry
    cache = json.loads((tmp_path / "temp-pool-lists.json").read_text(encoding="utf-8"))
    source_entry = cache["sources"]["https://api.test.invalid/v1"]
    assert "model/x:free" not in source_entry["ids"]
    assert source_entry["pricing"]["model/x:free"] == {"prompt": "0.002", "completion": "0.002"}

    # Not admitted by quota panel
    assert pq.read_free_models(tmp_path, now=NOW) == []


def test_live_status_alone_rejected_cached_fresh_200_accepted_and_errors_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_str = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def make_pool(name: str, model_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "name": name,
            "provider": "TestProvider",
            "model": model_id,
            "key_file": str(key_file),
            "free_list": "openai_models",
            "base_url": "https://api.test.invalid/v1",
            "index_score": 40,
            "coding_index": 45,
            "rate_limits": "free",
            "data_policy": "public",
            **kwargs,
        }

    pools = [
        make_pool("pool-claim-only", "model/claim:free", live_status="ok", live_ok_at=now_str),
        make_pool("pool-fresh-200", "model/fresh200:free"),
        make_pool("pool-404", "model/err404:free"),
        make_pool("pool-402", "model/err402:free"),
        make_pool("pool-timeout", "model/timeout:free"),
        make_pool("pool-stale", "model/stale:free"),
    ]

    lists_doc = {
        "sources": {
            "https://api.test.invalid/v1": {
                "ids": [p["model"] for p in pools],
                "read_at": now_str,
            }
        },
        "probes": {
            # Note: pool-claim-only has NO probe in lists_doc
            "model/fresh200:free": {"status": "ok", "code": 200, "at": now_str},
            "model/err404:free": {"status": "404", "code": 404, "at": now_str},
            "model/err402:free": {"status": "402", "code": 402, "at": now_str},
            "model/timeout:free": {"status": "timeout", "code": 408, "at": now_str},
            "model/stale:free": {"status": "ok", "code": 200, "at": stale_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, pools, lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # Panel check: ONLY fresh 200 is admitted
    admitted = pq.read_free_models(tmp_path, now=NOW)
    assert len(admitted) == 1
    assert admitted[0]["id"] == "pool-fresh-200"

    # Router checks:
    entries = wr.load_temp_pools(pools_file)
    assert len(entries) == 6

    # 1. Pool-entry live_status alone is rejected
    p_claim = wr.temp_pool_read(entries[0], now=NOW)
    assert p_claim.free is None and p_claim.reason == "missing_proof"

    # 2. Cached fresh 200 probe is accepted
    p_fresh = wr.temp_pool_read(entries[1], now=NOW)
    assert p_fresh.free == 1.0 and p_fresh.reason is None

    # 3. 404 probe rejects
    p_404 = wr.temp_pool_read(entries[2], now=NOW)
    assert p_404.free is None and p_404.reason == "probe_404"

    # 4. 402 probe rejects
    p_402 = wr.temp_pool_read(entries[3], now=NOW)
    assert p_402.free is None and p_402.reason == "payment_required"

    # 5. timeout probe rejects
    p_timeout = wr.temp_pool_read(entries[4], now=NOW)
    assert p_timeout.free is None and p_timeout.reason == "timeout"

    # 6. stale probe rejects
    p_stale = wr.temp_pool_read(entries[5], now=NOW)
    assert p_stale.free is None and p_stale.reason == "stale_proof"


def test_bounded_probe_producer_behavior_on_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-token", encoding="utf-8")

    pool = {
        "name": "pool-probe-test",
        "provider": "TestProvider",
        "model": "model/probed:free",
        "key_file": str(key_file),
        "free_list": "openai_models",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
    }
    pools_file = tmp_path / "temp-pools.json"
    pools_file.write_text(json.dumps({
        "reference": "luna",
        "scores": {"luna": 28},
        "coding_reference": "luna",
        "coding_scores": {"luna": 35},
        "pools": [pool],
    }), encoding="utf-8")
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    calls: list[dict[str, Any]] = []

    def mock_prober(entry: Any, model_id: str, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
        calls.append({
            "model_id": model_id,
            "url": url,
            "headers": headers,
            "payload": payload,
        })
        return {"choices": [{"message": {"content": "OK"}}]}

    def mock_opener(url: str, headers: dict[str, str], *args: Any) -> Any:
        assert url.endswith("/models")
        return {"data": [{"id": "model/probed:free"}]}

    entries = wr.load_temp_pools(pools_file)
    p = wr.temp_pool_read(entries[0], now=NOW, opener=mock_opener, prober=mock_prober)
    assert p.free == 1.0

    # Probe details verified
    assert len(calls) == 1  # bounded: exactly once per refresh
    call = calls[0]
    assert call["model_id"] == "model/probed:free"
    assert call["url"] == "https://api.test.invalid/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret-token"
    assert call["payload"] == {
        "model": "model/probed:free",
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 8,
    }

    # Lists cache contains probe
    cache = json.loads((tmp_path / "temp-pool-lists.json").read_text(encoding="utf-8"))
    probe = cache["probes"]["model/probed:free"]
    assert probe["status"] == "ok"
    assert probe["code"] == 200
    assert probe["at"] == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_pool_without_free_list_not_shown_even_if_free_source_lists_model_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now_str = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pool without free_list
    pool = {
        "name": "pool-no-free-list",
        "provider": "TestProvider",
        "model": "model/nofreelist:free",
        "base_url": "https://api.test.invalid/v1",
        "index_score": 40,
        "coding_index": 45,
        "rate_limits": "free",
        "data_policy": "public",
        # NO free_list key
    }

    # Lists doc has a generic "free" source and provider listing model/nofreelist:free
    lists_doc = {
        "sources": {
            "free": {
                "ids": ["model/nofreelist:free"],
                "read_at": now_str,
            },
            "TestProvider": {
                "ids": ["model/nofreelist:free"],
                "read_at": now_str,
            },
            "https://api.test.invalid/v1": {
                "ids": ["model/nofreelist:free"],
                "read_at": now_str,
            },
        },
        "probes": {
            "model/nofreelist:free": {"status": "ok", "code": 200, "at": now_str},
        },
    }

    pools_file, _ = _setup_free_pools_fixture(tmp_path, [pool], lists_doc)
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools_file))

    # Must be excluded (fail closed)
    assert pq.read_free_models(tmp_path, now=NOW) == []
