"""223: native coordinator evidence, fallback ordering and fail-closed subscription transport."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_relay_continuity import SUCCESS, T0, Env, _write_mission, mission_def, rc, reading

ra = rc.relay_argon


@pytest.fixture(autouse=True)
def _clean_lease_env(monkeypatch):
    monkeypatch.delenv("JARVISOS_ARGON_LEASE_FD", raising=False)


def stream(command: str = "pwd") -> list[dict]:
    def step(state: str) -> dict:
        return {"event": "step_update", "step_update": {"step_index": 2, "state": state, "conversation_id": "native-test",
                "step_type": "tool", "tool_name": "run_command", "tool_info": {
                    "parameters": {"CommandLine": command}, "output": "Command exited with code 0.\n"}}}
    return [{"event": "init", "conversation_id": "native-test", "init": {"model": ra.MODEL}}, step("ACTIVE"), step("DONE"),
            {"event": "result", "result": {"conversation_id": "native-test", "status": "SUCCESS", "response": 'RELAY_STATUS: {"status":"propose_done"}' }}]


def write_native(tmp: Path, events: list[dict], *, meta: dict | None = None) -> Path:
    turn = tmp / "sessions/20261027-075000-112233/turns/turn-001"
    turn.mkdir(parents=True)
    (turn / "state.json").write_text(json.dumps({"agent_key": "argon", "status": "continue"}))
    (turn / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    metadata = {"schema": "argon-coordinator.v1", "launched": True, "exit_code": 0, "quota": [], "reason": None, "model": ra.MODEL}
    metadata.update(meta or {})
    (turn / "native-result.json").write_text(json.dumps(metadata))
    return turn


def quota() -> list[dict]:
    return [{"id": ident, "remaining_fraction": 0.8, "reset_time": rc.iso(T0 + timedelta(hours=1))}
            for ident in sorted(ra.REQUIRED_BUCKETS)]


def mock_probe(monkeypatch, reading: list[dict]) -> None:
    monkeypatch.setattr(ra, "sandbox", lambda: True)
    monkeypatch.setattr(ra, "prepare_login", lambda: None)
    monkeypatch.setattr(ra, "subscription_settings", lambda: None)
    monkeypatch.setattr(ra.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({"status": "SUCCESS", "command": {"data": {"groups": [
            {"buckets": reading}]}}})))


def test_native_success_is_execution_evidence_not_just_exit_zero(tmp_path):
    report = rc.classify_turn(write_native(tmp_path, stream()))
    assert report.outcome == "success"
    assert report.agent == "argon"
    assert report.calls[0].done and report.calls[0].kind == "read_only"


@pytest.mark.parametrize("change", ["denied", "empty", "exit", "missing-meta", "incomplete", "error-tool",
                                   "torn", "trailing", "mutated", "reopened", "background", "async"])
def test_native_incomplete_or_ambiguous_work_never_succeeds(tmp_path, change):
    events = stream()
    meta = {}
    if change == "denied":
        events[-1]["result"]["denied_actions"] = [{"action": "command"}]
    elif change == "empty":
        events[-1]["result"]["response"] = ""
    elif change == "exit":
        meta["exit_code"] = 1
    elif change == "incomplete":
        events.pop(2)
    elif change == "error-tool":
        # An external call with error metadata stays strict (a failed local or read-only call does not: see below).
        events = stream("gh pr merge 1")
        events[2]["step_update"]["error"] = "failed"
    elif change == "trailing":
        events.append(events[1])
    elif change == "mutated":
        events[2]["step_update"]["tool_info"]["parameters"]["CommandLine"] = "gh pr merge 1"
    elif change == "reopened":
        events.insert(3, events[1])
    elif change == "background":
        events = stream("gh pr merge 1 &")
    elif change == "async":
        for item in events[1:3]:
            item["step_update"]["tool_info"]["parameters"]["WaitMsBeforeAsync"] = 1
    turn = write_native(tmp_path, events, meta=meta)
    if change == "missing-meta":
        (turn / "native-result.json").unlink()
    if change == "torn":
        with (turn / "output.jsonl").open("a") as handle:
            handle.write('{"event":')
    assert rc.classify_turn(turn).outcome != "success"


def test_unknown_native_interrupted_tool_keeps_external_uncertainty(tmp_path):
    events = stream()[:2]
    events[1]["step_update"]["tool_name"] = "future_remote_tool"
    report = rc.classify_turn(write_native(tmp_path, events))
    assert report.outcome == "unknown"
    assert len(report.in_flight) == 1
    assert rc.external(report.in_flight[0])
    env = Env(tmp_path / "env")
    verdicts, parked = rc.Supervisor(env.store, env.host)._settle_external(report)
    assert verdicts == {} and [c.label for c in parked] == ["future_remote_tool"]  # spec 254: parked, never settled


def test_nonce_recovery_uses_real_prompt_without_provider_parent(tmp_path):
    adapter = rc.RelayAdapter(tmp_path)
    nonce = "rc-20261027T075000Z-11223344"
    sid, _ = ra.new_session(tmp_path, f"[relay-continuity attempt {nonce}]\nReal task")
    assert adapter.find_by_nonce(nonce, rc.now_utc()) == sid


@pytest.mark.parametrize("bad", [True, -0.1, 1.1, float("nan"), float("inf"), None, "1"])
def test_quota_rejects_invalid_numeric_fraction(monkeypatch, bad):
    reading = quota()
    reading[0]["remaining_fraction"] = bad
    mock_probe(monkeypatch, reading)
    with pytest.raises(ra.Refused, match="quota_unreadable"):
        ra.read_quota("agy")


@pytest.mark.parametrize("change", ["missing", "duplicate", "disabled", "exhausted"])
def test_quota_requires_all_and_only_applicable_room(monkeypatch, change):
    reading = quota()
    if change == "missing":
        reading.pop()
    elif change == "duplicate":
        reading.append(reading[0])
    elif change == "disabled":
        reading[0]["disabled"] = True
    else:
        reading[0]["remaining_fraction"] = 0
    mock_probe(monkeypatch, reading)
    with pytest.raises(ra.Refused):
        ra.require_room(ra.read_quota("agy"))


def test_quota_rejects_nonzero_or_error_even_with_buckets(monkeypatch):
    mock_probe(monkeypatch, quota())
    monkeypatch.setattr(ra.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout=json.dumps({"status": "SUCCESS", "buckets": quota()})))
    with pytest.raises(ra.Refused, match="quota_unreadable"):
        ra.read_quota("agy")


def test_last_resort_returns_to_primary_as_soon_as_it_recovers(tmp_path, monkeypatch):
    env = Env(tmp_path)
    sup = rc.Supervisor(env.store, env.host)
    allowed = {"argon"}
    monkeypatch.setattr(sup, "available", lambda agent: agent in allowed)
    assert sup._choose("claude") == "argon"
    allowed.add("codex")
    assert sup._choose("argon") == "argon"
    allowed.add("claude")
    assert sup._choose("argon") == "argon"
    assert sup._choose("codex") == "claude"  # recover from fallback


def test_native_exhaustion_preserves_exact_reset(tmp_path, monkeypatch):
    env = Env(tmp_path)
    reset = rc.iso(T0 + timedelta(hours=2))
    monkeypatch.setattr(env.relay, "argon_limits", lambda agent: {"windows": [
        {"used_percent": 100, "resets_at": reset}, {"used_percent": 10, "resets_at": reset}]})
    sup = rc.Supervisor(env.store, env.host)
    assert not sup.available("argon")
    assert sup.state["cooldowns"]["argon"]["until"] == reset


@pytest.mark.parametrize("config", [{"useG1Credits": True}, {"useG1Credits": 0}, {"modelProvider": "gemini"},
                                    {"modelProvider": "custom"}])
def test_coordinator_refuses_credit_or_api_route_without_mutation(tmp_path, config):
    path = tmp_path / ".gemini/antigravity-cli/settings.json"
    path.parent.mkdir(parents=True)
    original = json.dumps(config)
    path.write_text(original)
    with pytest.raises(ra.Refused):
        ra.subscription_settings(tmp_path)
    assert path.read_text() == original


def test_login_link_never_overwrites_existing_state(tmp_path):
    target = tmp_path / ".local/share/jarvisos-sandbox/state/agy-home"
    target.mkdir(parents=True)
    ra.prepare_login(tmp_path)
    assert (tmp_path / ".gemini").resolve() == target
    ra.prepare_login(tmp_path)
    (tmp_path / ".gemini").unlink()
    (tmp_path / ".gemini").mkdir()
    with pytest.raises(ra.Refused, match="login_path_conflict"):
        ra.prepare_login(tmp_path)


def test_runner_refuses_outside_mission_sandbox_before_model(tmp_path, monkeypatch):
    sid, turn = ra.new_session(tmp_path, "task")
    monkeypatch.setattr(ra, "sandbox", lambda: False)
    monkeypatch.setattr(ra.subprocess, "run", lambda *a, **kw: pytest.fail("must not call provider"))
    assert ra.run(tmp_path, sid) == 1
    data = json.loads((turn / "native-result.json").read_text())
    assert data["reason"] == "not_mission_sandbox" and data["launched"] is False


def test_native_runner_and_parent_share_lease_until_reaper(tmp_path, monkeypatch):
    adapter = rc.RelayAdapter(tmp_path)
    seen = {}
    fake = SimpleNamespace()
    def popen(args, **kwargs):
        seen.update(args=args, **kwargs)
        return fake
    monkeypatch.setattr(rc.subprocess, "Popen", popen)
    proc = adapter.start("argon", "old-provider-session", "task", 3)
    assert "--continue" not in seen["args"]
    assert rc.coordinator_lease_held(tmp_path)
    assert seen["pass_fds"] == (int(seen["env"]["JARVISOS_ARGON_LEASE_FD"]),)
    adapter.release_turn(proc)
    assert not rc.coordinator_lease_held(tmp_path)
    for file in adapter._outputs.pop(id(proc)):
        file.close()


@pytest.mark.parametrize("change", ["wrong-model", "mixed-step", "mixed-result", "scalar", "missing-init"])
def test_native_identity_and_protocol_integrity(tmp_path, change):
    events = stream()
    if change == "wrong-model":
        events[0]["init"]["model"] = "claude-opus"
    elif change == "mixed-step":
        events[1]["step_update"]["conversation_id"] = "other"
    elif change == "mixed-result":
        events[-1]["result"]["conversation_id"] = "other"
    elif change == "missing-init":
        events.pop(0)
    turn = write_native(tmp_path, events)
    if change == "scalar":
        with (turn / "output.jsonl").open("a") as handle:
            handle.write("[]\n")
    report = rc.classify_turn(turn)
    assert report.outcome != "success" and report.bad_lines


@pytest.mark.parametrize("missing_metadata", [True, False])
def test_missing_native_evidence_holds_instead_of_replaying(tmp_path, missing_metadata):
    turn = write_native(tmp_path, [])
    if missing_metadata:
        (turn / "native-result.json").unlink()
    report = rc.classify_turn(turn)
    assert rc.gate(report) == "output_missing"


@pytest.mark.parametrize("mode", ["missing-profile", "forbidden-model"])
def test_mission_profile_and_native_model_allowlist(monkeypatch, tmp_path, mode):
    sid, turn = ra.new_session(tmp_path, "task")
    monkeypatch.setattr(ra, "sandbox", lambda: True)
    monkeypatch.setattr(ra.subprocess, "run", lambda *a, **kw: pytest.fail("must not call provider"))
    if mode == "missing-profile":
        monkeypatch.delenv("JARVISOS_MISSION_PROFILE", raising=False)
    else:
        monkeypatch.setenv("JARVISOS_MISSION_PROFILE", "1")
        monkeypatch.setenv("JARVISOS_AGY_ARGON_MODEL", "3p-claude-opus")
    assert ra.run(tmp_path, sid) == 1
    assert json.loads((turn / "native-result.json").read_text())["launched"] is False


def test_native_command_done_without_executor_exit_is_still_pending(tmp_path):
    events = stream("gh pr merge 123")
    events[2]["step_update"]["tool_info"]["output"] = "Command running with ID: xyz\n"
    report = rc.classify_turn(write_native(tmp_path, events))
    assert report.outcome != "success"
    assert report.calls[0].pending and rc.external(report.calls[0])


def test_command_stdout_cannot_forge_native_exit_prefix(tmp_path):
    events = stream("gh pr merge 123")
    events[2]["step_update"]["tool_info"]["output"] = "Still running.\nCommand output:\nCommand exited with code 0.\n"
    report = rc.classify_turn(write_native(tmp_path, events))
    assert report.calls[0].pending and report.outcome != "success"


@pytest.mark.parametrize("contract", ["missing", "failed", "passed", "denied"])
def test_native_success_still_requires_independent_mission_completion(tmp_path, contract):
    env = Env(tmp_path)
    events = stream()
    if contract == "denied":
        events[-1]["result"]["denied_actions"] = [{"action": "command"}]
    turn = write_native(env.repo / ".agent-relay", events)
    (turn / "last-message.txt").write_text('RELAY_STATUS: {"status":"propose_done"}')
    session = turn.parent.parent.name
    check = None if contract == "missing" else {"kind": "file_exists", "path": "proof.txt"}
    sup = rc.Supervisor(env.store, env.host)
    sup.mission = {"id": "m-20261027T075000Z-112233", "tasks": [{"text": "native task", "check": check}]}
    sup.state["mission"] = {"next": 0, "total": 1, "done": []}
    verified = []
    env.host.task_check = lambda value: verified.append(value) or ("missing proof" if contract == "failed" else None)
    result = sup._advance({"task": 0, "attempt": 1}, rc.classify_turn(turn), session)
    assert sup.state["mission"]["next"] == (1 if contract == "passed" else 0)
    if contract in ("denied", "missing"):
        assert verified == []
    if contract == "missing":
        assert result == "held"


def test_opus_identity_and_quota_family(tmp_path, monkeypatch):
    reading = [{"id": name, "remaining_fraction": 0.5} for name in ra.OPUS_BUCKETS] + quota()
    mock_probe(monkeypatch, reading)
    assert {b["id"] for b in ra.read_quota("agy", agent="agy_opus")} == ra.OPUS_BUCKETS
    mock_probe(monkeypatch, quota())
    with pytest.raises(ra.Refused, match="quota_bucket_missing"):
        ra.read_quota("agy", agent="agy_opus")
    events = stream()
    events[0]["init"]["model"] = ra.OPUS_MODEL
    turn = write_native(tmp_path, events, meta={"model": ra.OPUS_MODEL, "agent": "agy_opus"})
    (turn / "state.json").write_text(json.dumps({"agent_key": "agy_opus"}))
    report = rc.classify_turn(turn)
    assert report.outcome == "success" and report.agent == "agy_opus"
    assert report.detail == "native agy_opus result SUCCESS"
    events[0]["init"]["model"] = ra.MODEL
    (turn / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert rc.classify_turn(turn).outcome != "success"


def test_preferred_and_fallback_order(tmp_path, monkeypatch):
    env = Env(tmp_path)
    sup = rc.Supervisor(env.store, env.host)
    available = {"agy_opus", "codex"}
    monkeypatch.setattr(sup, "available", lambda agent: agent in available)
    assert sup._choose("codex") == "agy_opus"
    available.remove("agy_opus")
    assert sup._choose("agy_opus") == "codex"
    available.add("argon")
    assert sup._choose("codex") == "argon"


@pytest.mark.parametrize("change", ["missing", "duplicate", "disabled", "exhausted", "boolean", "nan"])
def test_opus_pool_rejects_unusable_shared_subscription_quota(monkeypatch, change):
    reading = [{"id": "3p-5h", "remaining_fraction": 0.5}, {"id": "3p-weekly", "remaining_fraction": 0.7}]
    if change == "missing":
        reading.pop()
    elif change == "duplicate":
        reading.append(reading[0])
    elif change == "disabled":
        reading[0]["disabled"] = True
    else:
        reading[0]["remaining_fraction"] = {"exhausted": 0, "boolean": True, "nan": float("nan")}[change]
    mock_probe(monkeypatch, reading + quota())
    with pytest.raises(ra.Refused):
        ra.require_room(ra.read_quota("agy", agent="agy_opus"))


def test_native_family_identity_rejects_unverified_agents_and_models():
    with pytest.raises(ra.Refused, match="agent_not_allowed"):
        ra.model_for("unknown", {})
    with pytest.raises(ra.Refused, match="model_not_allowed"):
        ra.model_for("agy_opus", {"JARVISOS_AGY_OPUS_MODEL": ra.MODEL})
    with pytest.raises(ra.Refused, match="model_not_allowed"):
        ra.model_for("argon", {"JARVISOS_AGY_ARGON_MODEL": ra.OPUS_MODEL})
    assert ra.model_for("agy_opus", {}) == "claude-opus-5-5-medium"


def test_adapter_keeps_native_quota_caches_separate_by_subscription_family(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rc.shutil, "which", lambda _: "agy")

    def read_quota(binary, env, *, agent):
        calls.append(agent)
        return [{"remaining_fraction": 0.4 if agent == "agy_opus" else 0.8}]

    monkeypatch.setattr(ra, "read_quota", read_quota)
    adapter = rc.RelayAdapter(tmp_path, env={"JARVISOS_MISSION_PROFILE": "1"})
    opus = adapter.argon_limits("agy_opus")
    argon = adapter.argon_limits("argon")
    assert opus["windows"][0]["used_percent"] == pytest.approx(60)
    assert argon["windows"][0]["used_percent"] == pytest.approx(20)
    assert adapter.argon_limits("agy_opus") == opus
    assert calls == ["agy_opus", "argon"]


def test_native_opus_runner_preflights_and_launches_the_same_model_family(tmp_path, monkeypatch):
    model = "claude-opus-5-5-high"
    monkeypatch.setenv("JARVISOS_MISSION_PROFILE", "1")
    monkeypatch.setenv("JARVISOS_AGY_OPUS_MODEL", model)
    monkeypatch.setattr(ra, "sandbox", lambda: True)
    monkeypatch.setattr(ra, "prepare_login", lambda: None)
    monkeypatch.setattr(ra, "subscription_settings", lambda: None)
    monkeypatch.setattr(ra.shutil, "which", lambda _: "agy")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "/quota" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "SUCCESS", "buckets": [
                {"id": "3p-5h", "remaining_fraction": 0.5}, {"id": "3p-weekly", "remaining_fraction": 0.5}]}))
        events = stream()
        events[0]["init"]["model"] = model
        kwargs["stdout"].write("".join(json.dumps(event) + "\n" for event in events))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ra.subprocess, "run", run)
    sid, turn = ra.new_session(tmp_path, "bounded task", "agy_opus")
    assert ra.run(tmp_path, sid, "agy_opus") == 0
    meta = json.loads((turn / "native-result.json").read_text())
    assert meta["agent"] == "agy_opus" and meta["model"] == model
    assert {b["id"] for b in meta["quota"]} == {"3p-5h", "3p-weekly"}
    assert calls[1][calls[1].index("--model") + 1] == model
    assert rc.classify_turn(turn).outcome == "success"


def test_hydrogen_identity_and_quota_family(tmp_path, monkeypatch):
    assert ra.model_for("hydrogen", {}) == ra.HYDROGEN_MODEL
    for level in ("low", "medium", "high"):
        override = f"gemini-4-hydrogen-{level}"
        assert ra.model_for("hydrogen", {"JARVISOS_AGY_HYDROGEN_MODEL": override}) == override
    with pytest.raises(ra.Refused, match="model_not_hydrogen"):
        ra.model_for("hydrogen", {"JARVISOS_AGY_HYDROGEN_MODEL": "gemini-4-argon-low"})
    with pytest.raises(ra.Refused, match="model_not_allowed"):
        ra.model_for("argon", {"JARVISOS_AGY_ARGON_MODEL": ra.HYDROGEN_MODEL})

    # read_quota requires exactly the two Gemini buckets (gemini-5h and gemini-weekly)
    reading = [{"id": name, "remaining_fraction": 0.5} for name in ra.HYDROGEN_BUCKETS] + [
        {"id": "gemini-argon-wtu-weekly", "remaining_fraction": 0.5},
        {"id": "3p-5h", "remaining_fraction": 0.5},
    ]
    mock_probe(monkeypatch, reading)
    assert {b["id"] for b in ra.read_quota("agy", agent="hydrogen")} == ra.HYDROGEN_BUCKETS

    # Missing bucket refuses
    mock_probe(monkeypatch, [{"id": "gemini-5h", "remaining_fraction": 0.5}])
    with pytest.raises(ra.Refused, match="quota_bucket_missing"):
        ra.read_quota("agy", agent="hydrogen")

    # Exhausted bucket refuses in require_room
    mock_probe(monkeypatch, [{"id": "gemini-5h", "remaining_fraction": 0.0}, {"id": "gemini-weekly", "remaining_fraction": 0.5}])
    with pytest.raises(ra.Refused, match="quota_exhausted"):
        ra.require_room(ra.read_quota("agy", agent="hydrogen"))

    # Stream classification and model mismatch
    events = stream()
    events[0]["init"]["model"] = ra.HYDROGEN_MODEL
    turn = write_native(tmp_path, events, meta={"model": ra.HYDROGEN_MODEL, "agent": "hydrogen"})
    (turn / "state.json").write_text(json.dumps({"agent_key": "hydrogen"}))
    report = rc.classify_turn(turn)
    assert report.outcome == "success" and report.agent == "hydrogen"
    assert report.detail == "native hydrogen result SUCCESS"

    # Criterion 5: Hydrogen turn whose stream reports an argon model is classified as model mismatch
    events[0]["init"]["model"] = ra.MODEL
    (turn / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert rc.classify_turn(turn).outcome != "success"

    # Argon turn whose stream reports hydrogen model is also classified as model mismatch
    events[0]["init"]["model"] = ra.HYDROGEN_MODEL
    argon_turn = write_native(tmp_path / "argon_test", events, meta={"model": ra.MODEL, "agent": "argon"})
    (argon_turn / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    assert rc.classify_turn(argon_turn).outcome != "success"


def test_native_hydrogen_cli_and_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVISOS_MISSION_PROFILE", "1")
    monkeypatch.setattr(ra, "sandbox", lambda: True)
    monkeypatch.setattr(ra, "prepare_login", lambda: None)
    monkeypatch.setattr(ra, "subscription_settings", lambda: None)
    monkeypatch.setattr(ra.shutil, "which", lambda _: "agy")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "/quota" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "SUCCESS", "buckets": [
                {"id": "gemini-5h", "remaining_fraction": 0.5}, {"id": "gemini-weekly", "remaining_fraction": 0.5}]}))
        events = stream()
        events[0]["init"]["model"] = ra.HYDROGEN_MODEL
        kwargs["stdout"].write("".join(json.dumps(event) + "\n" for event in events))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ra.subprocess, "run", run)
    sid, turn = ra.new_session(tmp_path, "bounded hydrogen task", "hydrogen")
    assert ra.run(tmp_path, sid, "hydrogen") == 0
    meta = json.loads((turn / "native-result.json").read_text())
    assert meta["agent"] == "hydrogen" and meta["model"] == ra.HYDROGEN_MODEL
    assert {b["id"] for b in meta["quota"]} == {"gemini-5h", "gemini-weekly"}
    assert calls[1][calls[1].index("--model") + 1] == ra.HYDROGEN_MODEL
    assert rc.classify_turn(turn).outcome == "success"


def test_completed_missions_sticky_coordinator_without_a_new_state_store(tmp_path):
    root = tmp_path / "control"
    env = Env(tmp_path, [("claude", SUCCESS, "propose_done")] * 4 + [("argon", stream(), "propose_done")])
    env.relay.readings = {
        "claude": reading("claude", 30, T0 + timedelta(days=5), T0),
        "codex": reading("codex", 20, T0 + timedelta(days=5), T0),
    }
    env.relay.argon_limits = lambda a: {"windows": [{"used_percent": 30}]} if a in ("argon", "agy_opus") else None

    def factory(store, m):
        return rc.Supervisor(store, env.host, env.limits, sender=env.sender)

    # Acceptance criterion 1: Four completed missions in a row, every agent available with room,
    # all run on the same agent as the first (claude), and no provider_switched notification is sent.
    missions = [mission_def(f"cycle {i}", mid=f"m-20261018T08000{i}Z-aabbc{i}",
                            submitted=T0 + timedelta(seconds=i)) for i in range(4)]
    for mission in missions:
        _write_mission(root, mission)

    for mission in missions:
        mid = mission["id"]
        assert rc.serve_step(root, factory, conflicts=lambda: [], now=lambda: T0) == f"ran:{mid}:idle"

    assert [launch["agent"] for launch in env.relay.launches] == ["claude", "claude", "claude", "claude"]
    assert not any("switched coordinator" in text for _, text in env.ledger)
    assert not any("cambiato assistente" in msg for msg in env.sent)

    # Sticky to a non-claude agent: when the last completed agent is argon,
    # the next mission is seeded with and stays on argon.
    m_argon = mission_def("cycle argon", mid="m-20261018T080010Z-aabbc9", submitted=T0 + timedelta(seconds=10))
    _write_mission(root, m_argon)
    store_argon = rc.mission_store(root, m_argon["id"])
    state_argon = rc.default_state()
    state_argon.update({
        "phase": "idle",
        "agent": "argon",
        "mission": {"id": m_argon["id"], "next": 1, "total": 1, "done": [{"task": 0, "outcome": "verified"}],
                    "completed_at": rc.iso(T0 + timedelta(seconds=10))},
    })
    store_argon.save(state_argon, T0 + timedelta(seconds=10))

    m_next = mission_def("cycle next", mid="m-20261018T080011Z-aabbca", submitted=T0 + timedelta(seconds=11))
    _write_mission(root, m_next)

    mid_next = m_next["id"]
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=lambda: T0 + timedelta(seconds=11)) == f"ran:{mid_next}:idle"
    assert env.relay.launches[-1]["agent"] == "argon"
    assert not any("switched coordinator" in text for _, text in env.ledger)
    assert not any("cambiato assistente" in msg for msg in env.sent)


def test_resume_preserves_the_selected_native_coordinator(tmp_path):
    root = tmp_path / "control"
    mission = mission_def("checkpointed mission")
    _write_mission(root, mission)
    store = rc.mission_store(root, mission["id"])
    state = rc.init_mission_state(store, mission, None, lambda: T0)
    state.update(agent="agy_opus", head_session="20261018-080000-112233")
    store.save(state, T0)
    captured = []

    def factory(current_store, _mission):
        captured.append(current_store.load()["agent"])
        return SimpleNamespace(run=lambda _: "stopped")

    assert rc.serve_step(root, factory, conflicts=lambda: [], now=lambda: T0).endswith(":stopped")
    assert captured == ["agy_opus"]
    assert store.load()["head_session"] == "20261018-080000-112233"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_native_cli_truncation_keeps_its_real_executor_exit_status(tmp_path, exit_code):
    events = stream("git diff scripts/relay_argon.py")
    events[2]["step_update"]["tool_info"]["output"] = (
        "The tool's output was truncated due to being too long. You can find the full output in a file at "
        "file:///tmp/native-proof/output.txt.\n\n"
        "Here is a preview with the middle of the output truncated:\n"
        f"Command exited with code {exit_code}.\n\nCommand output:\ndiff omitted\n")
    report = rc.classify_turn(write_native(tmp_path, events))
    assert report.calls[0].done and not report.calls[0].pending
    assert report.calls[0].exit_status == exit_code
    assert report.outcome == "success"


def _turn_with_commands(commands: list[tuple[str, str]]) -> list[dict]:
    """A native turn shaped like 20261107-095109-fc3bba: many finished foreground commands, some exiting non-zero."""
    events = [{"event": "init", "conversation_id": "native-test", "init": {"model": ra.MODEL}}]
    for index, (command, output) in enumerate(commands, start=2):
        for state in ("ACTIVE", "DONE"):
            events.append({"event": "step_update", "step_update": {
                "step_index": index, "state": state, "conversation_id": "native-test", "step_type": "tool",
                "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": command}, "output": output}}})
    events.append({"event": "result", "result": {"conversation_id": "native-test", "status": "SUCCESS",
                                                 "response": 'RELAY_STATUS: {"status":"propose_done"}'}})
    return events


FC3BBA_COMMANDS = [
    ("python3 scripts/devctx.py recover", "Command exited with code 0.\n"),
    ("python3 scripts/devctx.py search no-such-term", "Command exited with code 1.\n"),
    ("python -m pytest -q backend/tests/test_x.py", "Command exited with code 1.\n\nCommand output:\n1 failed\n"),
    ("python -m pytest -q backend/tests/test_x.py", "Command exited with code 0.\n"),
    ("node e2e/walk.mjs /tmp/evidence", "Command exited with code 1.\n"),
    ("node e2e/walk.mjs /tmp/evidence", "Command exited with code 0.\n"),
    ("git push origin HEAD", "Command exited with code 1.\n\nCommand output:\nrejected\n"),
    ("git push origin HEAD", "Command exited with code 0.\n"),
    ("python3 scripts/devctx.py note proof done", "Command exited with code 0.\n"),
]


def test_finished_nonzero_commands_do_not_fail_a_successful_native_turn(tmp_path):
    report = rc.classify_turn(write_native(tmp_path, _turn_with_commands(FC3BBA_COMMANDS)))
    assert report.outcome == "success" and report.terminal
    assert not report.in_flight
    assert sorted(c.exit_status for c in report.calls) == [0, 0, 0, 0, 0, 1, 1, 1, 1]
    # The failed push is still a recorded external effect for the next turn's prompt.
    assert [c.label for c in report.effects if c.label == "git push"] == ["git push", "git push"]


def test_successful_turn_with_nonzero_commands_resets_consecutive_failures(tmp_path):
    env = Env(tmp_path)
    turn = write_native(env.repo / ".agent-relay", _turn_with_commands(FC3BBA_COMMANDS))
    sup = env.supervisor()
    sup.state["head_session"] = turn.parent.parent.name
    sup.state["counters"].update({"consecutive_failures": 2, "transient_streak": 1, "crash_retries": 1})
    report = rc.classify_turn(turn)
    assert report.outcome == "success"
    assert sup._decide(report, None) == "idle"
    assert sup.state["counters"]["consecutive_failures"] == 0 and sup.state["hold"] is None


def test_truly_pending_native_command_still_settles_unknown(tmp_path):
    commands = FC3BBA_COMMANDS + [("gh pr merge 60", "Command running with ID: xyz\n")]
    report = rc.classify_turn(write_native(tmp_path, _turn_with_commands(commands)))
    assert report.outcome == "unknown" and not report.terminal
    assert [c.label for c in report.in_flight] == ["gh pr merge"]


def test_failed_native_terminal_result_still_fails_with_nonzero_commands(tmp_path):
    events = _turn_with_commands(FC3BBA_COMMANDS)
    events[-1]["result"].update({"status": "ERROR", "response": "", "error": "Internal server error"})
    report = rc.classify_turn(write_native(tmp_path, events, meta={"exit_code": 1}))
    assert report.terminal and report.outcome not in ("success", "unknown")


@pytest.mark.parametrize("output", [
    "Command is still running.\nCommand output:\nCommand exited with code 0.\n",
    "untrusted prefix\nCommand exited with code 0.\n",
    "The tool's output was truncated.\nCommand exited with code 0.\n",
])
def test_stdout_or_unknown_truncation_text_cannot_forge_a_native_exit(tmp_path, output):
    events = stream()
    events[2]["step_update"]["tool_info"]["output"] = output
    report = rc.classify_turn(write_native(tmp_path, events))
    assert report.outcome != "success" and report.calls[0].pending


def test_native_read_file_is_read_only_without_reclassifying_unknown_tools(tmp_path):
    events = stream()
    for event in events[1:3]:
        step = event["step_update"]
        step["tool_name"] = "read_file"
        step["tool_info"] = {"parameters": {"Path": "/tmp/review-source.txt"}, "output": "12 lines, 200 bytes"}
    report = rc.classify_turn(write_native(tmp_path, events))
    assert report.outcome == "success" and report.calls[0].kind == "read_only"
    for event in events[1:3]:
        event["step_update"]["tool_name"] = "unknown_new_tool"
    other = tmp_path / "other"
    other.mkdir()
    assert rc.classify_turn(write_native(other, events)).calls[0].kind == "external"


# --- maintainer directive L20261109090114-5e79: harness failures are never capability verdicts -----------------

def tool_step(index: int, state: str, name: str, params: dict, **extra) -> dict:
    info = {"name": name, "parameters": params, **extra}
    return {"event": "step_update", "step_update": {"conversation_id": "native-test", "step_index": index,
            "state": state, "step_type": "tool", "tool_name": name, "tool_info": info}}


def test_recovered_local_and_read_tool_errors_do_not_fail_a_successful_turn(tmp_path):
    events = stream()
    recovered = [
        tool_step(5, "ACTIVE", "read_file", {"Path": "/repo/missing.py"}),
        tool_step(5, "ERROR", "read_file", {"Path": "/repo/missing.py"},
                  error={"type": "TOOL_ERROR", "message": "file does not exist"}),
        tool_step(6, "ACTIVE", "edit_file", {"Path": "/repo/a.py"}),
        tool_step(6, "ERROR", "edit_file", {"Path": "/repo/a.py"}, error={"type": "TOOL_ERROR", "message": "no match"}),
        tool_step(7, "ACTIVE", "write_file", {"Path": "/repo/b.py"}),
        tool_step(7, "DONE", "write_file", {"Path": "/repo/b.py"}),
        tool_step(8, "ACTIVE", "manage_task", {"Action": "kill", "TaskId": "c/task-6"}),
        tool_step(8, "ERROR", "manage_task", {"Action": "kill", "TaskId": "c/task-6"},
                  error={"type": "TOOL_ERROR", "message": "task is not running (status: DONE)"}),
    ]
    report = rc.classify_turn(write_native(tmp_path, events[:3] + recovered + events[3:]))
    assert report.outcome == "success"
    kinds = {c.tool: c.kind for c in report.calls}
    assert kinds["edit_file"] == kinds["write_file"] == kinds["manage_task"] == "local_write"
    assert kinds["read_file"] == "read_only"
    assert not report.in_flight and report.harness_cause is None


def test_failed_external_tool_call_stays_strict(tmp_path):
    events = stream("gh pr merge 1")
    events[2]["step_update"]["state"] = "ERROR"
    assert rc.classify_turn(write_native(tmp_path, events)).outcome != "success"


def agy_error(**fields) -> str:
    return "root agent idle\nerror: x\n" + ra.AGY_ERROR_PREFIX + json.dumps(fields) + "\n"


def interrupted(tmp_path: Path, stderr: str, *, command: str = "git push origin topic") -> rc.TurnReport:
    events = stream(command)[:2]  # the push started, then the runner cut the turn: no result event
    turn = write_native(tmp_path, events, meta={"exit_code": 3})
    (turn / "stderr.log").write_text(stderr)
    return rc.classify_turn(turn)


def test_print_timeout_is_a_harness_cause_and_keeps_the_effect_in_flight(tmp_path):
    report = interrupted(tmp_path, agy_error(short_error="print timeout after 30m0s with the turn still in progress",
                                             status="DEADLINE_EXCEEDED", reason="print_timeout"))
    assert report.outcome == "unknown"  # same agent resumes after reconciliation
    assert report.harness_cause == "turn_time_limit"
    assert [c.label for c in report.in_flight] == ["git push"]  # still gated and probed, never assumed


@pytest.mark.parametrize(("fields", "outcome", "cause"), [
    ({"status": "RESOURCE_EXHAUSTED", "error_code": 429, "short_error": "Individual quota reached"}, "quota",
     "provider_quota"),
    ({"status": "UNAUTHENTICATED", "error_code": 401, "short_error": "UNAUTHENTICATED (code 401)"}, "transient",
     "provider_auth_blip"),
    ({"status": "UNKNOWN", "short_error": "agent executor error: request failed: Post"}, "transient",
     "provider_network"),
])
def test_provider_errors_are_classified_from_runner_evidence(tmp_path, fields, outcome, cause):
    report = interrupted(tmp_path, agy_error(**fields), command="pwd")
    assert (report.outcome, report.harness_cause) == (outcome, cause)


def test_runner_error_never_overrides_a_genuine_success(tmp_path):
    turn = write_native(tmp_path, stream())
    (turn / "stderr.log").write_text(agy_error(status="UNAVAILABLE", short_error="blip"))
    report = rc.classify_turn(turn)
    assert report.outcome == "success" and report.harness_cause is None


def test_agy_prompt_carries_the_harness_rules_and_the_hour_budget(tmp_path):
    assert ra.TURN_TIMEOUT == 3600
    _, turn = ra.new_session(tmp_path, "Task")
    prompt = (turn / "prompt.md").read_text()
    assert "hard limit of 60 minutes" in prompt and "timeout 900" in prompt and "absolute paths" in prompt


def network_blip_turn(env: Env) -> rc.TurnReport:
    turn = write_native(env.repo / ".agent-relay", stream("pwd")[:2], meta={"exit_code": 3})
    (turn / "stderr.log").write_text(agy_error(status="UNKNOWN", short_error="request failed: Post https://x"))
    report = rc.classify_turn(turn)
    report.agent = "argon"
    return report


def test_network_blip_retries_the_same_agent_instead_of_switching(tmp_path):
    env = Env(tmp_path)
    sup = env.supervisor()
    sup.state.update(head_session="20261027-075000-112233", agent="argon")
    report = network_blip_turn(env)
    assert report.harness_cause == "provider_network"
    decided = sup._decide(report, "task")
    assert isinstance(decided, tuple) and decided[0] == "argon"
    assert "argon" not in sup.state["cooldowns"]  # no cooldown: nothing pushes the work to another provider
    assert sup.state["counters"]["transient_argon"] == 1


def test_a_run_of_network_blips_falls_back_to_the_normal_cooldown(tmp_path):
    env = Env(tmp_path)
    sup = env.supervisor()
    sup.state.update(head_session="20261027-075000-112233", agent="argon")
    sup.state["counters"]["transient_argon"] = sup.limits.same_agent_retries
    sup._decide(network_blip_turn(env), "task")
    assert sup.state["cooldowns"]["argon"]["reason"] == "transient"


def test_preflight_unreadable_quota_is_a_network_cause(tmp_path):
    turn = write_native(tmp_path, [], meta={"launched": False, "exit_code": None, "reason": "quota_unreadable"})
    report = rc.classify_turn(turn)
    assert (report.outcome, report.harness_cause) == ("transient", "provider_network")


# --- resuming the interrupted agy conversation (maintainer directive L20261109091909-a4bb) ----------------------

CID = "5b934b69-1847-40af-93ec-eefda93178a5"


def test_conversation_of_reads_the_init_event(tmp_path):
    events = stream()
    events[0]["conversation_id"] = CID
    assert ra.conversation_of(write_native(tmp_path, events)) == CID
    events[0]["conversation_id"] = "../../etc"
    other = tmp_path / "other"
    other.mkdir()
    assert ra.conversation_of(write_native(other, events)) is None


def test_new_session_records_a_valid_resume_and_tells_the_model(tmp_path):
    _, turn = ra.new_session(tmp_path, "Task", resume=CID)
    assert json.loads((turn / "resume.json").read_text()) == {"conversation": CID}
    assert "## Resumed" in (turn / "prompt.md").read_text()
    _, fresh = ra.new_session(tmp_path, "Task", resume="not-a-uuid")
    assert not (fresh / "resume.json").exists() and "## Resumed" not in (fresh / "prompt.md").read_text()


@pytest.mark.parametrize(("size", "used", "compacted", "why"), [
    (None, False, None, "missing"),
    (1024, True, None, None),
    (ra.RESUME_COMPACT_BYTES + 1, True, True, None),
    (ra.RESUME_MAX_BYTES + 1, False, None, "too_large"),
])
def test_prepare_resume_uses_compacts_or_skips_by_size(tmp_path, monkeypatch, size, used, compacted, why):
    store = tmp_path / "store"
    store.mkdir()
    if size is not None:
        with (store / f"{CID}.db").open("wb") as handle:
            handle.truncate(size)
    _, turn = ra.new_session(tmp_path, "Task", resume=CID)
    calls = []
    monkeypatch.setattr(ra.subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    meta: dict = {}
    got = ra.prepare_resume("agy", ra.MODEL, tmp_path, turn, meta, store=store)
    assert got == (CID if used else None)
    assert meta["resume"]["used"] is used and meta["resume"].get("why") == why
    assert meta["resume"].get("compacted") == compacted
    assert bool(calls) is bool(compacted) and (not calls or ["-p", "/compact"] == calls[0][1:3])


def test_retry_after_an_unsuccessful_native_turn_resumes_its_conversation(tmp_path):
    env = Env(tmp_path)
    sup = env.supervisor()
    events = stream("pwd")[:2]
    events[0]["conversation_id"] = CID
    turn = write_native(env.repo / ".agent-relay", events, meta={"exit_code": 3})
    report = rc.classify_turn(turn)
    report.agent = "argon"
    sessions = env.repo / ".agent-relay/sessions"
    session_id, _, turn_name = report.turn_id.partition("/")
    assert (sessions / session_id / "turns" / turn_name).is_dir()
    seen = {}
    store = tmp_path / "conversations"
    store.mkdir()
    (store / f"{CID}.db").write_bytes(b"x")  # resumable: the supervisor only resumes a conversation that exists

    class Relay:
        def sessions_dir(self):
            return sessions

        def conversation_store(self):
            return store

        def start(self, agent, parent, task, turns, resume=None):
            seen.update(agent=agent, resume=resume)
            raise OSError("stop here")

    sup.host.relay = Relay()
    sup.state["permissions_digest"] = sup.host.permissions_digest()
    sup._launch("argon", "[relay-continuity attempt {nonce}]\ntask", report)
    assert seen == {"agent": "argon", "resume": CID}
    report.outcome = "success"
    sup._launch("argon", "[relay-continuity attempt {nonce}]\ntask", report)
    assert seen["resume"] is None  # a new task after success starts a fresh conversation
