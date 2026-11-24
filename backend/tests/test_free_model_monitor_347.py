"""Offline evidence for free-only discovery and supervisor isolation."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "free_model_monitor.py"
SPEC = importlib.util.spec_from_file_location("free_model_monitor", SCRIPT)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)
NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    control = tmp_path / "control"
    control.mkdir()
    target = tmp_path / "temp-pools.json"
    target.write_text(json.dumps({
        "index": {"name": "curated"}, "reference": "base", "scores": {"base": 30},
        "read_at": "2026-11-23", "catalog_scores": {"strong:free": 42, "paid:free": 50,
                                                       "low:free": 20, "zen-free": 35,
                                                       "custom:free": 40},
        "watchlist": ["glm-5.4"], "pools": [{"name": "manual", "model": "manual:free",
                                               "free_list": "openrouter_public", "index_score": 44}],
    }), encoding="utf-8")
    return control, target


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_admission_prices_metadata_and_manual_preservation(tmp_path: Path) -> None:
    control, target = fixture(tmp_path)
    original = read(target)
    key = tmp_path / "key"
    key.write_text("supersecret123", encoding="utf-8")
    (tmp_path / "free-monitor-sources.json").write_text(json.dumps({"sources": [{
        "source_id": "custom", "provider": "Example", "free_list": "openai_models",
        "base_url": "https://example.invalid/v1", "key_file": str(key), "free_suffix": ":free",
        "rate_limits": "limited", "data_policy": "public",
    }]}), encoding="utf-8")
    calls: list[tuple[str, dict[str, str]]] = []

    def opener(url: str, headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        calls.append((url, headers))
        if url == monitor.OPENROUTER:
            return {"data": [{"id": "strong:free", "pricing": {"prompt": "0", "completion": 0}},
                             {"id": "paid:free", "pricing": {"prompt": "0.01", "completion": 0}},
                             {"id": "low:free", "pricing": {"prompt": 0}},
                             {"id": "not-free", "pricing": {"prompt": 0}}]}
        if url == monitor.ZEN:
            return {"data": [{"id": "zen-free"}]}
        assert headers == {"Authorization": "Bearer supersecret123"}
        return {"data": [{"id": "custom:free", "pricing": {"prompt": "0", "completion": "0"}}]}

    result = monitor.check(control_dir=control, now=NOW, opener=opener)
    assert result["checked"]
    document = read(target)
    assert {k: document[k] for k in ("index", "reference", "scores", "read_at", "catalog_scores", "watchlist")} == {
        k: original[k] for k in ("index", "reference", "scores", "read_at", "catalog_scores", "watchlist")}
    assert document["pools"][0]["name"] == "manual"
    autos = {p["model"]: p for p in document["pools"] if p.get("auto_managed")}
    assert set(autos) == {"strong:free", "zen-free", "custom:free"}
    assert all(p["index_score"] >= 30 for p in autos.values())
    assert autos["custom:free"]["key_file"] == str(key)
    assert len(calls) == 3
    for path in (target, tmp_path / "free-model-monitor-state.json"):
        assert "supersecret123" not in path.read_text(encoding="utf-8")


def test_transitions_pruning_cadence_and_no_duplicate_alert(tmp_path: Path) -> None:
    control, target = fixture(tmp_path)
    messages: list[str] = []

    def opener(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        return {"data": [{"id": "strong:free"}, {"id": "glm-5.4:free"}]} if url == monitor.OPENROUTER else {"data": []}

    def notify(_kind, message):
        return messages.append(message)
    monitor.maybe_run_daily(control_dir=control, now=NOW, opener=opener, notify_fn=notify)
    assert messages == ["Nuovo modello gratuito disponibile: glm-5.4:free su OpenRouter.",
                        "Nuovo modello gratuito disponibile: strong:free su OpenRouter."]
    assert len([p for p in read(target)["pools"] if p.get("auto_managed")]) == 1
    monitor.maybe_run_daily(control_dir=control, now=NOW + timedelta(hours=1),
                            opener=lambda *_: pytest.fail("cadence fetched"), notify_fn=notify)
    monitor.maybe_run_daily(control_dir=control, now=NOW + timedelta(hours=1), force=True,
                            opener=opener, notify_fn=notify)
    assert len(messages) == 2
    monitor.maybe_run_daily(control_dir=control, now=NOW + timedelta(days=1), force=True,
                            opener=lambda *_: {"data": []}, notify_fn=notify)
    assert messages[-2:] == ["Il modello gratuito glm-5.4:free su OpenRouter non è più disponibile.",
                            "Il modello gratuito strong:free su OpenRouter non è più disponibile."]
    assert len(read(target)["pools"]) == 1
    monitor.maybe_run_daily(control_dir=control, now=NOW + timedelta(days=2), force=True,
                            opener=lambda *_: {"data": []}, notify_fn=notify)
    assert len(messages) == 4


def test_expiry_source_failure_and_exception_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control, target = fixture(tmp_path)
    doc = read(target)
    doc["pools"].append({"name": "expired", "model": "old:free", "source_id": "openrouter_public",
                         "auto_managed": True, "expires_at": (NOW - timedelta(days=1)).isoformat()})
    target.write_text(json.dumps(doc), encoding="utf-8")
    logs: list[str] = []

    def opener(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        if url == monitor.OPENROUTER:
            raise OSError("supersecret123")
        return {"data": [{"id": "zen-free"}]}

    result = monitor.check(control_dir=control, now=NOW, opener=opener, log=logs.append)
    assert result["sources"] == ["opencode_zen"]
    assert "expired" not in {p["name"] for p in read(target)["pools"]}
    assert "zen-free" in {p["model"] for p in read(target)["pools"]}
    assert all("supersecret123" not in value for value in logs)
    monkeypatch.setattr(monitor, "check", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("supersecret123")))
    assert monitor.maybe_run_daily(control_dir=control, now=NOW + timedelta(days=2), log=logs.append)["reason"] == "error"
    assert all("supersecret123" not in value for value in logs)


def test_supervisor_hook_does_not_stop_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    relay_script = SCRIPT.with_name("relay_continuity.py")
    spec = importlib.util.spec_from_file_location("relay_continuity", relay_script)
    assert spec and spec.loader
    relay = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = relay
    spec.loader.exec_module(relay)
    road = sys.modules["relay_continuity_roadmap"]
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path / "control"))
    monkeypatch.setattr(road, "in_sandbox", lambda: True)
    monkeypatch.setattr(road, "become_subreaper", lambda: True)
    monkeypatch.setattr(road, "sandbox_provider_env", lambda _home: {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")})
    monkeypatch.setattr(road, "link_relay_claude_credentials", lambda *_args: None)
    monkeypatch.setattr(road, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(relay.shutil, "which", lambda _name: "/test/claude")
    monkeypatch.setattr(relay.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(road, "release_sha", lambda: None)
    monkeypatch.setattr(road, "check_and_request_release_update", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(road, "fast_forward_main_checkout", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(road, "_reap_zombies", lambda: None)
    calls: list[str] = []
    monkeypatch.setattr(road, "serve_step", lambda *_args, **_kwargs: calls.append("serve") or "empty")
    monkeypatch.setattr(road, "roadmap_step", lambda *_args, **_kwargs: "roadmap:waiting:jobs")
    monkeypatch.setattr(road.free_model_monitor, "maybe_run_daily", lambda **_kwargs: 1 / 0)
    assert relay.main(["serve", "--once", "--no-notify"]) == 0
    assert calls == ["serve"]


def test_expired_pool_stays_pruned_until_listing_disappears(tmp_path: Path) -> None:
    control, target = fixture(tmp_path)
    document = read(target)
    document["pools"].append({"name": "expired", "model": "strong:free", "source_id": "openrouter_public",
                              "auto_managed": True, "index_score": 42,
                              "expires_at": (NOW - timedelta(hours=1)).isoformat()})
    target.write_text(json.dumps(document), encoding="utf-8")
    alerts: list[str] = []
    state = tmp_path / "free-model-monitor-state.json"
    state.write_text(json.dumps({"alert_hits": {"openrouter_public": ["strong:free"]}}), encoding="utf-8")
    def present(url, *_):
        return {"data": [{"id": "strong:free"}]} if url == monitor.OPENROUTER else {"data": []}
    monitor.check(control_dir=control, now=NOW, opener=present,
                  notify_fn=lambda _kind, message: alerts.append(message))
    assert not any(p.get("auto_managed") for p in read(target)["pools"])
    assert alerts == ["Il modello gratuito strong:free su OpenRouter non è più disponibile."]
    monitor.check(control_dir=control, now=NOW + timedelta(days=1), opener=present,
                  notify_fn=lambda _kind, message: alerts.append(message))
    assert len(alerts) == 1
    assert not any(p.get("auto_managed") for p in read(target)["pools"])


def test_failed_delivery_retries_and_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control, target = fixture(tmp_path)
    replacements: list[tuple[Path, Path]] = []
    original_replace = monitor.os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(monitor.os, "replace", replace)
    def opener(url, *_):
        return {"data": [{"id": "strong:free"}]} if url == monitor.OPENROUTER else {"data": []}
    monitor.check(control_dir=control, now=NOW, opener=opener, notify_fn=lambda *_: False)
    assert {destination for _, destination in replacements} == {target, tmp_path / "free-model-monitor-state.json"}
    assert all(source.parent == destination.parent and source != destination for source, destination in replacements)
    alerts: list[str] = []
    monitor.check(control_dir=control, now=NOW + timedelta(hours=1), opener=opener,
                  notify_fn=lambda _kind, message: alerts.append(message))
    assert alerts == ["Nuovo modello gratuito disponibile: strong:free su OpenRouter."]


def test_source_failure_keeps_previous_pool_and_alert_state(tmp_path: Path) -> None:
    control, target = fixture(tmp_path)
    alerts: list[str] = []
    def notify(_kind, message):
        return alerts.append(message)
    def good(url, *_):
        return {"data": [{"id": "strong:free"}]} if url == monitor.OPENROUTER else {"data": []}
    monitor.check(control_dir=control, now=NOW, opener=good, notify_fn=notify)
    assert len(alerts) == 1

    def broken(url: str, *_args: Any) -> dict[str, Any]:
        if url == monitor.OPENROUTER:
            raise OSError("private credential")
        return {"data": []}

    monitor.check(control_dir=control, now=NOW + timedelta(days=1), opener=broken, notify_fn=notify)
    assert len(alerts) == 1
    assert "strong:free" in {p["model"] for p in read(target)["pools"]}
    assert read(tmp_path / "free-model-monitor-state.json")["alert_hits"]["openrouter_public"] == ["strong:free"]


def test_offline_host_retries_hourly_not_every_loop(tmp_path: Path) -> None:
    control, _target = fixture(tmp_path)

    def offline(*_args: Any) -> dict[str, Any]:
        raise OSError("network unreachable")

    assert monitor.maybe_run_daily(control_dir=control, now=NOW, opener=offline)["checked"]
    state = read(tmp_path / "free-model-monitor-state.json")
    assert "last_checked_at" not in state and state["last_attempt_at"] == NOW.isoformat()
    assert monitor.maybe_run_daily(control_dir=control, now=NOW + timedelta(minutes=30),
                                   opener=lambda *_: pytest.fail("retried while offline"))["reason"] == "retry_backoff"
    result = monitor.maybe_run_daily(control_dir=control, now=NOW + timedelta(hours=1, minutes=1),
                                     opener=lambda url, *_: {"data": [{"id": "zen-free"}]} if url == monitor.ZEN
                                     else offline())
    assert result["sources"] == ["opencode_zen"]
