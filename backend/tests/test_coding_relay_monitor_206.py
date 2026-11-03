"""Spec 206: the read-only Relay owner monitor served to Coding → Runtime (offline)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.coding import relay_monitor, runtime_routes
from app.modules.coding.runtime_truth import CANONICAL_RUNTIME_REPOSITORY

ro = relay_monitor.observe()
rc = ro.rc

T0 = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)
M1 = "m-20261022T080000Z-aaaaaa"
M2 = "m-20261022T090000Z-bbbbbb"
S1 = "20261022-080000-abcdef"
S2 = "20261022-090000-fedcba"
S3 = "20261022-100000-123456"
GHP = "ghp_" + "d" * 36


def mission(root: Path, mid: str, *, at: datetime = T0) -> None:
    (root / "missions").mkdir(parents=True, exist_ok=True)
    (root / "missions" / f"{mid}.json").write_text(json.dumps({
        "schema": rc.MISSION_SCHEMA, "id": mid, "authority": "L20261022122708-233d", "submitted_at": rc.iso(at),
        "session": None, "repo": "/repo", "tasks": [{"text": "Roadmap continuation cycle 7."}]}))


def store_state(root: Path, mid: str, **changes: Any) -> None:
    state = rc.default_state()
    state["mission"] = {"id": mid, "next": 0, "total": 1, "done": []}
    for key, value in changes.items():
        if key == "mission":
            state["mission"].update(value)
        else:
            state[key] = value
    rc.Store(root / "missions" / mid).save(state, T0)


def say(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def output(repo: Path, session: str, number: int, text: str = "") -> Path:
    path = repo / ".agent-relay" / "sessions" / session / "turns" / f"turn-{number:03d}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "output.jsonl").write_text(text)
    return path / "output.jsonl"


def files(root: Path) -> list[tuple[Path, int]]:
    return sorted((p, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


def texts(page: dict[str, Any]) -> list[str]:
    return [i["text"] for i in page["items"]]


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root, repo = tmp_path / "control", tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(relay_monitor, "control_root", lambda: root)
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)
    return root, repo


def client() -> TestClient:
    app = FastAPI()
    app.include_router(runtime_routes.router)
    return TestClient(app)


def get_page(cursor: str | None = None, **params: Any) -> dict[str, Any]:
    query = {"repository": CANONICAL_RUNTIME_REPOSITORY, **params}
    if cursor:
        query["cursor"] = cursor
    response = client().get("/api/coding/relay-monitor/timeline", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_cursor_paging_resumes_without_loss_or_repeat(owner: tuple[Path, Path]) -> None:
    root, repo = owner
    mission(root, M1)
    store_state(root, M1, phase="running", chain=[S1], head_session=S1, agent="claude")
    output(repo, S1, 1, "".join(say(f"line {n}") for n in range(7)))
    first = get_page(limit=4)
    assert first["state"] == "ok" and first["more"] is True
    assert texts(first)[0].startswith(M1) and texts(first)[1] == f"{S1}/turn-001"
    assert texts(first)[2:] == ["line 0", "line 1"]
    seen = texts(first)[2:]
    cursor = first["cursor"]
    while True:
        page = get_page(cursor, limit=4)
        seen += texts(page)
        cursor = page["cursor"]
        if not page["more"]:
            break
    assert seen == [f"line {n}" for n in range(7)]
    assert all(i["session"] == S1 and i["turn"] == "turn-001" for i in page["items"])
    assert get_page(cursor)["items"] == []  # caught up: reconnecting at the cursor repeats nothing
    with open(repo / ".agent-relay" / "sessions" / S1 / "turns" / "turn-001" / "output.jsonl", "a") as handle:
        handle.write(say("line 7"))
    assert texts(get_page(cursor)) == ["line 7"]


def test_torn_final_line_is_returned_once_complete(owner: tuple[Path, Path]) -> None:
    root, repo = owner
    mission(root, M1)
    store_state(root, M1, phase="running", chain=[S1], head_session=S1, agent="claude")
    full = say("complete") + say("torn")
    path = output(repo, S1, 1, full[:-12])
    page = get_page()
    assert texts(page)[-1] == "complete"
    again = get_page(page["cursor"])
    assert again["items"] == [] and again["cursor"] == page["cursor"]
    with open(path, "a") as handle:
        handle.write(full[-12:] + "not json\n" + say("after"))
    assert texts(get_page(page["cursor"])) == ["torn", "after"]


def test_one_event_with_many_items_pages_without_loss(owner: tuple[Path, Path]) -> None:
    root, repo = owner
    mission(root, M1)
    store_state(root, M1, phase="running", chain=[S1], head_session=S1, agent="claude")
    blocks = [{"type": "text", "text": f"part {n}"} for n in range(9)]
    output(repo, S1, 1, json.dumps({"type": "assistant", "message": {"content": blocks}}) + "\n")
    page = get_page(limit=3)
    assert len(page["items"]) == 3
    seen = texts(page)[2:]
    while page["more"]:
        page = get_page(page["cursor"], limit=3)
        assert len(page["items"]) <= 3
        seen.extend(texts(page))
    assert seen == [f"part {n}" for n in range(9)]


def test_session_handoff_mid_timeline_and_next_mission_are_ordered_markers(owner: tuple[Path, Path]) -> None:
    root, repo = owner
    mission(root, M1)
    store_state(root, M1, phase="running", chain=[S1], head_session=S1, agent="claude")
    output(repo, S1, 1, say("uno"))
    page = get_page()
    assert texts(page)[-1] == "uno"
    # provider switch: a second turn, then a new session continues the task
    output(repo, S1, 2, say("due"))
    output(repo, S2, 1, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "tre"}}) + "\n")
    store_state(root, M1, phase="running", chain=[S1, S2], head_session=S2, agent="codex")
    nxt = get_page(page["cursor"])
    assert texts(nxt) == [f"{S1}/turn-002", "due", f"{S2}/turn-001", "tre"]
    assert [(i["kind"], i["session"]) for i in nxt["items"]] == [
        ("turn", S1), ("message", S1), ("turn", S2), ("message", S2)]
    # the mission completes and the owner starts the next one
    store_state(root, M1, phase="idle", chain=[S1, S2], head_session=S2, mission={"next": 1, "completed_at": rc.iso(T0)})
    mission(root, M2, at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC))
    store_state(root, M2, phase="running", chain=[S3], head_session=S3, agent="claude")
    output(repo, S3, 1, say("quattro"))
    last = get_page(nxt["cursor"])
    assert [i["kind"] for i in last["items"]] == ["mission", "turn", "message"]
    assert texts(last)[0].startswith(M2) and texts(last)[-1] == "quattro" and last["mission"] == M2
    # the whole mission history from its first turn
    history = get_page(start="mission")
    assert texts(history)[-1] == "quattro"


def test_items_are_bounded_scrubbed_and_never_carry_reasoning(owner: tuple[Path, Path]) -> None:
    root, repo = owner
    mission(root, M1)
    store_state(root, M1, phase="running", chain=[S1], head_session=S1, agent="claude")
    long = "x" * 5000
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hidden reasoning"},
            {"type": "text", "text": f"token {GHP} at /home/user/jarvis-control/work/continuity/serve.lock {long}"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/home/user/src/JarvisOS/AGENTS.md"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True,
                                                  "content": "line one\nline two"}]}},
        {"type": "result", "subtype": "success"},
    ]
    output(repo, S1, 1, "".join(json.dumps(e) + "\n" for e in events))
    page = get_page()
    body = json.dumps(page)
    assert "ghp_" not in body and "/home/" not in body and "hidden reasoning" not in body
    message, tool, result, done = page["items"][2:]
    assert len(message["text"]) <= 600 and len(message["detail"]) <= ro.DETAIL_LIMIT
    assert "…/serve.lock" in message["text"] and "[redacted]" in message["text"]
    assert tool["text"] == "Read(…/AGENTS.md)" and "detail" not in tool
    assert result == {**result, "kind": "output", "failed": True, "text": "line one line two",
                      "detail": "line one\nline two"}
    assert done["kind"] == "done" and done["text"] == "success"
    # the terminal monitor renders the same events from the same items
    assert ro.render_event(events[0])[0].startswith("● token [redacted]")
    assert ro.render_event(events[1]) == ["  ✗ line one line two"]


def test_secret_check_refuses_the_payload(owner: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, repo = owner
    mission(root, M1)
    store_state(root, M1, phase="running", chain=[S1], head_session=S1, agent="claude")
    output(repo, S1, 1, say("ok"))
    monkeypatch.setattr(ro, "event_items", lambda *_a, **_k: [{"kind": "message", "mark": "●", "text": GHP}])
    assert get_page() == {"state": "refused", "reason": "secret_detected"}


def test_browser_items_scrub_windows_and_other_absolute_paths() -> None:
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text":
             r"C:\Users\operator\private\report.txt /opt/internal/data.csv \\server\share\private\plan.pdf https://example.test/docs"}]}}
    item = ro.event_items(event, paths=True)[0]
    assert item["text"] == "…/report.txt …/data.csv …/plan.pdf https://example.test/docs"
    assert "Users" not in json.dumps(item) and "internal" not in json.dumps(item)


def test_snapshot_scrubs_agent_written_paths_before_delivery(owner: tuple[Path, Path],
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ro, "snapshot", lambda *_a: {
        "state": "checkpointed", "turn": {"next_step": r"Inspect C:\Users\operator\private\report.txt"},
        "mission": {"label": "Check /opt/internal/data.csv"},
    })
    monkeypatch.setattr(ro, "incidents", lambda *_a: {})
    response = client().get("/api/coding/relay-monitor", params={"repository": CANONICAL_RUNTIME_REPOSITORY})
    assert response.status_code == 200
    body = response.json()
    assert body["turn"]["next_step"] == "Inspect …/report.txt"
    assert body["mission"]["label"] == "Check …/data.csv"
    assert "Users" not in response.text and "internal" not in response.text


def test_invalid_cursor_and_wrong_repository_are_refused(owner: tuple[Path, Path]) -> None:
    for cursor in ("not-a-cursor", "***", ro.encode_cursor("../../etc", None, None, 0),
                   ro.encode_cursor(M1, "../x", "turn-001", 0), ro.encode_cursor(M1, S1, "turn-001", -1)):
        response = client().get("/api/coding/relay-monitor/timeline",
                                params={"repository": CANONICAL_RUNTIME_REPOSITORY, "cursor": cursor})
        assert response.status_code == 400 and response.json()["detail"]["code"] == "cursor_invalid"
    for path in ("/api/coding/relay-monitor", "/api/coding/relay-monitor/timeline"):
        response = client().get(path, params={"repository": "o/other"})
        assert response.status_code == 400 and response.json()["detail"]["code"] == "repository_mismatch"


def test_missing_or_unreadable_owner_state_is_unknown_not_an_error(
        tmp_path: Path, owner: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, _repo = owner
    params = {"repository": CANONICAL_RUNTIME_REPOSITORY}
    empty = get_page()
    assert empty["state"] == "ok" and empty["items"] == [] and empty["mission"] is None
    (root / "missions").mkdir()
    (root / "missions" / f"{M1}.json").write_text("{torn")
    assert get_page()["items"] == []
    monkeypatch.setattr(relay_monitor, "control_root", lambda: tmp_path / "absent")
    snap = client().get("/api/coding/relay-monitor", params=params)
    assert snap.status_code == 200 and snap.json() == {"state": "unknown", "reason": "owner_state_missing"}
    assert get_page()["state"] == "unknown"

    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("unreadable")

    monkeypatch.setattr(relay_monitor, "control_root", lambda: root)
    monkeypatch.setattr(ro, "snapshot", broken)
    body = client().get("/api/coding/relay-monitor", params=params).json()
    assert body == {"state": "unknown", "reason": "owner_state_unreadable:OSError"}


def test_snapshot_serves_the_adapter_state_bounded(owner: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    root, repo = owner
    mission(root, M1)
    pending = {"nonce": "rc-x", "agent": "claude", "parent": None, "started_at": rc.iso(T0), "pid": 1,
               "pid_start": 1, "fingerprint": "f", "attempt": 1, "task": 0}
    store_state(root, M1, phase="running", pending=pending, agent="claude")
    monkeypatch.setattr(ro, "owner_alive", lambda _p: True)
    monkeypatch.setattr(ro, "_ledger", lambda _r: [])
    original = ro.snapshot
    monkeypatch.setattr(ro, "snapshot", lambda r, p, n: original(r, p, n, alive=lambda _x: True,
                                                                 ledger=lambda _y: []))
    body = client().get("/api/coding/relay-monitor", params={"repository": CANONICAL_RUNTIME_REPOSITORY}).json()
    assert body["state"] == "running" and body["mission"]["id"] == M1 and body["mission"]["task"] == "1/1"
    assert body["incidents"] == [] and body["delivered"] == []


def test_monitor_takes_no_lock_and_writes_nothing(owner: tuple[Path, Path]) -> None:
    root, repo = owner
    mission(root, M1)
    store_state(root, M1, phase="running", chain=[S1], head_session=S1, agent="claude")
    output(repo, S1, 1, say("uno") + say("due"))
    before_root, before_repo = files(root), files(repo)
    page = get_page(limit=1)
    while page["more"]:
        page = get_page(page["cursor"], limit=1)
    client().get("/api/coding/relay-monitor", params={"repository": CANONICAL_RUNTIME_REPOSITORY})
    assert files(root) == before_root and files(repo) == before_repo
    assert not list(root.rglob("*.lock")) and not list(root.rglob("*.tmp"))
