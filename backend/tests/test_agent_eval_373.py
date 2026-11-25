"""Tests for Spec 373 — AGENT-EVAL-LAB-1.

Repeated, paired and resumable agent evaluation with held-out tasks and artifact checks.
Offline: a stub HTTP backend on a loopback port and a temporary SQLite fixture.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmarks.agent_eval.runner import (  # noqa: E402
    check_outcome,
    compare_report,
    compare_runs,
    format_summary,
    load_run_results,
    main,
    run_eval,
    score_task,
)

EXAMPLE_TASKS_PATH = REPO_ROOT / "scripts" / "benchmarks" / "agent_eval" / "example_tasks.json"


# --- Stub backend ------------------------------------------------------------


@contextmanager
def stub_backend(
    outcomes: dict[str, list[bool]], *, fail_after: int | None = None,
    on_interaction: Callable[[str, str], None] | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Serve threads and interactions; a prompt's n-th call answers "42" when outcomes[prompt][n] else "no".

    Yields (base_url, state); state["interactions"] counts interactions served. After `fail_after` interactions
    the backend answers HTTP 500 (an interrupted run).
    """
    state: dict[str, Any] = {"threads": 0, "interactions": 0, "calls": {}, "thread_for_prompt": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _send(self, code: int, body: dict[str, Any]) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path == "/ai/threads":
                state["threads"] += 1
                self._send(200, {"id": f"thread-stub-{state['threads']}"})
                return
            if fail_after is not None and state["interactions"] >= fail_after:
                self._send(500, {"detail": "interrupted"})
                return
            state["interactions"] += 1
            prompt = body["prompt"]
            index = state["calls"].get(prompt, 0)
            state["calls"][prompt] = index + 1
            thread_id = self.path.split("/")[3]
            if on_interaction:
                on_interaction(prompt, thread_id)
            ok = outcomes[prompt][index]
            self._send(200, {"interaction": {
                "id": "i-1", "flow_state": "completed", "assistant_text": "42" if ok else "no",
                "terminal_reason": None, "model_id": "stub", "role_id": "isaac"}})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()


def number_task(task_id: str, **extra: Any) -> dict[str, Any]:
    return {"id": task_id, "prompt": task_id, "max_points": 2.0,
            "checks": [{"kind": "number", "value": 42.0, "rel_tol": 0.01}], **extra}


def write_tasks(path: Path, tasks: list[dict[str, Any]], set_id: str = "set373") -> Path:
    path.write_text(json.dumps({"set_id": set_id, "cloud_ok": False, "tasks": tasks}), encoding="utf-8")
    return path


def run(tasks_path: Path, base_url: str, out: Path, **kwargs: Any) -> dict[str, Any]:
    return run_eval(tasks_path=tasks_path, label=kwargs.pop("label", "lab"), reps=kwargs.pop("reps", 3),
                    base_url=base_url, out_dir=out, **kwargs)


TIMING_KEYS = {"wall_time_s", "yield_wait_s", "thread_id", "mean_wall_time_s", "total_wall_time_s", "timestamp"}


def without_timings(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_timings(v) for k, v in value.items() if k not in TIMING_KEYS}
    if isinstance(value, list):
        return [without_timings(v) for v in value]
    return value


MIXED = {"A": [True, True, True], "B": [True, True, False], "C": [False, False, False]}


# --- Statistics ----------------------------------------------------------------


def test_pass_statistics_match_hand_computed_values(tmp_path: Path) -> None:
    tasks = write_tasks(tmp_path / "t.json", [number_task("A"), number_task("B"), number_task("C")])
    with stub_backend(MIXED) as (url, _):
        result = run(tasks, url, tmp_path / "out")

    per_task = result["tasks"]
    assert [(per_task[t]["passes"], per_task[t]["n"]) for t in "ABC"] == [(3, 3), (2, 3), (0, 3)]
    assert [per_task[t]["pass_at_1"] for t in "ABC"] == [1.0, round(2 / 3, 4), 0.0]
    assert [per_task[t]["pass_at_k"] for t in "ABC"] == [1.0, 0.0, 0.0]

    summary = result["summary"]
    # Hand values: pass^1 = (1, 2/3, 0): mean 5/9, sd = sqrt(((4/9)^2 + (1/9)^2 + (5/9)^2) / 2), SE = sd / sqrt(3).
    assert summary["mean_pass_at_1"] == pytest.approx(5 / 9, abs=1e-4)
    assert summary["pass_at_1_se"] == pytest.approx(0.2940, abs=1e-4)
    assert summary["pass_at_1_se"] == pytest.approx(statistics.stdev([1, 2 / 3, 0]) / math.sqrt(3), abs=1e-4)
    # pass^k = (1, 0, 0): mean 1/3, sd = sqrt(1/3), SE = 1/3.
    assert summary["mean_pass_at_k"] == pytest.approx(1 / 3, abs=1e-4)
    assert summary["pass_at_k_se"] == pytest.approx(1 / 3, abs=1e-4)
    # Points: (2, 4/3, 0): mean 10/9, existing total stays the sum of task means.
    assert summary["mean_points"] == pytest.approx(10 / 9, abs=1e-4)
    assert summary["mean_points_se"] == pytest.approx(statistics.stdev([2, 4 / 3, 0]) / math.sqrt(3), abs=1e-4)
    assert result["total_mean_score"] == pytest.approx(10 / 3, abs=1e-3)
    assert summary["k"] == 3 and summary["tasks"] == 3


def test_single_task_standard_error_is_zero_and_failed_flow_never_passes(tmp_path: Path) -> None:
    tasks = write_tasks(tmp_path / "t.json", [number_task("A")])
    with stub_backend({"A": [True, True, True]}) as (url, _):
        result = run(tasks, url, tmp_path / "out")
    assert result["summary"]["pass_at_1_se"] == 0.0 and result["summary"]["mean_points_se"] == 0.0

    points, passed, total = score_task("42", "failed_terminal", number_task("A"))
    assert (points, passed, total) == (0.0, 0, 1)


def test_run_summary_line_names_every_field(tmp_path: Path) -> None:
    tasks = write_tasks(tmp_path / "t.json", [number_task("A"), number_task("B"), number_task("C")])
    with stub_backend(MIXED) as (url, _):
        result = run(tasks, url, tmp_path / "out")
    line = format_summary(result)
    for fragment in ("set=set373", "split=dev", "tasks=3", "k=3", "points=1.111±", "pass^1=0.556±0.294",
                     "pass^k=0.333±0.333", "reps=9", "tool_calls=0", "wall="):
        assert fragment in line


# --- Held-out split ------------------------------------------------------------


def split_tasks(tmp_path: Path) -> Path:
    return write_tasks(tmp_path / "t.json", [
        number_task("D1"), number_task("D2", split="dev", tags=["x"]), number_task("H1", split="heldout"),
    ])


def test_default_split_runs_dev_only_and_records_selection(tmp_path: Path) -> None:
    tasks = split_tasks(tmp_path)
    with stub_backend({p: [True] * 3 for p in ("D1", "D2", "H1")}) as (url, state):
        result = run(tasks, url, tmp_path / "out")
    assert result["split"] == "dev" and result["task_ids"] == ["D1", "D2"]
    assert set(result["tasks"]) == {"D1", "D2"} and "H1" not in state["calls"]
    import hashlib
    assert result["tasks_sha256"] == hashlib.sha256(tasks.read_bytes()).hexdigest()


def test_heldout_requires_the_explicit_flag(tmp_path: Path) -> None:
    tasks = split_tasks(tmp_path)
    with stub_backend({p: [True] * 4 for p in ("D1", "D2", "H1")}) as (url, state):
        for split in ("heldout", "all"):
            with pytest.raises(ValueError, match="allow-heldout"):
                run(tasks, url, tmp_path / "out", split=split)
        assert state["interactions"] == 0 and not (tmp_path / "out" / "results.json").exists()
        result = run(tasks, url, tmp_path / "out", split="heldout", allow_heldout=True)
        assert list(result["tasks"]) == ["H1"] and result["split"] == "heldout"
        everything = run(tasks, url, tmp_path / "out_all", split="all", allow_heldout=True, reps=1)
        assert set(everything["tasks"]) == {"D1", "D2", "H1"}


def test_cli_split_guard(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tasks = split_tasks(tmp_path)
    base = ["run", "--tasks", str(tasks), "--label", "lab", "--out", str(tmp_path / "out"), "--reps", "1",
            "--timeout", "700"]
    with stub_backend({p: [True] * 3 for p in ("D1", "D2", "H1")}) as (url, _):
        assert main([*base, "--base-url", url, "--split", "heldout"]) == 1
        assert "--allow-heldout" in capsys.readouterr().err
        assert main([*base, "--base-url", url, "--split", "heldout", "--allow-heldout"]) == 0
        assert "split=heldout tasks=1" in capsys.readouterr().out


def test_invalid_split_or_tags_are_refused(tmp_path: Path) -> None:
    for bad in ({"split": "test"}, {"tags": "a"}):
        tasks = write_tasks(tmp_path / "t.json", [number_task("A", **bad)])
        with pytest.raises(ValueError):
            run_eval(tasks_path=tasks, label="x", out_dir=tmp_path / "o", reference="haiku")


# --- Paired comparison ---------------------------------------------------------


def paired_runs(tmp_path: Path) -> tuple[Path, Path, Path]:
    tasks = write_tasks(tmp_path / "t.json", [number_task("A"), number_task("B"), number_task("C")])
    base = {"A": [True] * 3, "B": [True, False, False], "C": [False] * 3}
    cand = {"A": [True, True, False], "B": [True, True, False], "C": [True, False, False]}
    with stub_backend(base) as (url, _):
        run(tasks, url, tmp_path / "base")
    with stub_backend(cand) as (url, _):
        run(tasks, url, tmp_path / "cand", label="cand")
    return tasks, tmp_path / "base", tmp_path / "cand"


def test_paired_comparison_matches_hand_values(tmp_path: Path) -> None:
    _, base, cand = paired_runs(tmp_path)
    code, report, numbers = compare_report(load_run_results(base), load_run_results(cand))
    # pass^1 base (1, 1/3, 0), cand (2/3, 2/3, 1/3): diffs (-1/3, 1/3, 1/3): mean 1/9, paired SE 2/9.
    p1 = numbers["paired"]["pass_at_1"]
    assert p1["mean_diff"] == pytest.approx(1 / 9, abs=1e-4)
    assert p1["se"] == pytest.approx(statistics.stdev([-1 / 3, 1 / 3, 1 / 3]) / math.sqrt(3), abs=1e-4)
    assert p1["se"] == pytest.approx(2 / 9, abs=1e-4)
    assert (p1["better"], p1["worse"], p1["equal"]) == (2, 1, 0)
    # Points are 2 * pass^1 here, so the difference and its SE double.
    pts = numbers["paired"]["points"]
    assert pts["mean_diff"] == pytest.approx(2 / 9, abs=1e-4) and pts["se"] == pytest.approx(4 / 9, abs=1e-4)
    assert "pass^1" in report and "better 2  worse 1  equal 0" in report
    assert code == numbers["exit_code"] == 0
    assert compare_runs(load_run_results(base), load_run_results(cand))[0] == code


def test_compare_cli_writes_json_and_keeps_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, base, cand = paired_runs(tmp_path)
    out_json = tmp_path / "cmp.json"
    assert main(["compare", "--baseline", str(base), "--candidate", str(cand), "--json", str(out_json)]) == 0
    assert json.loads(out_json.read_text())["paired"]["pass_at_1"]["better"] == 2
    # Swapped, the candidate's total drops by 2/3 points (> 0.5): the existing regression rule still exits 1.
    assert main(["compare", "--baseline", str(cand), "--candidate", str(base)]) == 1
    assert "Paired over 3 tasks" in capsys.readouterr().out


def test_compare_refuses_set_sha_or_task_drift_unless_allowed(tmp_path: Path) -> None:
    tasks, base, cand = paired_runs(tmp_path)
    baseline, candidate = load_run_results(base), load_run_results(cand)

    other_set = {**candidate, "set_id": "elsewhere"}
    with pytest.raises(ValueError, match="task set ids differ"):
        compare_runs(baseline, other_set)
    other_sha = {**candidate, "tasks_sha256": "0" * 64}
    with pytest.raises(ValueError, match="SHA-256 differs"):
        compare_runs(baseline, other_sha)
    fewer = {**candidate, "tasks": {k: v for k, v in candidate["tasks"].items() if k != "C"}}
    fewer["total_mean_score"] = round(sum(t["mean_score"] for t in fewer["tasks"].values()), 4)
    with pytest.raises(ValueError, match="task ids differ"):
        compare_runs(baseline, fewer)

    code, report = compare_runs(baseline, other_sha, allow_task_drift=True)
    assert "TASK DRIFT ALLOWED: task-file SHA-256 differs" in report
    code, report = compare_runs(baseline, fewer, allow_task_drift=True)
    assert "task ids differ" in report and "Paired over 2 tasks" in report and code in (0, 1)
    assert main(["compare", "--baseline", str(base), "--candidate", str(cand), "--allow-task-drift"]) == 0


def test_compare_reads_earlier_results_without_new_fields(tmp_path: Path) -> None:
    def rep(points: float) -> dict[str, Any]:
        return {"rep": 0, "answer": "42", "flow_state": "completed", "points": points, "max_points": 2.0}

    def old_run(points: list[float]) -> dict[str, Any]:
        tasks = {f"T{i}": {"mean_score": p, "min_score": p, "repetitions": [rep(p), rep(p)]}
                 for i, p in enumerate(points)}
        return {"set_id": "old", "total_mean_score": sum(points), "tasks": tasks}

    code, report, numbers = compare_report(old_run([2.0, 1.0, 0.0]), old_run([2.0, 2.0, 1.0]))
    assert numbers["paired"]["pass_at_1"] == {"mean_diff": pytest.approx(1 / 3, abs=1e-4), "se": pytest.approx(
        statistics.stdev([0, 1, 0]) / math.sqrt(3), abs=1e-4), "better": 1, "worse": 0, "equal": 2}
    assert "predates split, tasks_sha256, complete" in report
    assert code == 0


def test_compare_refuses_incomplete_run(tmp_path: Path) -> None:
    _, base, cand = paired_runs(tmp_path)
    candidate = {**load_run_results(cand), "complete": False}
    with pytest.raises(ValueError, match="incomplete"):
        compare_runs(load_run_results(base), candidate)


# --- Artifact checks -------------------------------------------------------------


def fixture_db(path: Path) -> Path:
    db = path / "jarvisos.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, workspace_id TEXT, event_type TEXT NOT NULL, "
                 "actor TEXT, target_type TEXT, target_id TEXT, payload TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE studio_drafts (model_spec_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                 "source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    return db


def add_tool_call(db: Path, thread_id: str, call_id: str, tool: str | None) -> None:
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO events (event_type, target_id, payload) VALUES ('hermes.agent_event', ?, ?)",
                 (f"ev-{call_id}", json.dumps({"kind": "tool.call", "correlation_id": call_id,
                                               "session_ref": {"jarvis_thread_id": thread_id}})))
    if tool is not None:
        conn.execute("INSERT INTO events (event_type, target_id, payload) VALUES ('hermes.tool_started', ?, ?)",
                     (call_id, json.dumps({"call_id": call_id, "tool_name": tool})))
    conn.commit()
    conn.close()


def add_draft(db: Path, workspace: str, source: str, at: str, updated: str | None = None) -> None:
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO studio_drafts VALUES (?, ?, ?, ?, ?)",
                 (f"m{time.monotonic_ns()}", workspace, source, at, updated or at))
    conn.commit()
    conn.close()


