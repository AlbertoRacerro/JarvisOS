"""Read-only liveness classification: activity is evidence, never kill authority."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "relay_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("relay_diagnostics_tested", SCRIPT)
assert SPEC and SPEC.loader
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


def row(pid: int = 100, ticks: int = 0) -> dict:
    return {"pid": pid, "ppid": 1, "start_ticks": 20, "state": "S", "cpu_ticks": ticks,
            "io": {"read_bytes": 0, "write_bytes": 0}, "threads": [], "children": [], "fds": {}}


def pair() -> tuple[dict, dict]:
    before = {"monotonic_s": 1, "root": row(), "descendants": [], "journals": []}
    after = copy.deepcopy(before)
    after["monotonic_s"] = 2
    return before, after


def test_high_root_cpu_alone_does_not_declare_stall_or_authorize_recovery() -> None:
    before, after = pair()
    after["root"]["cpu_ticks"] = 100
    result = diagnostics.compare(before, after, 100)
    assert result["root_delta"]["cpu_percent"] == 100
    assert result["classification"] == "no_observed_progress_requires_investigation"


@pytest.mark.parametrize("activity", ["cpu", "io", "new_child"])
def test_worker_activity_is_visible_without_claiming_semantic_progress(activity: str) -> None:
    before, after = pair()
    after["descendants"] = [row(101)]
    if activity != "new_child":
        before["descendants"] = [row(101)]
    if activity == "cpu":
        after["descendants"][0]["cpu_ticks"] = 75
    if activity == "io":
        after["descendants"][0]["io"]["write_bytes"] = 4096
    assert diagnostics.compare(before, after, 100)["classification"] == "observed_descendant_activity"


@pytest.mark.parametrize("change", ["exit", "reuse"])
def test_root_identity_loss_overrides_apparent_worker_activity(change: str) -> None:
    before, after = pair()
    after["descendants"] = [row(101, 100)]
    if change == "exit":
        after["root"] = None
    else:
        after["root"]["start_ticks"] += 1
    assert diagnostics.compare(before, after, 100)["classification"] == "exited_or_identity_changed"


def test_journal_metadata_change_is_reported_without_reading_content(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    before, after = pair()
    before["journals"] = [diagnostics.journal_metadata(journal)]
    journal.write_text("PRIVATE-CONTENT-MUST-NOT-APPEAR")
    after["journals"] = [diagnostics.journal_metadata(journal)]
    result = diagnostics.compare(before, after, 100)
    assert result["classification"] == "observed_journal_progress"
    assert result["changed_journals"] == [str(journal)]
    assert "PRIVATE-CONTENT" not in json.dumps(after)


def test_per_thread_cpu_and_wait_channels_are_interval_measurements() -> None:
    before, after = pair()
    before["root"]["threads"] = [dict(row(101, 10), wchan="0")]
    after["root"]["threads"] = [dict(row(101, 60), wchan="pipe_write")]
    result = diagnostics.compare(before, after, 100)
    assert result["root_delta"]["threads"][0]["cpu_percent"] == 50
    assert result["root_delta"]["threads"][0]["wchan"] == "pipe_write"


def test_child_created_by_nonleader_thread_is_discovered(tmp_path: Path) -> None:
    proc = tmp_path / "proc"

    def fake_process(pid: int, parent: int, tid: int, children: str = "") -> None:
        folder = proc / str(pid)
        task = folder / "task" / str(tid)
        task.mkdir(parents=True)
        values = ["S", str(parent)] + ["0"] * 25
        values[19] = "123"
        text = f"{pid} (no command inspected) " + " ".join(values)
        (folder / "stat").write_text(text)
        (task / "stat").write_text(text)
        (task / "wchan").write_text("do_wait")
        (task / "children").write_text(children)

    fake_process(100, 1, 101, "200")
    fake_process(200, 100, 200)
    captured = diagnostics.snapshot(100, [], proc)
    assert captured["root"]["children"] == [200]
    assert [child["pid"] for child in captured["descendants"]] == [200]


def test_cli_reads_real_owned_process_metadata_without_private_content(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    journal.write_text("PRIVATE-JOURNAL")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "PRIVATE-ARGUMENT"])
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--pid", str(child.pid), "--journal", str(journal),
             "--interval", "0.1", "--samples", "2"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        data = json.loads(result.stdout)
        assert len(data["snapshots"]) == 2 and len(data["intervals"]) == 1
        assert data["snapshots"][0]["root"]["pid"] == child.pid
        assert data["snapshots"][0]["root"]["start_ticks"] > 0
        assert data["snapshots"][0]["root"]["threads"]
        assert data["snapshots"][0]["root"]["fds"].keys() == {"0", "1", "2"}
        assert "PRIVATE-JOURNAL" not in result.stdout and "PRIVATE-ARGUMENT" not in result.stdout
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("arguments", [["--samples", "7"], ["--interval", "0"], ["--interval", "nan"]])
def test_cli_refuses_unbounded_or_invalid_sampling(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--pid", str(os.getpid()), *arguments],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode == 2
