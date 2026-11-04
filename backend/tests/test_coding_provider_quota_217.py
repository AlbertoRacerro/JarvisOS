"""Spec 217: read-only provider quota rings for Coding → Runtime (offline)."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.coding import provider_quota as pq
from app.modules.coding import runtime_routes
from app.modules.coding.runtime_truth import CANONICAL_RUNTIME_REPOSITORY

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def reading(agy: Any = None, **agents: Any) -> dict[str, Any]:
    codex = agents.pop("codex", None)
    return {"relay": {"agents": [{"agent_key": key, **body} for key, body in agents.items()]}, "agy": agy, "codex": codex}


AGY = {"type": "command", "command": {"name": "quota", "data": {"groups": [
    {"name": "Gemini Models", "buckets": [
        {"id": "gemini-5h", "remaining_fraction": 1.0, "reset_time": "2026-10-24T16:00:00Z", "disabled": False},
        {"id": "gemini-weekly", "remaining_fraction": 0.49, "reset_time": "2026-10-25T22:00:00Z", "disabled": False}]},
    {"name": "Claude and GPT models", "buckets": [
        {"id": "3p-5h", "remaining_fraction": 1.0, "reset_time": "2026-10-24T16:00:00Z", "disabled": False},
        {"id": "3p-weekly", "remaining_fraction": 1.0, "reset_time": "2026-10-28T00:00:00Z", "disabled": True}]},
    {"name": "Gemini 4 Argon", "buckets": [
        {"id": "argon-weekly", "remaining_fraction": 0.49, "reset_time": "2026-10-25T22:00:00Z"}]}]}}}


def provider(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return next(item for item in payload["providers"] if item["provider"] == key)


def test_five_hour_weekly_and_model_pool_windows_are_normalised() -> None:
    payload = pq.normalize(reading(claude={"status": "ok", "as_of": iso(NOW - timedelta(minutes=2)), "windows": [
        {"id": "five_hour", "used_percent": 51, "resets_at": iso(NOW + timedelta(hours=2))},
        {"id": "seven_day", "used_percent": 12.34, "resets_at": iso(NOW + timedelta(days=3))},
        {"id": "seven_day_opus", "label": "Opus weekly", "used_percent": 80, "resets_at": iso(NOW + timedelta(days=3))},
    ]}), NOW)
    claude = provider(payload, "claude")
    assert claude["status"] == "ok"
    kinds = [(w["kind"], w["used_percent"], w["remaining_percent"]) for w in claude["windows"]]
    assert kinds == [("five_hour", 51.0, 49.0), ("weekly", 12.3, 87.7), ("frontier", 80.0, 20.0)]
    assert claude["windows"][2]["label"] == "Opus weekly"
    assert not any(w["stale"] or w["exhausted"] for w in claude["windows"])


def test_window_minutes_win_over_ids() -> None:
    assert pq.window_kind({"id": "primary", "window_minutes": 10080}) == "weekly"
    assert pq.window_kind({"id": "x", "window_minutes": 300}) == "five_hour"
    assert pq.window_kind({"id": "secondary"}) == "weekly"


def test_exhausted_stale_and_past_reset() -> None:
    payload = pq.normalize(reading(codex={"status": "ok", "as_of": iso(NOW - timedelta(hours=3)), "windows": [
        {"id": "primary", "used_percent": 100, "resets_at": iso(NOW + timedelta(minutes=40))},
        {"id": "secondary", "used_percent": 100, "resets_at": iso(NOW - timedelta(hours=1))},
    ]}), NOW)
    five, week = provider(payload, "codex")["windows"]
    assert five["exhausted"] and five["stale"]  # old reading
    assert week["stale"] and not week["exhausted"]  # reset already passed: the 100 % is out of date


@pytest.mark.parametrize("windows", [None, [], [{}], [{"id": "s", "used_percent": "50"}], [{"id": "s", "used_percent": True}],
                                     [{"id": "s", "used_percent": 140}], [{"id": "s", "used_percent": -1}]])
def test_unusable_windows_are_not_rendered_as_zero(windows: Any) -> None:
    claude = provider(pq.normalize(reading(claude={"status": "ok", "as_of": iso(NOW), "windows": windows}), NOW), "claude")
    assert claude["status"] == "unavailable" and claude["windows"] == []


def test_unauthenticated_missing_and_unreadable_states() -> None:
    payload = pq.normalize(reading(claude={"status": "not_logged_in"}), NOW)
    assert provider(payload, "claude")["status"] == "unauthenticated"
    assert provider(payload, "codex") == {"provider": "codex", "label": "Codex (ChatGPT)", "source": "codex account/rateLimits/read",
                                         "status": "unavailable", "reason": "codex_limits_unreadable", "windows": []}
    unreadable = pq.normalize({"relay": None, "agy": None}, NOW)
    assert {p["reason"] for p in unreadable["providers"]} == {"relay_limits_unreadable", "agy_quota_unreadable", "codex_limits_unreadable", "copilot_quota_unreadable"}


def test_antigravity_buckets_from_agy_quota() -> None:
    agy = provider(pq.normalize(reading(AGY), NOW), "antigravity")
    assert agy["status"] == "ok" and agy["source"] == "agy /quota" and agy["as_of"] == iso(NOW)
    rows = [(w["id"], w["kind"], w["label"], w["remaining_percent"]) for w in agy["windows"]]
    # AGY owns the shared Claude & GPT pool; its disabled weekly bucket stays absent.
    assert rows == [("gemini-5h", "five_hour", None, 100.0), ("gemini-weekly", "weekly", None, 49.0),
                    ("3p-5h", "pool", "Claude & GPT · 5-hour", 100.0),
                    ("argon-weekly", "frontier", "Argon · weekly", 49.0)]


def test_agy_claude_windows_keep_their_source_separate_from_claude_code() -> None:
    buckets = [{"id": "3p-5h", "remaining_fraction": 0.23, "reset_time": iso(NOW + timedelta(hours=1))},
               {"id": "3p-weekly", "remaining_fraction": 0.81, "reset_time": iso(NOW + timedelta(days=2))}]
    payload = pq.normalize(reading({"buckets": buckets}, claude={"status": "not_logged_in"}), NOW)
    assert provider(payload, "claude")["status"] == "unauthenticated"
    agy = provider(payload, "antigravity")
    assert agy["source"] == "agy /quota" and agy["as_of"] == iso(NOW)
    assert [(w["id"], w["kind"], w["remaining_percent"]) for w in agy["windows"]] == [
        ("3p-5h", "pool", 23.0), ("3p-weekly", "pool", 81.0)]
    assert agy["windows"][1]["label"] == "Claude & GPT · weekly"


@pytest.mark.parametrize("bucket", [
    {"remaining_fraction": 0.6, "disabled": True}, {"remaining_fraction": True},
    {"remaining_fraction": -0.1}, {"remaining_fraction": 1.1}, {"remaining_fraction": "0.6"},
    {"remaining_fraction": float("nan")}, {},
])
def test_unusable_agy_claude_bucket_is_absent_not_zero(bucket: dict[str, Any]) -> None:
    agy = provider(pq.normalize(reading({"buckets": [{"id": "3p-weekly", **bucket}]}), NOW), "antigravity")
    assert agy["status"] == "unavailable" and agy["windows"] == []


def test_antigravity_is_never_estimated() -> None:
    unreadable = provider(pq.normalize(reading(None), NOW), "antigravity")
    assert unreadable["status"] == "unavailable" and unreadable["reason"] == "agy_quota_unreadable" and unreadable["windows"] == []
    junk = {"command": {"data": {"groups": [{"buckets": [{"id": "gemini-5h", "remaining_fraction": 1.4},
                                                          {"id": "gemini-weekly", "remaining_fraction": "0.5"}]}]}}}
    empty = provider(pq.normalize(reading(junk), NOW), "antigravity")
    assert empty["status"] == "unavailable" and empty["reason"] == "no_usage_windows"


def test_agy_output_with_log_lines_before_the_json() -> None:
    assert pq._json_document("warming up\n" + json.dumps(AGY)) == AGY
    assert pq._json_document("no json here") is None


def test_no_tool_text_paths_or_secrets_reach_the_payload() -> None:
    secret = "ghp_" + "a" * 36
    payload = pq.normalize(reading(claude={"status": "ok", "as_of": iso(NOW), "stderr": secret, "path": "/home/x/.config",
                                           "windows": [{"id": secret, "label": "/home/x/secret", "used_percent": 5, "raw": secret}]}), NOW)
    text = json.dumps(payload)
    assert secret not in text and "/home/" not in text
    assert provider(payload, "claude")["windows"][0]["id"] == "window"


def test_cache_reads_once_per_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_PROVIDER_QUOTA_TTL_S", "600")
    calls: list[int] = []
    clock = [NOW]

    def reader() -> dict[str, Any]:
        calls.append(1)
        return reading(claude={"status": "ok", "as_of": iso(clock[0]), "windows": [{"id": "five_hour", "used_percent": 10}]})

    cache = pq.QuotaCache(reader, clock=lambda: clock[0])
    first = cache.get()
    clock[0] = NOW + timedelta(minutes=5)
    assert cache.get()["read_at"] == first["read_at"] and len(calls) == 1
    clock[0] = NOW + timedelta(minutes=11)
    assert cache.get()["read_at"] == iso(clock[0]) and len(calls) == 2
    assert first["next_read_at"] == iso(NOW + timedelta(minutes=10))


def test_ttl_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_PROVIDER_QUOTA_TTL_S", "3")
    assert pq.ttl_seconds() == pq.MIN_TTL_S
    monkeypatch.setenv("JARVISOS_PROVIDER_QUOTA_TTL_S", "nope")
    assert pq.ttl_seconds() == pq.DEFAULT_TTL_S


def test_missing_tools_are_unreadable_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_AGENT_RELAY_BIN", "/nonexistent/agent-relay")
    monkeypatch.setenv("JARVISOS_AGY_BIN", "/nonexistent/agy")
    monkeypatch.setenv("JARVISOS_CODEX_BIN", "/nonexistent/codex")
    monkeypatch.setenv("JARVISOS_GH_BIN", "/nonexistent/gh")
    readings = pq.read_all()
    assert readings == {"relay": None, "agy": None, "codex": None, "copilot": None,
                        "errors": {"relay": "not_found", "agy": "not_found", "codex": "not_found", "copilot": "not_found"}}
    assert {p["reason"] for p in pq.normalize(readings, NOW)["providers"]} == {"relay_not_found", "agy_not_found", "codex_not_found", "copilot_not_found"}


def test_tools_outside_the_service_path_are_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The launcher starts the backend without a login PATH; the CLIs usually live in ~/.local/bin.
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "agent-relay"
    tool.write_text("#!/bin/sh\necho 'warming up'\necho '{\"agents\": []}'\n")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("JARVISOS_AGENT_RELAY_BIN", raising=False)
    assert pq.read_relay_limits() == ({"agents": []}, None)


def test_read_failures_name_their_cause(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\necho 'not signed in' >&2\nexit 1\n")
    silent.chmod(0o755)
    monkeypatch.setenv("JARVISOS_AGY_BIN", str(silent))
    assert pq.read_agy_quota() == (None, "no_json")

    def slow(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("agent-relay", pq.READ_TIMEOUT_S)

    monkeypatch.setattr(pq.subprocess, "run", slow)
    assert pq.read_relay_limits() == (None, "timeout")
    reasons = pq.normalize({"relay": None, "agy": None, "errors": {"relay": "timeout", "agy": "no_json"}}, NOW)
    assert {p["reason"] for p in reasons["providers"]} == {"relay_timeout", "agy_no_json", "codex_limits_unreadable", "copilot_quota_unreadable"}
    junk = pq.normalize({"relay": None, "agy": None, "errors": {"relay": "/home/x/secret", "agy": 3}}, NOW)
    assert {p["reason"] for p in junk["providers"]} == {"relay_limits_unreadable", "agy_quota_unreadable", "codex_limits_unreadable", "copilot_quota_unreadable"}


def test_route_requires_the_runtime_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(runtime_routes.router)
    monkeypatch.setattr(pq, "_cache", pq.QuotaCache(lambda: {}, clock=lambda: NOW))
    client = TestClient(app)
    prefix = runtime_routes.router.prefix
    ok = client.get(f"{prefix}/provider-quotas", params={"repository": CANONICAL_RUNTIME_REPOSITORY})
    assert ok.status_code == 200 and [p["provider"] for p in ok.json()["providers"]] == ["claude", "codex", "antigravity", "copilot"]
    assert client.get(f"{prefix}/provider-quotas", params={"repository": "someone/else"}).status_code == 400


# Shapes captured from the maintainer's host on 2026-10-24 (agent-relay limits, agy 1.4 `/quota`); free text trimmed.
REAL_RELAY = {"command": "limits", "agents": [
    {"agent_key": "claude", "display_name": "Claude Code", "status": "ok", "plan": None, "reason": None,
     "as_of": "2026-10-24T13:39:36.058877+00:00", "windows": [
         {"id": "session", "label": "5-hour limit", "used_percent": 6.0, "resets_at": "2026-10-24T17:20:00.001189+00:00"},
         {"id": "weekly", "label": "Weekly limit", "used_percent": 80.0, "resets_at": "2026-10-26T06:00:00.001234+00:00"}]},
    {"agent_key": "codex", "display_name": "Codex", "status": "ok", "plan": "plus", "reason": None,
     "as_of": "2026-10-22T14:16:38.267Z", "windows": [
         {"id": "session", "label": "5-hour limit", "used_percent": 4.0, "resets_at": "2026-10-22T17:37:26+00:00"},
         {"id": "weekly", "label": "Weekly limit", "used_percent": 88.0, "resets_at": "2026-10-27T07:45:12+00:00"}]}]}
REAL_AGY = "\n" + json.dumps({"conversation_id": "", "status": "SUCCESS", "num_turns": 0, "command": {"name": "usage", "data": {"groups": [
    {"name": "Gemini Models", "buckets": [
        {"id": "gemini-weekly", "name": "Weekly Limit Remaining", "window": "weekly", "remaining_fraction": 1,
         "reset_time": "2026-10-31T13:39:53Z"},
        {"id": "gemini-5h", "name": "Five Hour Limit Remaining", "window": "5h", "remaining_fraction": 1,
         "reset_time": "2026-10-24T18:39:53Z"}]},
    {"name": "Claude and GPT models", "buckets": [
        {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1, "reset_time": "2026-10-31T13:39:53Z"},
        {"id": "3p-5h", "window": "5h", "remaining_fraction": 1, "reset_time": "2026-10-24T18:39:53Z"}]},
    {"name": "Gemini 4 Argon", "buckets": [
        {"id": "gemini-argon-wtu-weekly", "name": "Gemini 4 Argon Weekly Limit", "window": "weekly",
         "remaining_fraction": 0.4909217357635498, "reset_time": "2026-10-25T07:58:48Z"}]}]}}})


def test_real_host_readings_map_to_the_expected_rings() -> None:
    now = datetime(2026, 10, 24, 13, 40, tzinfo=UTC)
    payload = pq.normalize({"relay": REAL_RELAY, "codex": REAL_RELAY["agents"][1], "agy": pq._json_document(REAL_AGY)}, now)
    rows = {p["provider"]: [(w["kind"], w["remaining_percent"], w["stale"]) for w in p["windows"]] for p in payload["providers"]}
    assert rows["claude"] == [("five_hour", 94.0, False), ("weekly", 20.0, False)]
    # Relay's Codex reading was two days old on the host: shown, but flagged out of date.
    assert rows["codex"] == [("five_hour", 96.0, True), ("weekly", 12.0, True)]
    assert rows["antigravity"] == [("weekly", 100.0, False), ("five_hour", 100.0, False),
                                    ("pool", 100.0, False), ("pool", 100.0, False), ("frontier", 49.1, False)]


def test_manual_refresh_bypasses_ttl_and_cache_ages_against_current_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_PROVIDER_QUOTA_TTL_S", "3600")
    calls = []
    clock = [NOW]

    def reader() -> dict[str, Any]:
        calls.append(clock[0])
        return reading(codex={"status": "ok", "as_of": iso(clock[0]),
                              "windows": [{"id": "primary", "used_percent": 23,
                                           "resets_at": iso(clock[0] + timedelta(minutes=10))}]})

    cache = pq.QuotaCache(reader, clock=lambda: clock[0])
    original = cache.get()
    clock[0] += timedelta(minutes=11)
    cached = cache.get()
    assert len(calls) == 1 and cached["read_at"] == original["read_at"]
    assert provider(cached, "codex")["windows"][0]["stale"]  # reset passed inside the TTL
    fresh = cache.get(refresh=True)
    assert len(calls) == 2 and fresh["read_at"] == iso(clock[0])
    assert not provider(fresh, "codex")["windows"][0]["stale"]
    clock[0] += timedelta(minutes=31)
    assert provider(cache.get(), "codex")["windows"][0]["stale"]


def test_concurrent_manual_refreshes_share_one_reading() -> None:
    entered = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    lock = threading.Lock()
    calls = []

    class ObservedLock:
        def __enter__(self) -> None:
            if lock.locked():
                waiting.set()
            lock.acquire()

        def __exit__(self, *args: Any) -> None:
            lock.release()

    def reader() -> dict[str, Any]:
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {}

    cache = pq.QuotaCache(reader, clock=lambda: NOW)
    cache._lock = ObservedLock()  # type: ignore[assignment]
    results = []
    first = threading.Thread(target=lambda: results.append(cache.get(refresh=True)))
    second = threading.Thread(target=lambda: results.append(cache.get(refresh=True)))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert waiting.wait(5)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert len(calls) == 1 and len(results) == 2


def test_cached_antigravity_retains_source_observation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_PROVIDER_QUOTA_TTL_S", "3600")
    clock = [NOW]
    cache = pq.QuotaCache(lambda: reading(AGY), clock=lambda: clock[0])
    first = cache.get()
    clock[0] += timedelta(minutes=31)
    cached = cache.get()
    assert provider(cached, "antigravity")["as_of"] == provider(first, "antigravity")["as_of"] == iso(NOW)
    assert all(w["stale"] for w in provider(cached, "antigravity")["windows"])
    refreshed = cache.get(refresh=True)
    assert provider(refreshed, "antigravity")["as_of"] == iso(clock[0])
    assert not any(w["stale"] for w in provider(refreshed, "antigravity")["windows"])


def test_refresh_route_forwards_explicit_refresh_after_repository_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(runtime_routes.router)
    calls = []

    def quotas(*, refresh: bool = False) -> dict[str, Any]:
        calls.append(refresh)
        return {"state": "ok"}

    monkeypatch.setattr(pq, "quotas", quotas)
    client = TestClient(app)
    url = f"{runtime_routes.router.prefix}/provider-quotas"
    assert client.get(url, params={"repository": CANONICAL_RUNTIME_REPOSITORY, "refresh": True}).status_code == 200
    assert client.get(url, params={"repository": CANONICAL_RUNTIME_REPOSITORY}).status_code == 200
    assert client.get(url, params={"repository": "someone/else", "refresh": True}).status_code == 400
    assert client.get(url, params={"repository": CANONICAL_RUNTIME_REPOSITORY, "refresh": "bad"}).status_code == 422
    assert calls == [True, False]


def test_codex_usage_protocol_has_no_model_turn_and_discards_account_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool = tmp_path / "codex"
    log = tmp_path / "requests.jsonl"
    tool.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ["JARVISOS_CODEX_TEST_LOG"], "a") as log:
        log.write(json.dumps(request) + "\\n")
    if request["method"] == "initialize":
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif request["method"] == "account/rateLimits/read":
        print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
        print(json.dumps({"id": request["id"], "result": {
            "accountId": "private-account-id", "rateLimitUpsell": "private-message",
            "rateLimits": {"primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {"codex": {
                "primary": {"usedPercent": 23, "windowDurationMins": 300, "resetsAt": 1793363692},
                "secondary": {"usedPercent": 77, "windowDurationMins": 10080, "resetsAt": 1793775598}
            }}
        }}), flush=True)
''')
    tool.chmod(0o755)
    monkeypatch.setenv("JARVISOS_CODEX_BIN", str(tool))
    monkeypatch.setenv("JARVISOS_CODEX_TEST_LOG", str(log))
    result, error = pq.read_codex_limits()
    assert error is None and result is not None
    assert [w["used_percent"] for w in result["windows"]] == [23, 77]
    assert [w["window_minutes"] for w in result["windows"]] == [300, 10080]
    assert all(w["resets_at"].endswith("Z") for w in result["windows"])
    assert "private" not in json.dumps(result)
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["method"] for r in requests] == ["initialize", "initialized", "account/rateLimits/read"]


def test_codex_usage_timeout_reaps_ephemeral_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool = tmp_path / "slow-codex"
    tool.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    tool.chmod(0o755)
    monkeypatch.setenv("JARVISOS_CODEX_BIN", str(tool))
    monkeypatch.setattr(pq, "READ_TIMEOUT_S", 0.05)
    real_popen = subprocess.Popen
    children = []

    def tracked_popen(*args: Any, **kwargs: Any) -> Any:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(pq.subprocess, "Popen", tracked_popen)
    assert pq.read_codex_limits() == (None, "timeout")
    assert len(children) == 1 and children[0].poll() is not None


def test_status_sources_start_independently_and_preserve_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Barrier(4)

    def relay(*, refresh: bool = False) -> tuple[dict[str, Any], None]:
        started.wait(timeout=5)
        return {"agents": []}, None

    def agy() -> tuple[None, str]:
        started.wait(timeout=5)
        return None, "timeout"

    def codex() -> tuple[dict[str, Any], None]:
        started.wait(timeout=5)
        return {"status": "ok", "windows": []}, None

    def copilot() -> tuple[None, str]:
        started.wait(timeout=5)
        return None, "timeout"

    monkeypatch.setattr(pq, "read_copilot_quota", copilot)
    monkeypatch.setattr(pq, "read_relay_limits", relay)
    monkeypatch.setattr(pq, "read_agy_quota", agy)
    monkeypatch.setattr(pq, "read_codex_limits", codex)
    result = pq.read_all()
    assert result == {"relay": {"agents": []}, "agy": None, "codex": {"status": "ok", "windows": []}, "copilot": None,
                      "errors": {"relay": None, "agy": "timeout", "codex": None, "copilot": "timeout"}}


@pytest.mark.parametrize(("bucket", "expected"), [
    ({"percent_remaining": 35}, 35), ({"unlimited": True}, 100),
    ({"percent_remaining": 50, "remaining": 0}, 0), ({"has_quota": False}, 0),
])
def test_copilot_pool(bucket: dict[str, Any], expected: int) -> None:
    data = {"quota_snapshots": {"premium_interactions": bucket}, "quota_reset_date": "2026-11-01"}
    result = provider(pq.normalize({"copilot": data}, NOW), "copilot")
    assert result["status"] == "ok"
    assert result["windows"][0]["remaining_percent"] == expected
    assert result["windows"][0]["resets_at"] == "2026-11-01T00:00:00Z"


@pytest.mark.parametrize(("data", "reason"), [
    ({"cli_enabled": False}, "cli_disabled"), ({}, "bucket_missing"),
    ({"quota_snapshots": {"premium_interactions": {"has_quota": False, "entitlement": 0}}}, "no_entitlement"),
    ({"quota_snapshots": {"premium_interactions": {"percent_remaining": float("nan")}}}, "invalid_reading"),
])
def test_copilot_invalid(data: dict[str, Any], reason: str) -> None:
    assert provider(pq.normalize({"copilot": data}, NOW), "copilot")["reason"] == reason


def test_relay_cached_and_refresh_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    cached = {"agents": [{"agent_key": "claude", "status": "ok", "as_of": iso(now),
                          "windows": [{"id": "weekly", "used_percent": 12}]}]}
    calls: list[list[str]] = []
    def run(_env: str, _cmd: str, args: list[str]) -> tuple[str, None]:
        calls.append(args)
        return json.dumps(cached if len(args) == 2 else {"agents": [{"agent_key": "claude", "status": "unavailable"}]}), None
    monkeypatch.setattr(pq, "_run", run)
    assert pq.read_relay_limits()[0] == cached and len(calls) == 1
    assert pq.read_relay_limits(refresh=True)[0] == cached and len(calls) == 3


def test_last_good_retains_age_and_unavailable_reason() -> None:
    clock = [NOW]
    values = iter([reading(claude={"status": "ok", "as_of": iso(NOW), "windows": [{"used_percent": 20}]}), {}, {}])
    cache = pq.QuotaCache(lambda: next(values), clock=lambda: clock[0])
    cache.get()
    clock[0] += timedelta(hours=1)
    result = provider(cache.get(refresh=True), "claude")
    assert result["status"] == "ok" and result["last_error"] == "relay_limits_unreadable"
    assert result["as_of"] == iso(NOW) and result["windows"][0]["stale"]
    assert provider(cache.get(), "copilot")["status"] == "unavailable"


@pytest.mark.parametrize("error", ["timeout", "no_json"])
def test_copilot_cli_failure(monkeypatch: pytest.MonkeyPatch, error: str) -> None:
    monkeypatch.setattr(pq, "_run", lambda *_args: (None, error))
    data, failure = pq.read_copilot_quota()
    result = provider(pq.normalize({"copilot": data, "errors": {"copilot": failure}}, NOW), "copilot")
    assert result["status"] == "unavailable" and result["reason"] == f"copilot_{error}"