def test_tool_called_counts_the_named_tool_of_that_thread(tmp_path: Path) -> None:
    db = fixture_db(tmp_path)
    add_tool_call(db, "th1", "c1", "mcp__jarvis__jarvis_studio_act")
    add_tool_call(db, "th1", "c2", "jarvis_studio_act")
    add_tool_call(db, "th1", "c3", "bench_run")
    add_tool_call(db, "th2", "c4", "jarvis_studio_act")
    add_tool_call(db, "th1", "c5", None)
    ctx = {"data_root": tmp_path, "thread_id": "th1", "workspace_id": "w", "started_at": 0.0, "ended_at": 1.0}

    def check(**kw: Any) -> tuple[bool, str | None]:
        return check_outcome("", {"kind": "tool_called", **kw}, ctx)

    assert check(tool="jarvis_studio_act") == (True, None)
    assert check(tool="jarvis_studio_act", min=2) == (True, None)
    ok, reason = check(tool="jarvis_studio_act", min=3)
    assert not ok and "seen 2, need 3" in (reason or "")
    assert check(tool="bench_run", min=2)[0] is False
    assert check(tool="never_called")[0] is False
    assert check_outcome("", {"kind": "tool_called", "tool": "bench_run"}, {**ctx, "thread_id": "th9"})[0] is False


