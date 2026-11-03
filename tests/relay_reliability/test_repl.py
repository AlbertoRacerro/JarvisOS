"""Offline, parent-bounded Relay REPL/session proof; never touches live sessions."""

import contextlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pyte
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_fixture(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".config/relay"
    config.mkdir(parents=True)
    (config / "config.toml").write_text('[repl]\ndefault_agent = "claude"\ntmux_auto = false\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=5)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Offline Test", "-c", "user.email=offline@example.invalid", "commit", "--allow-empty", "-qm", "isolated fixture"], check=True, timeout=5)
    fake = tmp_path / "bin"
    fake.mkdir()
    command = fake / "claude"
    command.write_text(f'''#!{sys.executable}
import json, pathlib, signal, subprocess, sys, time
base = pathlib.Path({str(tmp_path)!r})
if "--version" in sys.argv:
    print("fake-offline-847")
    raise SystemExit(0)
mode = (base / "mode").read_text() if (base / "mode").exists() else "normal"
(base / "started").write_text(str(__import__("os").getpid()))
subprocess.run([sys.executable, {str(ROOT / "scripts/devctx.py")!r}, "--repo", {str(repo)!r}, "note", "attempt", "Isolated Lumen Light v2.1 reliability proof"], env={{**__import__("os").environ, "JARVIS_CONTROL_DIR": str(base / "control")}}, check=True, stdout=subprocess.DEVNULL, timeout=5)
if mode == "interrupt":
    signal.signal(signal.SIGINT, lambda *_: ((base / "interrupted").write_text("yes"), sys.exit(130)))
    time.sleep(30)
else:
    time.sleep(1.5)
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "image", "source": {{"type": "base64", "data": "a" * 604628}}}}]}}}}), flush=True)
print(json.dumps({{"type": "assistant", "message": {{"role": "assistant", "content": [{{"type": "text", "text": "RELAY847_COMPLETED"}}]}}}}), flush=True)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "RELAY847_COMPLETED", "session_id": "fake-847"}}), flush=True)
''')
    command.chmod(0o755)
    # Explicit minimal environment: no inherited credentials or live config.
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "PATH": f"{fake}:/usr/bin:/bin", "TERM": "xterm-256color", "LANG": "C.UTF-8"}
    try:
        yield repo, env
    finally:
        # A turn starts its own process group. Reap only this fixture's fake
        # executable, including when the externally bounded session times out.
        started = tmp_path / "started"
        if started.exists():
            pid = int(started.read_text())
            with contextlib.suppress(FileNotFoundError, ProcessLookupError):
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if os.fsencode(command) in argv and Path(f"/proc/{pid}/cwd").resolve() == repo:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)


