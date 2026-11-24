"""Offline account catalog and launch identity tests for spec 360."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_agy_worker_221 import Env, worker
from test_relay_argon_223 import stream, write_native
from test_relay_continuity import rc

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "account_model_resolution.py"
SPEC = importlib.util.spec_from_file_location("account_model_resolution", SCRIPT)
assert SPEC and SPEC.loader
models = sys.modules.get("account_model_resolution") or importlib.util.module_from_spec(SPEC)
if "account_model_resolution" not in sys.modules:
    sys.modules["account_model_resolution"] = models
    SPEC.loader.exec_module(models)


def document(*ids: str) -> bytes:
    return json.dumps({"models": [{"id": mid} for mid in ids]}).encode()


@pytest.mark.parametrize(("role", "candidates", "wanted"), [
    ("worker_flash", ("gemini-3.8-flash-low", "gemini-4-flash-low", "gemini-4.1-flash-low"),
     "gemini-4.1-flash-low"),
    ("worker_argon-high", ("gemini-4-argon-high", "gemini-5-argon-high", "gemini-5.2-argon-high"),
     "gemini-5.2-argon-high"),
    ("worker_agy-opus", ("claude-opus-5-5-high", "claude-opus-6-1-high"), "claude-opus-6-1-high"),
    ("worker_luna", ("gpt-6-luna", "gpt-6.1-luna", "gpt-7-luna"), "gpt-7-luna"),
])
def test_numeric_order_for_exact_role(monkeypatch: pytest.MonkeyPatch, role: str,
                                      candidates: tuple[str, ...], wanted: str) -> None:
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: frozenset(candidates))
    assert models.resolve(role, "unused", {}) == wanted


def test_family_effort_preview_and_aliases_cannot_outrank(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = frozenset(("gemini-4-argon-low", "gemini-9-argon-high", "gemini-9-argon-low-preview",
                     "gemini-10-argon2-low", "gemini-latest-argon-low", "gemini-8-argon-low-extra"))
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: ids)
    assert models.resolve("coordinator_argon", "unused", {}) == "gemini-4-argon-low"
    for exact in ("gemini-9-argon-high", "gemini-9-argon-low-preview", "gemini-10-argon2-low"):
        with pytest.raises(models.Refused, match="model_not_authorized"):
            models.resolve("coordinator_argon", "unused", {}, exact=exact)


@pytest.mark.parametrize("raw", [b"", b"{}", b'{"models":"wrong"}', b'{"models":[{"id":1}]}',
                                     b'{"models":[{"id":"x"},{"id":"x"}]}', b"\xff", b"bad prose"])
def test_malformed_catalog_refused(raw: bytes) -> None:
    with pytest.raises(models.Refused, match="catalog_malformed"):
        models.parse_catalog(raw, "agy")


def test_plain_catalog_exact_lines_only() -> None:
    assert models.parse_catalog(b"gemini-4-argon-low\ngemini-4.1-argon-low\n", "agy") == {
        "gemini-4-argon-low", "gemini-4.1-argon-low"}
    with pytest.raises(models.Refused, match="catalog_malformed"):
        models.parse_catalog(b"Available models: gemini-4-argon-low", "agy")


def test_tab_separated_catalog_parses_real_agy_models(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = (
        b"gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
        b"gemini-4-flash-low\tGemini 4 Flash (Low)\n"
        b"gemini-4.1-flash-low\tGemini 4.1 Flash (Low)\n"
        b"gemini-4-flash-medium\tGemini 4 Flash (Medium)\n"
        b"gemini-4-argon-low\tGemini 4 Argon (Low)\n"
        b"gemini-5.2-argon-high\tGemini 5.2 Argon (High)\n"
        b"claude-opus-6-1-high\tClaude Opus 6.1 (High)\n"
        b"claude-sonnet-4-5-high\tClaude Sonnet 4.5 (High)\n"
    )
    parsed = models.parse_catalog(raw, "agy")
    assert parsed == {
        "gemini-3.8-flash-low",
        "gemini-4-flash-low",
        "gemini-4.1-flash-low",
        "gemini-4-flash-medium",
        "gemini-4-argon-low",
        "gemini-5.2-argon-high",
        "claude-opus-6-1-high",
        "claude-sonnet-4-5-high",
    }
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: parsed)
    assert models.resolve("worker_flash", "unused", {}) == "gemini-4.1-flash-low"
    assert models.resolve("worker_flash-medium", "unused", {}) == "gemini-4-flash-medium"
    assert models.resolve("worker_argon", "unused", {}) == "gemini-4-argon-low"
    assert models.resolve("worker_argon-high", "unused", {}) == "gemini-5.2-argon-high"
    assert models.resolve("worker_agy-opus", "unused", {}) == "claude-opus-6-1-high"
    assert models.resolve("worker_agy-sonnet", "unused", {}) == "claude-sonnet-4-5-high"


def test_tab_separated_catalog_allows_mixed_lines_and_max_length_name() -> None:
    long_name = "X" * 160
    raw = f"gemini-4-argon-low\t{long_name}\ngemini-4.1-argon-low\n".encode()
    assert models.parse_catalog(raw, "agy") == {"gemini-4-argon-low", "gemini-4.1-argon-low"}


@pytest.mark.parametrize("raw", [
    b"gemini-4-neon-medium\t",
    b"gemini-4-neon-medium\t\n",
    b"gemini-4-neon-medium\t   \n",
    b"gemini-4-neon-medium\tGemini 4 Neon\textra\n",
    b"gemini-4-neon-medium\t\tGemini 4 Neon\n",
    b"\tgemini-4-neon-medium\tGemini 4 Neon\n",
    b"GEMINI-4-FLASH\tGemini 4 Flash\n",
    b"bad_id\tGemini 4 Flash\n",
    b"-bad-id\tGemini 4 Flash\n",
    b"g\tGemini 4 Flash\n",
    b"\tGemini 4 Flash\n",
    b"gemini 4 flash\tGemini 4 Flash\n",
    b"Available models:\n",
    b"Available models:\tGemini 4 Flash\n",
    b"gemini-4-flash\tGemini 4 Flash\nError: rate limit exceeded\n",
    b"gemini-4-flash\tGemini\x00Flash\n",
    b"gemini-4-flash\tGemini\x1bFlash\n",
    b"gemini-4-flash\tGemini\x08Flash\n",
    b"gemini-4-flash\t" + (b"X" * 161) + b"\n",
    b"gemini-4-flash\tGemini 4 Flash\n\ngemini-5-flash\tGemini 5 Flash\n",
    b"\ngemini-4-flash\tGemini 4 Flash\n",
    b"gemini-4-flash\tGemini 4 Flash\n\n",
    b"gemini-4-flash\tGemini 4 Flash\ngemini-4-flash\tGemini 4 Flash Beta\n",
])
def test_malformed_tab_catalog_refused(raw: bytes) -> None:
    with pytest.raises(models.Refused, match="catalog_malformed"):
        models.parse_catalog(raw, "agy")


def test_tab_lines_refused_for_non_agy_cli() -> None:
    with pytest.raises(models.Refused, match="catalog_malformed"):
        models.parse_catalog(b"gpt-6-luna\tGPT 6 Luna\n", "codex")


def test_explicit_stale_catalog_is_refused() -> None:
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with pytest.raises(models.Refused, match="catalog_stale"):
        models.parse_catalog(json.dumps({"as_of": old, "models": [{"id": "gpt-6-luna"}]}).encode(), "codex")


def test_missing_catalog_never_uses_old_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: frozenset())
    with pytest.raises(models.Refused, match="catalog_no_match"):
        models.resolve("worker_flash", "unused", {})
    with pytest.raises(models.Refused, match="role_not_allowed"):
        models.resolve("worker_unknown", "unused", {})


def test_no_cross_account_or_stale_reuse(tmp_path: Path) -> None:
    fake = tmp_path / "agy"
    reading = tmp_path / "reading"
    fake.write_text(f"#!/bin/sh\ncat {reading}\n")
    fake.chmod(0o755)
    reading.write_bytes(document("gemini-4-argon-low"))
    assert models.resolve("coordinator_argon", str(fake), {"HOME": str(tmp_path / "a")}) == "gemini-4-argon-low"
    reading.write_bytes(document("gemini-5-argon-low"))
    assert models.resolve("coordinator_argon", str(fake), {"HOME": str(tmp_path / "b")}) == "gemini-5-argon-low"
    reading.unlink()
    with pytest.raises(models.Refused, match="catalog_unreadable"):
        models.resolve("coordinator_argon", str(fake), {"HOME": str(tmp_path / "b")})


def test_codex_app_server_catalog_is_metadata_only(tmp_path: Path) -> None:
    fake = tmp_path / "codex"
    calls = tmp_path / "calls"
    fake.write_text("#!/usr/bin/env python3\nimport json, sys\nfrom pathlib import Path\n"
                    f"out = Path({str(calls)!r})\n"
                    "assert sys.argv[1:] == ['app-server']\n"
                    "first = json.loads(sys.stdin.readline()); out.write_text(first['method'] + '\\n')\n"
                    "print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
                    "second = json.loads(sys.stdin.readline()); third = json.loads(sys.stdin.readline())\n"
                    "out.write_text(out.read_text() + second['method'] + '\\n' + third['method'] + '\\n')\n"
                    "print(json.dumps({'id': 2, 'result': {'data': [{'id': 'gpt-6-luna'}, "
                    "{'id': 'gpt-6.1-luna'}]}}), flush=True)\n")
    fake.chmod(0o755)
    assert models.resolve("worker_luna", str(fake), {"HOME": str(tmp_path)}) == "gpt-6.1-luna"
    assert calls.read_text().splitlines() == ["initialize", "initialized", "model/list"]


def test_worker_router_selects_verified_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    wr = rc.worker_routing
    pools = {"gemini": wr.Pool("gemini", {"5h": .9}, .9),
             "codex": wr.Pool("codex", {"5h": .9}, .9)}

    def resolve(model: object) -> str:
        if model.key == "flash":
            raise models.Refused("catalog_no_match")
        if model.key == "luna":
            return "gpt-7-luna"
        raise models.Refused("catalog_unavailable")

    decision = wr.decide("low", "low", pools, model_resolver=resolve)
    assert decision["worker"] == "luna" and decision["model"] == "gpt-7-luna"
    assert "--model gpt-7-luna" in decision["command"]
    assert decision["pool"] == "codex" and decision["review"] == "tests_only"


def test_existing_nonversioned_worker_fallback_remains_available(monkeypatch: pytest.MonkeyPatch) -> None:
    wr = rc.worker_routing
    monkeypatch.setenv("JARVISOS_ACCOUNT_MODEL_RESOLUTION", "1")
    monkeypatch.setattr(wr, "resolve_worker_catalog", lambda model: model.model if model.kind == "copilot"
                        else (_ for _ in ()).throw(models.Refused("catalog_unavailable")))
    pools = {"copilot": wr.Pool("copilot", {"5h": .9}, .9)}
    decision = wr.decide("low", "low", pools)
    assert decision["worker"] == "copilot-auto" and decision["model"] == "auto"


def test_relay_codex_exact_model_and_resume_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_ACCOUNT_MODEL_RESOLUTION", "1")
    monkeypatch.setattr(rc.relay_argon.account_models, "catalog", lambda *a, **kw: frozenset({"gpt-6-sol", "gpt-6.1-sol"}))
    monkeypatch.setattr(rc.RelayAdapter.start.__globals__["shutil"], "which", lambda name: "/fake/codex")
    spawned = []

    class Proc:
        pid = 123
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def popen(args, **kwargs):
        spawned.append(args)
        kwargs["stdout"].write(json.dumps({"session_id": "test-session"}))
        return Proc()

    monkeypatch.setattr(rc.RelayAdapter.start.__globals__["subprocess"], "Popen", popen)
    repo = tmp_path / "repo"
    turn = repo / ".agent-relay/sessions/test-session/turns/turn-001"
    turn.mkdir(parents=True)
    nonce = "rc-20261124T000000Z-abcdef12"
    (turn / "prompt.md").write_text(f"header\n## Task\n[relay-continuity attempt {nonce}]\n")
    adapter = rc.RelayAdapter(repo)
    proc = adapter.start("codex", None, f"[relay-continuity attempt {nonce}]\ntask", 1)
    assert spawned[0][-2:] == ["--model", "gpt-6.1-sol"]
    assert adapter.finish(proc).raw_ok
    assert json.loads((repo / ".agent-relay/sessions/test-session/selected-model.json").read_text()) == {
        "model": "gpt-6.1-sol"}
    newer = "rc-20261124T000001Z-abcdef12"
    (turn / "prompt.md").write_text(f"header\n## Task\n[relay-continuity attempt {newer}]\n")
    proc = adapter.start("codex", None, f"[relay-continuity attempt {newer}]\ntask", 1)
    assert adapter.finish(proc).diagnostics == "model_record_failed"
    assert json.loads((repo / ".agent-relay/sessions/test-session/selected-model.json").read_text()) == {
        "model": "gpt-6.1-sol"}
    monkeypatch.setattr(rc.relay_argon.account_models, "catalog", lambda *a, **kw: frozenset({"gpt-6-sol"}))
    with pytest.raises(OSError, match="model_not_authorized"):
        adapter.start("codex", "test-session", "resume", 1)
    assert len(spawned) == 2


def test_worker_exact_selection_refuses_catalog_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setenv("JARVISOS_ACCOUNT_MODEL_RESOLUTION", "1")
    env.set(models_out=document("gemini-4-flash-low").decode())
    result = env.run()
    assert result["status"] == "succeeded" and result["model"] == "gemini-4-flash-low"
    env.clean()
    env.log.unlink()
    env.set(models_out=document("gemini-5-flash-low").decode())
    args = type("Args", (), {"task_file": str(env.task), "worktree": str(env.wt), "model": "flash",
                             "timeout": 30, "evidence_dir": str(env.evidence), "coordinator_fallback": False,
                             "resolved_model": "gemini-4-flash-low"})()
    _, result = worker.execute(args, sandbox_check=lambda: True)
    assert result["status"] == "refused" and result["reason"] == "model_not_authorized"
    assert not env.invoked()


def test_worker_resume_requires_recorded_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setenv("JARVISOS_ACCOUNT_MODEL_RESOLUTION", "1")
    args = type("Args", (), {"task_file": str(env.task), "worktree": str(env.wt), "model": "flash",
                             "timeout": 30, "evidence_dir": str(env.evidence), "coordinator_fallback": False,
                             "resume_conversation": "00000000-0000-0000-0000-000000000000",
                             "resolved_model": None})()
    _, result = worker.execute(args, sandbox_check=lambda: True)
    assert result["status"] == "refused" and result["reason"] == "resume_model_missing"
    assert not env.invoked()


def test_native_evidence_accepts_only_recorded_exact_id(tmp_path: Path) -> None:
    events = stream()
    events[0]["init"]["model"] = "gemini-5.2-argon-low"
    turn = write_native(tmp_path, events, meta={"model": "gemini-5.2-argon-low", "catalog_verified": True})
    assert rc.classify_turn(turn).outcome == "success"
    events[0]["init"]["model"] = "gemini-5.3-argon-low"
    (turn / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert rc.classify_turn(turn).outcome != "success"