def test_studio_draft_needs_workspace_window_and_every_term(tmp_path: Path) -> None:
    db = fixture_db(tmp_path)
    inside = "2026-11-25T10:00:30+00:00"
    start = time.mktime(time.strptime("2026-11-25T10:00:00", "%Y-%m-%dT%H:%M:%S")) - time.timezone
    ctx = {"data_root": tmp_path, "thread_id": "th", "workspace_id": "w", "started_at": start, "ended_at": start + 60}
    add_draft(db, "w", "def Heat_Exchanger(): pass", inside)
    add_draft(db, "other", "weir flume", inside)
    add_draft(db, "w", "weir flume old", "2026-11-25T09:00:00+00:00")
    add_draft(db, "w", "updated later", "2026-11-25T09:00:00+00:00", updated="2026-11-25T10:00:10Z")

    def check(*terms: str, c: dict[str, Any] = ctx) -> bool:
        return check_outcome("", {"kind": "studio_draft", "terms": list(terms)}, c)[0]

    assert check()  # any draft of the workspace in the window
    assert check("heat_exchanger", "PASS")  # case-insensitive, all terms
    assert check("updated")  # updated_at inside the window counts
    assert not check("heat_exchanger", "missing")
    assert not check("weir")  # the other workspace's draft and the stale one do not count
    assert not check("heat", c={**ctx, "started_at": start + 120, "ended_at": start + 180})


