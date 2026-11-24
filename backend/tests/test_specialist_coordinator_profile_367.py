"""Offline mission and account catalog evidence for spec 367."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_account_model_resolution_360 import models
from test_relay_continuity import CODEX_SUCCESS, Env, mission_def, rc, rc_missions, room, run_mission, start_mission


def mission() -> dict:
    return {
        "schema": rc.MISSION_SCHEMA,
        "id": "m-20261124T000000Z-aabbcc",
        "authority": "L20261124000000-aabb",
        "submitted_at": "2026-11-24T00:00:00+00:00",
        "tasks": [{"text": "Complete the migration"}],
    }


def test_mission_profile_validation_and_existing_definition() -> None:
    definition = mission()
    assert rc_missions.validate_mission(definition) is None
    for profile in ("standard", "specialist"):
        assert rc_missions.validate_mission({**definition, "coordinator_profile": profile}) is None
    for profile in (None, "", "other", 1, True, [], {}):
        assert rc_missions.validate_mission({**definition, "coordinator_profile": profile}) == "bad coordinator_profile"


def test_role_resolution_and_bounded_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert models.coordinator_role_name("codex") == "coordinator_codex"
    assert models.coordinator_role_name("codex", "specialist") == "coordinator_codex_specialist"
    assert models.coordinator_role_name("argon", "specialist") == "coordinator_argon"
    with pytest.raises(models.Refused, match="profile_not_allowed"):
        models.coordinator_role_name("codex", "expert")
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: frozenset({"gpt-6-sol", "gpt-6-astra", "gpt-6.1-astra"}))
    assert models.resolve("coordinator_codex_specialist", "unused", {}) == "gpt-6.1-astra"
    assert models.resolve("coordinator_codex", "unused", {}) == "gpt-6-sol"
    with pytest.raises(models.Refused, match="catalog_no_match"):
        models.resolve("coordinator_codex_specialist", "unused", {"JARVISOS_COORDINATOR_SPECIALIST_STEM": "gpt-other"})
    with pytest.raises(models.Refused, match="role_stem_not_allowed"):
        models.resolve("coordinator_codex_specialist", "unused", {"JARVISOS_COORDINATOR_SPECIALIST_STEM": "gpt-*"})


def test_specialist_launch_resume_and_profile_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_ACCOUNT_MODEL_RESOLUTION", "1")
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: frozenset({"gpt-6-sol", "gpt-6-astra", "gpt-6.1-astra"}))
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
    adapter = rc.RelayAdapter(repo)
    nonce = "rc-20261124T000000Z-abcdef12"
    (turn / "prompt.md").write_text(f"header\n## Task\n[relay-continuity attempt {nonce}]\n")
    proc = adapter.start("codex", None, f"[relay-continuity attempt {nonce}]", 1,
                         coordinator_profile="specialist")
    assert spawned[-1][-2:] == ["--model", "gpt-6.1-astra"]
    assert adapter.finish(proc).raw_ok
    record = repo / ".agent-relay/sessions/test-session/selected-model.json"
    assert json.loads(record.read_text()) == {"model": "gpt-6.1-astra", "coordinator_profile": "specialist"}

    newer = "rc-20261124T000001Z-abcdef12"
    (turn / "prompt.md").write_text(f"header\n## Task\n[relay-continuity resume {newer}]\n")
    proc = adapter.start("codex", "test-session", f"[relay-continuity resume {newer}]", 1,
                         coordinator_profile="specialist")
    assert spawned[-1][-2:] == ["--model", "gpt-6.1-astra"]
    assert adapter.finish(proc).raw_ok
    with pytest.raises(rc.CatalogRefused, match="resume_profile_mismatch"):
        adapter.start("codex", "test-session", "resume", 1)
    assert len(spawned) == 2

    monkeypatch.setattr(models, "catalog", lambda *a, **kw: frozenset({"gpt-6.2-astra"}))
    with pytest.raises(rc.CatalogRefused, match="model_not_authorized"):
        adapter.start("codex", "test-session", "resume", 1, coordinator_profile="specialist")
    assert len(spawned) == 2


def test_specialist_catalog_absence_refuses_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_ACCOUNT_MODEL_RESOLUTION", "1")
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: frozenset({"gpt-6-sol"}))
    monkeypatch.setattr(rc.RelayAdapter.start.__globals__["shutil"], "which", lambda name: "/fake/codex")
    adapter = rc.RelayAdapter(tmp_path)
    with pytest.raises(rc.CatalogRefused, match="catalog_no_match"):
        adapter.start("codex", None, "task", 1, coordinator_profile="specialist")
    with pytest.raises(rc.CatalogRefused, match="profile_not_allowed"):
        adapter.start("codex", None, "task", 1, coordinator_profile="other")


def test_limits_preflight_checks_selected_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_ACCOUNT_MODEL_RESOLUTION", "1")
    monkeypatch.setattr(models, "catalog", lambda *a, **kw: frozenset({"gpt-6-sol"}))
    monkeypatch.setattr(rc.RelayAdapter.start.__globals__["shutil"], "which", lambda name: "/fake/codex")

    class LimitsResult:
        stdout = json.dumps({"agents": [{"agent_key": "codex", "status": "ok"}]})

    monkeypatch.setattr(rc.RelayAdapter.start.__globals__["subprocess"], "run", lambda *a, **kw: LimitsResult())
    adapter = rc.RelayAdapter(tmp_path)
    assert "codex" in adapter.limits()
    assert "codex" not in adapter.limits(coordinator_profile="specialist")


def test_existing_mission_store_defaults_to_standard(tmp_path: Path) -> None:
    definition = mission()
    root = tmp_path / "control"
    path = root / "missions" / f"{definition['id']}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(definition))
    loaded = rc_missions.load_missions(root)
    assert loaded == [(definition, definition["id"], None)]
    assert loaded[0][0].get("coordinator_profile", "standard") == "standard"
    store = rc_missions.mission_store(root, definition["id"])
    state = rc.default_state()
    state["mission"] = {"id": definition["id"], "next": 0, "total": 1, "done": []}
    store.save(state, rc.now_utc())
    status, restored = rc_missions.mission_status(store)
    assert status == "queued"
    assert restored is not None and not rc_missions.mission_mismatch(definition, definition["id"], restored)


def test_supervisor_threads_profile_from_mission_to_launch_and_limits(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    readings = room(env, "codex")
    profiles = []

    def limits(coordinator_profile="standard"):
        profiles.append(coordinator_profile)
        return readings()

    env.relay.limits = limits
    definition = mission_def("complete migration")
    definition["coordinator_profile"] = "specialist"
    start_mission(env, definition)
    state = env.store.load()
    state["agent"] = "codex"
    env.store.save(state, env.clock.now())
    result, _supervisor = run_mission(env, definition, session=None)
    assert result == "idle"
    assert "specialist" in profiles
    assert env.relay.launches[0]["coordinator_profile"] == "specialist"
    launches = [json.loads(line) for line in env.store.journal_path.read_text().splitlines()
                if json.loads(line).get("event") == "launch"]
    assert launches[0]["coordinator_profile"] == "specialist"