def cpu_seconds(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


@pytest.mark.parametrize("mode", ["normal", "interrupt"])
def test_turn_session_large_output_and_graceful_interrupt(tmp_path, mode, isolated_fixture):
    _repo, env = isolated_fixture
    (tmp_path / "mode").write_text(mode)
    program = '''
import json, pathlib, runpy, sys, threading, time
runpy.run_path(sys.argv[1])["apply_repair"]()
from agent_relay.repl import agent_session
from agent_relay.repl.agent_session import TurnBasedAgentSession, SessionOutputDone
collector_started = threading.Event()
original_detector = agent_session.user_facing_run_error
def slow_detector(*args):
    collector_started.set()
    time.sleep(.25)
    return original_detector(*args)
agent_session.user_facing_run_error = slow_detector
base = pathlib.Path(sys.argv[2])
session = TurnBasedAgentSession("claude", cwd=base / "repo")
try:
    session.write("offline reliability proof")
    deadline = time.monotonic() + 8
    while not (base / "started").exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert (base / "started").exists(), "child did not start"
    if sys.argv[3] == "interrupt":
        time.sleep(.3)
        session.interrupt()
    else:
        assert collector_started.wait(timeout=5), "collector did not start"
        assert session._proc.poll() is not None, "fixture subprocess should have exited"
        assert session.busy, "session stopped being busy before output collector finished"
    chunks = []
    while time.monotonic() < deadline:
        chunk = session.read(timeout=.05)
        if isinstance(chunk, SessionOutputDone):
            break
        chunks.append(str(chunk))
    else:
        raise AssertionError("turn did not finish")
    if sys.argv[3] == "normal":
        assert "RELAY847_COMPLETED" in "".join(chunks)
        assert session.provider_session_id == "fake-847"
    else:
        assert (base / "interrupted").exists()
    assert not session.busy
    assert (base / "control/ledger.jsonl").exists()
    print(json.dumps({"mode": sys.argv[3], "completed": True, "exit": session.exit_code}))
finally:
    session.terminate(grace=.2)
'''
    result = subprocess.run([sys.executable, "-c", program, str(ROOT / "scripts/relay_safe.py"), str(tmp_path), mode],
                            env=env, text=True, capture_output=True, timeout=12, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


class OwnedPTY:
    def __init__(self, repo, env):
        import fcntl
        import pty
        import struct
        import termios
        self.proof_dir = repo.parent
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        bootstrap = "import fcntl, os, runpy, termios, sys; os.setsid(); fcntl.ioctl(0, termios.TIOCSCTTY, 0); path = sys.argv.pop(1); runpy.run_path(path, run_name='__main__')"
        self.proc = subprocess.Popen([sys.executable, "-c", bootstrap, str(ROOT / "scripts/relay_safe.py"), "--no-tmux", "--no-onboarding"],
                                     cwd=repo, env=env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.output = b""
        self.screen = pyte.Screen(120, 40)
        self.screen.write_process_input = lambda response: os.write(self.master, response.encode())
        self.screen_stream = pyte.ByteStream(self.screen)

    def pump(self):
        """Drain output and emulate cursor reports during every terminal phase."""
        if not select.select([self.master], [], [], .05)[0]:
            return True
        try:
            data = os.read(self.master, 65536)
        except OSError:
            return False
        if not data:
            return False
        self.output += data
        # Parse actual terminal state. DSR/CPR replies use the real cursor,
        # including escape sequences split across reads and the final exit.
        self.screen_stream.feed(data)
        return True

    def read_until(self, marker, timeout=8):
        deadline = time.monotonic() + timeout
        start = len(self.output)
        while time.monotonic() < deadline:
            visible = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", self.output[start:])
            if marker in visible:
                return
            if not self.pump():
                break
            if self.proc.poll() is not None:
                break
        (self.proof_dir / "terminal-output.bin").write_bytes(self.output)
        clean = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", self.output)
        lines = [line.strip() for line in clean.splitlines() if line.strip() and len(set(line.strip())) > 3]
        raise AssertionError(f"PTY did not emit {marker!r}: {lines[-25:]!r}")

    def send(self, text):
        # Footer paint precedes editor readiness. Require the complete command
        # to be visibly echoed by the real editor before submitting Enter;
        # never retry a command whose side effects may already have started.
        typed = text.rstrip("\r")
        for character in typed:
            os.write(self.master, character.encode())
            time.sleep(.03)
        # Slash suggestions can contain the full command before the editor
        # has received it. Match the input row, never an autocomplete entry.
        self.read_until(b"> " + typed.encode(), timeout=5)
        if text.endswith("\r"):
            os.write(self.master, b"\r")

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=2)
        if self.master is not None:
            os.close(self.master)
            self.master = None


def test_actual_pty_repl_large_output_idle_disconnect_and_resume(tmp_path, isolated_fixture):
    repo, env = isolated_fixture
    terminal = OwnedPTY(repo, env)
    evidence = {}
    try:
        terminal.read_until(b"/help for commands", 10)
        time.sleep(.3)
        terminal.send("offline proof\r")
        terminal.read_until(b"RELAY847_COMPLETED", 15)
        time.sleep(.3)
        before = cpu_seconds(terminal.proc.pid)
        time.sleep(1)
        evidence["idle_cpu_seconds_over_one_second"] = cpu_seconds(terminal.proc.pid) - before
        assert evidence["idle_cpu_seconds_over_one_second"] < .3
        terminal.send("/help\r")
        terminal.read_until(b"Print a shareable diagnostic bundle", 5)
        journal_files = list(repo.rglob("*.jsonl"))
        assert journal_files, "REPL did not persist session artifacts"
        preserved = {str(p): p.read_bytes() for p in journal_files}
        os.close(terminal.master)
        terminal.master = None
        terminal.proc.wait(timeout=5)
        assert all(Path(p).read_bytes() == data for p, data in preserved.items())
        evidence["disconnect_preserved_journals"] = True
    finally:
        terminal.close()
    recovered = OwnedPTY(repo, env)
    try:
        recovered.read_until(b"/help for commands", 10)
        time.sleep(.3)
        recovered.send("/resume 1\r")
        recovered.read_until(b"resumed session", 8)
        time.sleep(.3)
        recovered.send("/exit\r")
        deadline = time.monotonic() + 5
        while recovered.proc.poll() is None and time.monotonic() < deadline:
            if not recovered.pump():
                break
        (tmp_path / "exit-output.bin").write_bytes(recovered.output)
        recovered.proc.wait(timeout=1)
        assert recovered.proc.returncode == 0
        evidence["resume_succeeded"] = True
    finally:
        recovered.close()
    (tmp_path / "proof.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence))