def test_artifact_checks_fail_without_data_root_and_record_the_reason(tmp_path: Path) -> None:
    task = {"id": "T", "prompt": "T", "max_points": 2.0, "checks": [
        {"kind": "number", "value": 42.0, "rel_tol": 0.01}, {"kind": "tool_called", "tool": "bench_run"}]}
    for kind_check in ({"kind": "tool_called", "tool": "bench_run"}, {"kind": "studio_draft"}):
        ok, reason = check_outcome("42", kind_check, None)
        assert not ok and "requires --data-root" in (reason or "")
    tasks = write_tasks(tmp_path / "t.json", [task])
    with stub_backend({"T": [True] * 3}) as (url, _):
        result = run(tasks, url, tmp_path / "out")
    rep = result["tasks"]["T"]["repetitions"][0]
    assert rep["passed"] is False and rep["points"] == 1.0
    assert "tool_called: requires --data-root" in rep["check_notes"][0]
    assert result["tasks"]["T"]["pass_at_1"] == 0.0


def test_run_evaluates_artifact_checks_on_the_proof_database(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    db = fixture_db(root)
    task_a = {"id": "A", "prompt": "A", "max_points": 2.0, "checks": [
        {"kind": "tool_called", "tool": "jarvis_studio_act"}, {"kind": "studio_draft", "terms": ["pump"]}]}
    task_b = {"id": "B", "prompt": "B", "max_points": 2.0, "checks": [
        {"kind": "tool_called", "tool": "jarvis_studio_act"}]}
    stamp = lambda: datetime.now(UTC).isoformat()  # noqa: E731

    def act(prompt: str, thread_id: str) -> None:
        # The agent of task A calls the tool and saves a draft; task B's agent answers without the tool.
        if prompt == "A":
            add_tool_call(db, thread_id, f"call-{thread_id}", "mcp__jarvis__jarvis_studio_act")
            add_draft(db, "default", "pump sizing model", stamp())

    tasks = write_tasks(tmp_path / "t.json", [task_a, task_b])
    with stub_backend({"A": [True] * 2, "B": [True] * 2}, on_interaction=act) as (url, _):
        result = run(tasks, url, tmp_path / "out", reps=2, data_root=root)
    assert result["tasks"]["A"]["passes"] == 2 and result["tasks"]["B"]["passes"] == 0
    assert result["tasks"]["A"]["repetitions"][0]["tool_calls"] == 1
    assert result["summary"]["mean_pass_at_1"] == 0.5


# --- Atomic partial writes and resume ---------------------------------------------


def resume_tasks(tmp_path: Path) -> tuple[Path, dict[str, list[bool]]]:
    tasks = write_tasks(tmp_path / "t.json", [number_task("A"), number_task("B"), number_task("C")])
    return tasks, {"A": [True, True, False, True, True], "B": [False] * 5, "C": [True] * 5}


def test_interrupted_run_leaves_a_partial_results_file_and_resume_matches_uninterrupted(tmp_path: Path) -> None:
    tasks, outcomes = resume_tasks(tmp_path)
    out = tmp_path / "out"
    with stub_backend(outcomes, fail_after=4) as (url, state):
        with pytest.raises(urllib.error.URLError):
            run(tasks, url, out)
    assert state["interactions"] == 4
    partial = json.loads((out / "results.json").read_text())
    assert partial["complete"] is False and partial["completed_repetitions"] == 4
    assert len(partial["tasks"]["A"]["repetitions"]) == 3 and len(partial["tasks"]["B"]["repetitions"]) == 1
    assert not list(out.glob(".*.tmp"))
    assert json.loads((out / "lab.json").read_text()) == partial

    with stub_backend({k: v[3:] if k == "A" else v[1:] if k == "B" else v for k, v in outcomes.items()}) as (url, state):
        with pytest.raises(ValueError, match="unfinished run"):
            run(tasks, url, out)
        resumed = run(tasks, url, out, resume=True)
    # 9 repetitions in total, 4 finished: the stub served only the remaining 5 and C ran from the start.
    assert state["interactions"] == 5 and state["calls"] == {"B": 2, "C": 3}

    with stub_backend({"A": [True, True, False], "B": [False] * 3, "C": [True] * 3}) as (url, _):
        straight = run(tasks, url, tmp_path / "straight")
    assert resumed["complete"] is True
    assert without_timings(resumed) == without_timings(straight)
    assert without_timings(json.loads((out / "results.json").read_text())) == without_timings(straight)


def test_resume_of_a_complete_run_repeats_nothing(tmp_path: Path) -> None:
    tasks, outcomes = resume_tasks(tmp_path)
    with stub_backend(outcomes) as (url, _):
        first = run(tasks, url, tmp_path / "out", reps=2)
    with stub_backend(outcomes) as (url, state):
        again = run(tasks, url, tmp_path / "out", reps=2, resume=True)
    assert state["interactions"] == 0 and without_timings(again) == without_timings(first)


def test_mismatched_resume_refuses(tmp_path: Path) -> None:
    tasks, outcomes = resume_tasks(tmp_path)
    out = tmp_path / "out"
    with stub_backend(outcomes, fail_after=2) as (url, _):
        with pytest.raises(urllib.error.URLError):
            run(tasks, url, out)
    with stub_backend(outcomes) as (url, state):
        with pytest.raises(ValueError, match="label differs"):
            run(tasks, url, out, label="other", resume=True)
        with pytest.raises(ValueError, match="reps differs"):
            run(tasks, url, out, reps=4, resume=True)
        with pytest.raises(ValueError, match="split differs"):
            run(tasks, url, out, split="all", allow_heldout=True, resume=True)
        write_tasks(tasks, [number_task("A"), number_task("B"), number_task("C"), number_task("D")])
        outcomes["D"] = [True] * 5
        with pytest.raises(ValueError, match="tasks_sha256 differs"):
            run(tasks, url, out, resume=True)
        with pytest.raises(ValueError, match="no .*results.json"):
            run(tasks, url, tmp_path / "empty", resume=True)
    assert state["interactions"] == 0


# --- Example set -------------------------------------------------------------------


def test_example_set_has_a_heldout_task_with_a_tool_called_check() -> None:
    data = json.loads(EXAMPLE_TASKS_PATH.read_text(encoding="utf-8"))
    heldout = [t for t in data["tasks"] if t.get("split") == "heldout"]
    assert len(heldout) == 1 and any(c["kind"] == "tool_called" for c in heldout[0]["checks"])
    assert len([t for t in data["tasks"] if t.get("split", "dev") == "dev"]) >= 6
