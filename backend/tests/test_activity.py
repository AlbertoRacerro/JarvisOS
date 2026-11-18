"""Spec 298: the read-only Activity read model, one fixture per source."""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.core.paths import build_paths
from app.modules.coding import activity, relay_monitor, runtime_routes

rj = relay_monitor.observe().rc.relay_jobs
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
GHP = "ghp_" + "d" * 36


def stamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_id(minutes_ago: int, tail: str = "abcdef") -> str:
    return f"j-{(NOW - timedelta(minutes=minutes_ago)).strftime('%Y%m%dT%H%M%SZ')}-{tail}"


def write_job(root: Path, minutes_ago: int, *, status: str, tail: str = "abcdef", log: str | None = None,
              spec: str | None = None, ended: bool = True, argv: list[str] | None = None) -> str:
    jid = job_id(minutes_ago, tail)
    path = rj.jobs_dir(root) / f"{jid}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if log is not None:
        path.write_text(log)
    rj.write_json_atomic(rj.job_path(jid, root), {
        "schema": rj.SCHEMA, "id": jid, "argv": argv or ["python3", "solve.py"], "label": "overnight sweep 298 cycle 7", "spec": spec, "status": status,
        "created_at": stamp(minutes_ago), "started_at": stamp(minutes_ago), "log": str(path), "exit_code": 0,
        "ended_at": stamp(minutes_ago - 1) if ended else None, "pid_namespace": "pid:[999999]"})
    return jid


@pytest.fixture
def control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    monkeypatch.setattr(relay_monitor, "control_root", lambda: root)
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: tmp_path / "repo")
    monkeypatch.setattr(activity.provider_quota, "quotas", lambda **_: {"providers": [
        {"label": "Claude", "provider": "claude", "windows": [{"remaining_percent": 60}, {"remaining_percent": 88}]},
        {"label": "Codex", "provider": "codex", "windows": []}]})
    initialize_database()
    return root


def seed_sql() -> None:
    with open_sqlite_connection() as db:
        db.execute("INSERT INTO ai_jobs (id, created_at, status, task_kind, route_reason_json) VALUES (?,?,?,?,?)",
                   ("a1", stamp(30), "provider_error", "chat", "{}"))
        db.execute("INSERT INTO ai_jobs (id, created_at, status, task_kind, route_reason_json) VALUES (?,?,?,?,?)",
                   ("a2", stamp(30), "success", "chat", "{}"))
        db.execute("INSERT INTO ai_jobs (id, created_at, status, task_kind, route_reason_json) VALUES (?,?,?,?,?)",
                   ("a3", stamp(60 * 30), "provider_error", "chat", "{}"))
        db.execute("INSERT INTO ai_flows (id, task_kind, state, created_at, updated_at) VALUES (?,?,?,?,?)",
                   ("f1", "chat", "running", stamp(5), stamp(5)))
        db.commit()


def seed_dynamic() -> None:
    directory = build_paths().data_root / "process_dynamic" / "ws1" / "d1"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({
        "job_id": "d1", "workspace_id": "ws1", "draft_id": "x", "status": "running", "progress": 0.4,
        "created_at": stamp(10), "started_at": stamp(9)}))


def test_rows_from_every_source_newest_first(control: Path) -> None:
    write_job(control, 20, status="succeeded")
    write_job(control, 3, status="running", tail="bbbbbb", ended=False)
    seed_sql()
    seed_dynamic()
    result = activity.read(NOW)
    kinds = [r.kind for r in result.rows]
    assert {"job", "dynamic", "generation", "ai_error"} <= set(kinds)
    started = [r.started_at for r in result.rows]
    assert started == sorted(started, reverse=True)
    assert sum(1 for r in result.rows if r.kind == "ai_error") == 1  # success and out-of-window rows are excluded
    dynamic = next(r for r in result.rows if r.kind == "dynamic")
    assert dynamic.progress and (dynamic.progress.done, dynamic.progress.total) == (40, 100)
    assert next(r for r in result.rows if r.kind == "generation").progress is None
    assert result.running == sum(1 for r in result.rows if r.state == "running")
    assert result.quota.pools[0].percent_free == 60 and result.quota.pools[1].percent_free is None
    assert result.downloads == [] and result.downloads_empty == "nessun download registrato"


def test_unverifiable_job_is_never_running_or_failed(control: Path) -> None:
    write_job(control, 3, status="running", ended=False)
    row = next(r for r in activity.read(NOW).rows if r.kind == "job")
    assert row.state == "unknown" and row.state_word == "stato non verificabile da qui"


def test_supervisor_mission_and_worker_rows(control: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay_monitor, "snapshot", lambda now=None: {
        "state": "running", "at": stamp(1), "mission": {"id": "m-x", "task": "1/1", "agent": "claude"}, "turn": {}})
    rows = [r for r in activity.read(NOW).rows if r.kind in ("supervisor", "worker")]
    assert {r.runner for r in rows} == {"supervisione", "modello di lavoro"}


