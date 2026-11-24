"""Spec 349: Offline unit tests for Claude live refresh, Codex resilience, and live-verified free models."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import scripts.worker_routing as wr

from app.modules.coding import provider_quota as pq

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
