from datetime import UTC, datetime

from app.modules.coding import relay_monitor as monitor


def test_question_parsing_and_text_settlement():
    ro = monitor.observe()
    entries = [
        {"id": "L20261014124945-bc19", "kind": "question", "text": "Historical free-form blocker without numbered options"},
        {"id": "L20261103151936-d164", "kind": "question", "text": "Choose\n1. (consigliata) Yes — Conseguenza: Done\n2) No"},
        {"id": "L20261103152432-3610", "kind": "decision", "text": "Maintainer answer to question L20261103151936-d164"},
        {"id": "L20261103151937-abcd", "kind": "question", "text": "Proceed?\n1. (recommended) Yes - Consequence: safe"},
    ]
    questions = ro.parse_open_questions(entries)
    assert len(questions) == 1
    assert questions[0]["options"] == [{"index": 1, "label": "Yes", "recommended": True, "consequence": "safe"}]


def test_controls_and_answer(tmp_path, monkeypatch):
    root, repo = tmp_path / "continuity", tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("JARVIS_CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(monitor, "control_root", lambda: root)
    monkeypatch.setattr(monitor, "relay_repo", lambda: repo)
    assert monitor.answer_question("L20261103151937-abcd", option_index=1)["reason"] == "owner_state_missing"
    root.mkdir()
    ro = monitor.observe()
    road = {"enabled": True, "paused": None, "authority": "test"}
    ro.rc.write_json_atomic(ro.rc.roadmap_path(root), road)
    assert monitor.control_supervisor("pause")["state"] == "ok"
    assert ro.rc.load_roadmap(root)["paused"] == "maintainer_stop"
    assert monitor.control_supervisor("resume")["state"] == "ok"
    assert ro.rc.load_roadmap(root)["paused"] is None
    road.update(paused="coordinator_blocked", job_wait=["j-20261103T180000Z-abcdef"])
    ro.rc.write_json_atomic(ro.rc.roadmap_path(root), road)
    assert monitor.control_supervisor("resume")["state"] == "ok"
    assert ro.rc.load_roadmap(root)["paused"] == "coordinator_blocked"
    assert ro.rc.load_roadmap(root)["job_wait"] == ["j-20261103T180000Z-abcdef"]
    road.pop("job_wait")
    assert monitor.control_supervisor("graceful_restart")["state"] == "ok"
    assert (root / "STOP").exists()
    env = ro.devctx.default_env(repo)
    question = ro.devctx.make_entry(env, "question", "Choose\n1. Yes — Consequence: done")
    ro.devctx.append_entry(env, question)
    road.update(paused="coordinator_blocked", paused_at=ro.rc.iso(datetime.now(UTC)))
    ro.rc.write_json_atomic(ro.rc.roadmap_path(root), road)
    assert monitor.answer_question(question["id"], option_index=3)["reason"] == "option_invalid"
    assert monitor.answer_question(question["id"], option_index=1)["state"] == "ok"
    assert ro.rc.load_roadmap(root)["paused"] is None
    assert monitor.answer_question(question["id"], option_index=1)["reason"] == "question_not_open"