def test_job_runner_follows_its_command(control: Path) -> None:
    write_job(control, 20, status="succeeded", argv=["bash", "-c", "cd w && claude -p < task"])
    write_job(control, 10, status="succeeded", tail="cccccc")
    runners = {r.started_at: r.runner for r in activity.read(NOW).rows if r.kind == "job"}
    assert runners == {stamp(20): "modello di lavoro", stamp(10): "motore locale"}


@pytest.mark.parametrize("state", ["paused", "held", "stopped"])
def test_inactive_mission_is_not_counted_running(control: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    monkeypatch.setattr(relay_monitor, "snapshot", lambda now=None: {
        "state": state, "at": stamp(1), "mission": {"id": "m-x", "task": "1/1", "agent": "claude"}, "turn": {}})
    rows = [r for r in activity.read(NOW).rows if r.kind in ("supervisor", "worker")]
    assert [r.state for r in rows] == ["unknown"]


def test_bound_and_window(control: Path) -> None:
    for i in range(60):
        write_job(control, 2 + i, status="succeeded", tail=f"{i:06x}")
    write_job(control, 60 * 30, status="succeeded", tail="ffffff")
    rows = [r for r in activity.read(NOW).rows if r.kind == "job"]
    assert len(rows) == activity.MAX_ROWS
    assert all(_parse_ok(r.started_at) for r in rows)


def _parse_ok(value: str | None) -> bool:
    return value is not None and NOW - activity._parse(value) < timedelta(hours=24)  # type: ignore[operator]


def test_failing_source_is_a_group_not_an_error(control: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_dynamic()
    seed_sql()

    def boom(limit: int) -> list[dict[str, object]]:
        raise OSError("disk")

    monkeypatch.setattr(activity.dynamic_jobs, "list_recent", boom)
    result = activity.read(NOW)
    groups = {g.id: g for g in result.groups}
    assert groups["dynamic"].state == "non disponibile" and groups["dynamic"].reason == "lettura non riuscita"
    assert groups["ai_errors"].state == "ok" and any(r.kind == "ai_error" for r in result.rows)


def test_missing_control_root_marks_supervisor_unavailable(control: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay_monitor, "control_root", lambda: tmp_path / "absent")
    groups = {g.id: g for g in activity.read(NOW).groups}
    assert groups["supervisor"].state == "non disponibile"


def test_detail_refuses_secret_and_scrubs_paths(control: Path) -> None:
    clean = write_job(control, 5, status="succeeded", tail="111111", log="start\nwrote /home/someone/private/out.csv\nend\n")
    secret = write_job(control, 6, status="succeeded", tail="222222", log=f"token {GHP}\n")
    ok = activity.detail(f"job:{clean}")
    assert ok.state == "ok" and "/home/someone" not in "\n".join(ok.lines) and ok.lines[0] == "start"
    refused = activity.detail(f"job:{secret}")
    assert refused.state == "refused" and refused.lines == []
    assert activity.detail("job:not-an-id").state == "not_found"


def test_detail_keeps_last_forty_lines(control: Path) -> None:
    jid = write_job(control, 5, status="succeeded", log="\n".join(f"line {i}" for i in range(100)))
    lines = activity.detail(f"job:{jid}").lines
    assert len(lines) == 40 and lines[-1] == "line 99"


def test_detail_refuses_log_outside_jobs_dir(control: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other.log"
    outside.write_text("outside\n")
    jid = write_job(control, 5, status="succeeded")
    path = rj.job_path(jid, control)
    record = json.loads(path.read_text())
    record["log"] = str(outside)
    path.write_text(json.dumps(record))
    assert activity.detail(f"job:{jid}").lines == []


def test_visible_text_has_no_ids_shas_or_commands(control: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_job(control, 20, status="succeeded", spec="298")
    write_job(control, 3, status="running", tail="bbbbbb", ended=False)
    seed_sql()
    seed_dynamic()
    monkeypatch.setattr(relay_monitor, "snapshot", lambda now=None: {
        "state": "waiting", "at": stamp(1), "mission": {"id": "m-x", "task": "1/1", "agent": "claude"}, "turn": {}})
    result = activity.read(NOW)
    pattern = re.compile(r"\b[0-9a-f]{7,40}\b|\bj-\d{8}T|\bm-\d{8}T|\bL\d{14}|python3?\b|git |scripts/|--\w|cycle|ciclo")
    for row in result.rows:
        for text in (row.title, row.state_word, row.step or "", row.outcome or ""):
            assert not pattern.search(text), text


def test_titles_never_show_the_label(control: Path) -> None:
    write_job(control, 3, status="succeeded")
    row = next(r for r in activity.read(NOW).rows if r.kind == "job")
    assert "overnight" not in row.title and "298" not in row.title


def test_routes_are_read_only_gets(control: Path) -> None:
    app = FastAPI()
    app.include_router(runtime_routes.router)
    client = TestClient(app)
    jid = write_job(control, 5, status="succeeded", log="hello\n")
    body = client.get("/api/coding/activity").json()
    assert {"rows", "groups", "quota", "downloads_empty"} <= set(body)
    assert client.get("/api/coding/activity/detail", params={"key": f"job:{jid}"}).json()["lines"] == ["hello"]
    assert client.post("/api/coding/activity").status_code == 405


def write_download(
    root: Path,
    minutes_ago: int,
    *,
    did: str = "d1",
    label: str = "weights.gguf",
    state: str = "running",
    bytes_done: int = 524_288_000,
    bytes_total: int | None = 1_048_576_000,
    reason: str | None = None,
    pid: int = 12345,
    mtime_minutes_ago: int | None = None,
) -> Path:
    d_dir = root / "downloads"
    d_dir.mkdir(parents=True, exist_ok=True)
    path = d_dir / f"{did}.json"
    record = {
        "id": did,
        "label": label,
        "host": "huggingface.co",
        "file_name": label,
        "bytes_done": bytes_done,
        "bytes_total": bytes_total,
        "state": state,
        "reason": reason,
        "started_at": stamp(minutes_ago),
        "ended_at": stamp(minutes_ago - 1) if state in ("succeeded", "failed") else None,
        "pid": pid,
        "sha256": "abc123" if state == "succeeded" else None,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    mtime = (NOW - timedelta(minutes=mtime_minutes_ago if mtime_minutes_ago is not None else minutes_ago)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def test_download_rows_running_succeeded_failed(control: Path) -> None:
    write_download(
        control,
        minutes_ago=10,
        did="d-run",
        label="weights.gguf",
        state="running",
        bytes_done=524_288_000,
        bytes_total=1_048_576_000,
        pid=os.getpid(),
        mtime_minutes_ago=0,
    )
    write_download(
        control,
        minutes_ago=20,
        did="d-succ",
        label="dataset.parquet",
        state="succeeded",
        bytes_done=209_715_200,
        bytes_total=209_715_200,
    )
    write_download(
        control,
        minutes_ago=30,
        did="d-fail",
        label="installer.sh",
        state="failed",
        bytes_done=10_485_760,
        bytes_total=52_428_800,
        reason="checksum mismatch",
    )

    result = activity.read(NOW)
    assert len(result.downloads) == 3
    assert all(r.kind == "download" for r in result.downloads)
    assert all(r.runner == "motore locale" for r in result.downloads)

    run_row = next(r for r in result.downloads if r.key == "download:d-run")
    assert run_row.title == "Download di weights.gguf"
    assert run_row.state == "running"
    assert run_row.state_word == "in corso"
    assert run_row.step == "sto scaricando"
    assert run_row.outcome is None
    assert run_row.progress is not None
    assert (run_row.progress.done, run_row.progress.total, run_row.progress.unit) == (500.0, 1000.0, "MB")

    succ_row = next(r for r in result.downloads if r.key == "download:d-succ")
    assert succ_row.title == "Download di dataset.parquet"
    assert succ_row.state == "done"
    assert succ_row.state_word == "completato"
    assert succ_row.step is None
    assert succ_row.outcome == "completato"
    assert succ_row.progress is not None
    assert (succ_row.progress.done, succ_row.progress.total, succ_row.progress.unit) == (200.0, 200.0, "MB")

    fail_row = next(r for r in result.downloads if r.key == "download:d-fail")
    assert fail_row.title == "Download di installer.sh"
    assert fail_row.state == "failed"
    assert fail_row.state_word == "non riuscito"
    assert fail_row.step is None
    assert fail_row.outcome == "non riuscito: checksum mismatch"
    assert fail_row.progress is not None
    assert (fail_row.progress.done, fail_row.progress.total, fail_row.progress.unit) == (10.0, 50.0, "MB")


def test_stale_running_download_shows_da_verificare(control: Path) -> None:
    # Last write older than 2 minutes (5 minutes ago): the pid is never evidence
    write_download(
        control,
        minutes_ago=10,
        did="d-stale",
        label="stale.gguf",
        state="running",
        pid=999999,
        mtime_minutes_ago=5,
    )

    result = activity.read(NOW)
    row = next(r for r in result.downloads if r.key == "download:d-stale")
    assert row.state == "unknown"
    assert row.state_word == "da verificare"
    assert row.step is None
    assert row.outcome is None


def test_malformed_download_record_is_skipped(control: Path) -> None:
    d_dir = control / "downloads"
    d_dir.mkdir(parents=True, exist_ok=True)
    (d_dir / "bad1.json").write_text("not json", encoding="utf-8")
    (d_dir / "bad2.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    (d_dir / "bad3.json").write_text(json.dumps({"state": "invalid_state"}), encoding="utf-8")
    write_download(control, minutes_ago=5, did="d-good", label="good.bin", state="succeeded")

    result = activity.read(NOW)
    assert len(result.downloads) == 1
    assert result.downloads[0].key == "download:d-good"

