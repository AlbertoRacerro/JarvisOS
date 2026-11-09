"""Spec 218 recovery: identity across PID namespaces, persistence and safe polling."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "relay_jobs.py"
SPEC = importlib.util.spec_from_file_location("relay_jobs_recovery", SCRIPT)
assert SPEC and SPEC.loader
jobs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jobs)
JOB_ID = "j-20261026T070721Z-e6d1ba"


def process(proc: Path, pid: int, *, inner: int | None = None, start: int = 123,
            state: str = "S", namespace: str = "pid:[42]", argv: bytes | None = None) -> None:
    base = proc / str(pid)
    (base / "ns").mkdir(parents=True, exist_ok=True)
    (base / "ns/pid").symlink_to(namespace)
    fields = [state, "1", *(["0"] * 17), str(start), *(["0"] * 30)]
    (base / "stat").write_text(f"{pid} (job with spaces) " + " ".join(fields))
    (base / "status").write_text("NSpid:\t" + str(pid) + (f"\t{inner}" if inner else "") + "\n")
    (base / "cmdline").write_bytes(argv if argv is not None else
                                   b"python3\0/scripts/relay_jobs.py\0_run\0" + JOB_ID.encode() + b"\0")


def table(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-one\n")
    (proc / "self/ns").mkdir(parents=True)
    (proc / "self/ns/pid").symlink_to("pid:[42]")
    job = {"schema": jobs.SCHEMA, "id": JOB_ID, "status": "running", "pid": 47,
           "pid_start": 123, "pid_namespace": "pid:[42]", "boot_id": "boot-one"}
    jobs._PID_CACHE.clear()
    return proc, job


def test_host_namespace_translation_is_not_lost_and_reaper_uses_host_pid(tmp_path: Path) -> None:
    proc, job = table(tmp_path)
    process(proc, 250047, inner=47)
    root = tmp_path / "control"
    jobs.write_json_atomic(jobs.job_path(JOB_ID, root), job)
    assert jobs.observed_pid(job, proc) == 250047
    assert jobs.is_wrapper(job, proc)
    assert jobs.effective_status(job, proc) == "running"
    assert jobs.live_roots(root, proc) == {250047}


def test_same_namespace_observer_uses_recorded_pid(tmp_path: Path) -> None:
    proc, job = table(tmp_path)
    process(proc, 47)
    assert jobs.process_identity(47, proc) == {key: job[key] for key in
                                           ("pid", "pid_start", "pid_namespace", "boot_id")}
    assert jobs.observed_pid(job, proc) == 47
    assert jobs.effective_status(job, proc) == "running"


@pytest.mark.parametrize("mutation", ["reuse", "argv", "namespace", "boot"])
def test_translation_rejects_reuse_spoofing_wrong_namespace_and_boot(tmp_path: Path, mutation: str) -> None:
    proc, job = table(tmp_path)
    process(proc, 250047, inner=47, start=999 if mutation == "reuse" else 123,
            namespace="pid:[99]" if mutation == "namespace" else "pid:[42]",
            argv=b"python3\0-c\0_run\0" + JOB_ID.encode() + b"\0" if mutation == "argv" else None)
    if mutation == "boot":
        job["boot_id"] = "previous-boot"
    assert jobs.observed_pid(job, proc) is None
    assert not jobs.is_wrapper(job, proc)


def test_zombie_wrapper_is_lost_without_persistent_outcome(tmp_path: Path) -> None:
    proc, job = table(tmp_path)
    process(proc, 47, state="Z", argv=b"")
    assert not jobs.is_wrapper(job, proc)
    assert jobs.effective_status(job, proc) == "lost"


def test_dead_wrapper_live_identified_solver_remains_orphaned(tmp_path: Path) -> None:
    proc, job = table(tmp_path)
    argv = b"solver\0-case\0/pinned-case\0"
    process(proc, 250048, inner=48, start=124, argv=argv)
    job["child"] = {"pid": 48, "pid_start": 124, "pid_namespace": "pid:[42]", "boot_id": "boot-one",
                    "cmdline_sha256": hashlib.sha256(argv).hexdigest()}
    assert jobs.effective_status(job, proc) == "orphaned"
    root = tmp_path / "control"
    jobs.write_json_atomic(jobs.job_path(JOB_ID, root), job)
    assert jobs.live_roots(root, proc) == {250048}
    assert jobs.observed_pid(job, proc, child=True) == 250048
    job["child"]["pid_start"] = 999
    assert jobs.effective_status(job, proc) == "lost"


@pytest.mark.parametrize("outcome,code", [("succeeded", 0), ("failed", 1), ("interrupted", -15)])
def test_persistent_outcome_survives_wrapper_exit(tmp_path: Path, outcome: str, code: int) -> None:
    proc, job = table(tmp_path)
    job.update(status=outcome, exit_code=code, ended_at="2026-10-26T08:10:07Z")
    jobs.write_json_atomic(jobs.job_path(JOB_ID, tmp_path), job)
    saved = jobs.load(JOB_ID, tmp_path)
    assert saved == job
    assert jobs.effective_status(saved, proc) == outcome
    assert saved["exit_code"] == code


def test_cached_translation_revalidates_identity_without_proc_rescan(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    proc, job = table(tmp_path)
    process(proc, 250047, inner=47)
    assert jobs.observed_pid(job, proc) == 250047
    original = Path.iterdir

    def no_scan(path: Path):
        if path == proc:
            raise AssertionError("cached lookup scanned /proc")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", no_scan)
    assert jobs.observed_pid(job, proc) == 250047
    stat = proc / "250047/stat"
    stat.write_text(stat.read_text().replace("123", "999"))
    assert jobs.observed_pid(job, proc) is None


def test_linux_real_wrapper_observed_inside_and_outside_pid_namespace(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        pytest.skip("requires Linux unshare")
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", "true"],
                           capture_output=True, text=True, timeout=10)
    if probe.returncode:
        pytest.skip("unprivileged namespaces unavailable: " + probe.stderr.strip())
    root = tmp_path / "control"
    path = jobs.job_path(JOB_ID, root)
    job = {"schema": jobs.SCHEMA, "id": JOB_ID, "status": "starting", "pid": None, "pid_start": None,
           "created_at": jobs.iso(jobs.now_utc()), "deadline": jobs.iso(jobs.now_utc() + jobs.timedelta(seconds=30)),
           "argv": ["sleep", "25"], "cwd": str(tmp_path), "log": str(tmp_path / "job.log"),
           "label": "isolated namespace integration", "git": {}, "spec": "218"}
    jobs.write_json_atomic(path, job)
    driver = tmp_path / "driver.py"
    driver.write_text('''import importlib.util, json, os, subprocess, sys, time
from pathlib import Path
spec=importlib.util.spec_from_file_location("jobs", sys.argv[1]); j=importlib.util.module_from_spec(spec); spec.loader.exec_module(j)
root=Path(sys.argv[2]); jid=sys.argv[3]
p=subprocess.Popen([sys.executable, "-I", sys.argv[1], "_run", jid])
try:
    end=time.monotonic()+10
    while time.monotonic()<end:
        record=j.load(jid,root)
        if record.get("status")=="running":
            assert j.effective_status(record)=="running"
            (root/"inside.json").write_text(json.dumps({"status":"running", "pid":record["pid"]}))
            break
        time.sleep(.02)
    else: raise AssertionError("wrapper did not start")
    p.wait(timeout=35)
finally:
    (j.jobs_dir(root)/(jid+".cancel")).touch()
    p.wait(timeout=10)
''')
    env = {**os.environ, "RELAY_CONTINUITY_DIR": str(root), "JARVIS_CONTROL_DIR": str(tmp_path / "ledger")}
    proc = subprocess.Popen(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
                             sys.executable, str(driver), str(SCRIPT), str(root), JOB_ID],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    marker = jobs.jobs_dir(root) / f"{JOB_ID}.cancel"
    try:
        deadline = time.monotonic() + 12
        while not (root / "inside.json").exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(.02)
        assert (root / "inside.json").exists()
        record = jobs.load(JOB_ID, root)
        host_pid = jobs.observed_pid(record)
        assert host_pid is not None and host_pid != record["pid"]
        assert jobs.effective_status(record) == "running"
        assert host_pid in jobs.live_roots(root)
        marker.touch()
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stdout + stderr
        final = jobs.load(JOB_ID, root)
        assert final["status"] == "cancelled" and final["exit_code"] == -15
        assert jobs.effective_status(final) == "cancelled"
        # Simulate a stale running snapshot after this temporary namespace dies;
        # a previously proven host observer can establish actual loss.
        assert jobs.effective_status(record) == "lost"
        assert "cancelled" in Path(final["log"]).read_text()
    finally:
        marker.touch()
        proc.communicate(timeout=35)


def test_absent_wrapper_in_same_namespace_is_actual_loss(tmp_path: Path) -> None:
    proc, job = table(tmp_path)
    assert jobs.effective_status(job, proc) == "lost"


def test_unseen_namespace_absence_is_uncertain_without_repeated_scans(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    proc, job = table(tmp_path)
    (proc / "self/ns/pid").unlink()
    (proc / "self/ns/pid").symlink_to("pid:[99]")
    assert jobs.effective_status(job, proc) == "unknown"
    original = Path.iterdir

    def no_scan(path: Path):
        if path == proc:
            raise AssertionError("uncertain job rescanned /proc during cache lifetime")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", no_scan)
    assert jobs.effective_status(job, proc) == "unknown"


def test_child_exec_preserves_immutable_attribution(tmp_path: Path) -> None:
    proc, job = table(tmp_path)
    process(proc, 250048, inner=48, start=124, argv=b"sh\0-c\0exec solver\0")
    job["child"] = {"pid": 48, "pid_start": 124, "pid_namespace": "pid:[42]", "boot_id": "boot-one"}
    (proc / "250048/cmdline").write_bytes(b"solver\0-case\0/long-computation\0")
    assert jobs.effective_status(job, proc) == "orphaned"
    root = tmp_path / "control"
    jobs.write_json_atomic(jobs.job_path(JOB_ID, root), job)
    assert jobs.live_roots(root, proc) == {250048}


def test_duplicate_live_and_completed_work_requires_evidence_and_retry_reason(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "control"
    monkeypatch.setattr(jobs, "allowed_roots", lambda: (tmp_path,))
    monkeypatch.setattr(jobs, "ledger", lambda *args: None)
    common = {"label": "case", "authority": "spec 218 regression", "cwd": tmp_path,
              "wall_hours": 1, "root": root, "spawn": lambda cmd: None, "wait": 0}
    first = jobs.start(["true"], **common)
    with pytest.raises(ValueError, match="matching job"):
        jobs.start(["true"], retry_reason="cannot override a live case", **common)
    first.update(status="succeeded", exit_code=0)
    jobs.write_json_atomic(jobs.job_path(first["id"], root), first)
    with pytest.raises(ValueError, match="matching job"):
        jobs.start(["true"], **common)
    second = jobs.start(["true"], retry_reason="independent reproducibility measurement", **common)
    assert second["retry_reason"] == "independent reproducibility measurement"


def test_sigterm_wrapper_preserves_exit_log_and_stops_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    root = tmp_path / "control"
    monkeypatch.setattr(jobs, "allowed_roots", lambda: (tmp_path,))
    monkeypatch.setattr(jobs, "ledger", lambda *args: None)
    job = jobs.start([sys.executable, "-c", "import time; print('evidence retained', flush=True); time.sleep(30)"],
                     label="isolated termination", authority="spec 218 regression", cwd=tmp_path,
                     wall_hours=1, root=root)
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end:
            saved = jobs.load(job["id"], root)
            if saved.get("child") and "evidence retained" in Path(saved["log"]).read_text():
                break
            time.sleep(.02)
        os.kill(job["pid"], signal.SIGTERM)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            saved = jobs.load(job["id"], root)
            if saved["status"] == "interrupted":
                break
            time.sleep(.02)
        assert saved["status"] == "interrupted" and saved["exit_code"] == -signal.SIGTERM
        assert "evidence retained" in Path(saved["log"]).read_text()
        assert not jobs._alive(saved["child"]["pid"], Path("/proc"))
    finally:
        (jobs.jobs_dir(root) / f"{job['id']}.cancel").touch()


def test_wrapper_observation_error_cleans_up_before_persisting_diagnosis(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "control"
    monkeypatch.setattr(jobs, "allowed_roots", lambda: (tmp_path,))
    monkeypatch.setattr(jobs, "ledger", lambda *args: None)
    job = jobs.start(["unused"], label="isolated error", authority="spec 218 regression", cwd=tmp_path,
                     wall_hours=1, root=root, spawn=lambda cmd: None, wait=0)

    class Child:
        pid = 987654321

        def wait(self):
            return -15

    events = []
    real_write = jobs.write_json_atomic
    failed = False

    def write(path, data):
        nonlocal failed
        if data.get("child") and not failed:
            failed = True
            raise OSError("injected temporary persistence failure")
        if data["status"] == "interrupted":
            assert events == ["cleanup"]
        real_write(path, data)

    monkeypatch.setattr(jobs, "write_json_atomic", write)
    monkeypatch.setattr(jobs, "become_subreaper", lambda: True)
    monkeypatch.setattr(jobs.signal, "signal", lambda *args: None)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(jobs, "_stop_tree", lambda child, grace: events.append("cleanup") or 0)
    assert jobs.run_wrapper(job["id"], root) == 0
    saved = jobs.load(job["id"], root)
    assert saved["status"] == "interrupted" and saved["exit_code"] == -15
    assert saved["reason"] == "wrapper error: OSError"
    assert "wrapper error: OSError" in Path(saved["log"]).read_text()


@pytest.mark.parametrize(("status", "minutes_ago", "should_succeed"), [
    ("unknown", 20, True),
    ("unknown", 5, False),
    ("running", 20, False),
])
def test_stale_unknown_records_do_not_fill_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, minutes_ago: int, should_succeed: bool,
) -> None:
    root = tmp_path / "control"
    monkeypatch.setattr(jobs, "allowed_roots", lambda: (tmp_path,))
    monkeypatch.setattr(jobs, "ledger", lambda *args: None)
    common = {"label": "case", "authority": "spec 218 regression", "cwd": tmp_path,
              "wall_hours": 1, "root": root, "spawn": lambda cmd: None, "wait": 0}
    now = jobs.now_utc()
    deadline = (now - jobs.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(4):
        jid = f"j-20261109T12000{i}Z-aaaaa{i}"
        record = {
            "schema": jobs.SCHEMA,
            "id": jid,
            "label": f"job-{i}",
            "authority": "spec 254 test",
            "argv": [f"cmd-{i}"],
            "cwd": str(tmp_path),
            "git": {},
            "inputs": {},
            "wall_s": 3600,
            "created_at": jobs.iso(now - jobs.timedelta(hours=1)),
            "deadline": deadline,
            "status": "running",
            "pid": None,
            "pid_start": None,
            "exit_code": None,
            "ended_at": None,
        }
        jobs.write_json_atomic(jobs.job_path(jid, root), record)
    monkeypatch.setattr(jobs, "effective_status", lambda job: status)
    if should_succeed:
        started = jobs.start(["true"], **common)
        assert started["status"] == "starting"
    else:
        with pytest.raises(ValueError, match=f"{jobs.MAX_RUNNING} jobs already running"):
            jobs.start(["true"], **common)

