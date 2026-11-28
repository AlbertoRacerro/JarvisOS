"""Tests for Spec 388 — AGENT-EVAL-EXPLORER-1.

Covers:
1. Request-set generator (explore-gen): determinism, family weighting (>= 60%),
   dev/heldout split, interactive intent/preferences, and zero private terms.
2. reference_shim.py explorer mode: allowlists for claude/agy, fail-closed reference mode,
   command construction, runner env stripping, live loopback HTTP completions,
   and --zone-a validation.
3. extract_thread_tool_trace: deterministic SQLite read-only join of hermes events,
   duration calculation, bare tool naming, and multi-thread isolation.
4. parse_work_report & needs_clarification: tolerant parsing of structured reports,
   empty/refusal fallback without exceptions, and clarification phrase heuristics.
5. run_exploration: multi-turn simulated maintainer loop, vault-path refusal,
   loopback host and port 8000 validation, and atomic resumable checkpoints.
6. mine_exploration_logs: transitions, frequent chains, cross-model agreement,
   loops/retries, parallel bursts, and wish-list reuse checks against Jarvis capabilities.

All tests run offline without network access or GPU interaction.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import vault_guard  # noqa: E402
from scripts.benchmarks.agent_eval.explorer import (  # noqa: E402
    DESIGN_FAMILIES,
    RESEARCH_FAMILIES,
    SimulatedMaintainer,
    extract_thread_tool_trace,
    generate_request_set,
    needs_clarification,
    parse_work_report,
    run_exploration,
    wrap_explorer_prompt,
)
from scripts.benchmarks.agent_eval.miner import (  # noqa: E402
    format_mining_report,
    load_known_capabilities,
    mine_exploration_logs,
)
from scripts.benchmarks.agent_eval.reference_shim import (  # noqa: E402
    build_agy_command,
    default_agy_runner,
    is_allowed_explorer_model,
    is_allowed_reference_model,
    make_reference_shim_server,
    validate_tasks_file,
)
from scripts.benchmarks.agent_eval.runner import (  # noqa: E402
    main as runner_main,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure loopback requests are never routed through ambient proxy."""
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")


def _http_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# -----------------------------------------------------------------------------
# 1. Request-Set Generator (explore-gen)
# -----------------------------------------------------------------------------


def test_generate_request_set_determinism() -> None:
    set1 = generate_request_set(count=24, heldout_fraction=0.25, seed=42)
    set2 = generate_request_set(count=24, heldout_fraction=0.25, seed=42)
    assert set1 == set2
    assert set1["set_id"] == "explorer_public_v1"
    assert set1["cloud_ok"] is True
    assert len(set1["tasks"]) == 24

    set_diff_seed = generate_request_set(count=24, heldout_fraction=0.25, seed=99)
    # Different seed should shuffle splits differently
    assert set1 != set_diff_seed


def test_generate_request_set_family_weighting() -> None:
    for count in (20, 24, 40):
        data = generate_request_set(count=count, heldout_fraction=0.25, seed=42)
        tasks = data["tasks"]
        assert len(tasks) == count

        primary_families = set(DESIGN_FAMILIES + RESEARCH_FAMILIES)
        primary_count = sum(1 for t in tasks if t["family"] in primary_families)
        fraction = primary_count / count
        # Spec 388: weighted >= 60% towards design and research
        assert fraction >= 0.60, f"Expected >= 60% primary families, got {fraction:.2f}"


def test_generate_request_set_splits_and_interactive() -> None:
    data = generate_request_set(count=40, heldout_fraction=0.25, seed=42)
    tasks = data["tasks"]
    heldout_count = sum(1 for t in tasks if t["split"] == "heldout")
    dev_count = sum(1 for t in tasks if t["split"] == "dev")

    assert heldout_count == 10
    assert dev_count == 30

    interactive_tasks = [t for t in tasks if t["interactive"]]
    assert len(interactive_tasks) > 0
    for t in interactive_tasks:
        assert isinstance(t.get("user_intent"), str) and t["user_intent"].strip()
        assert isinstance(t.get("preferences"), str) and t["preferences"].strip()


def test_generate_request_set_zero_private_terms() -> None:
    data = generate_request_set(count=40, seed=42)
    raw = json.dumps(data).lower()
    # Ensure standard public repository hygiene (no private IP, customer names, etc.)
    forbidden = ["secret", "internal_customer", "classified", "confidential"]
    for word in forbidden:
        assert word not in raw


def test_explore_gen_cli(tmp_path: Path) -> None:
    out_file = tmp_path / "gen_requests.json"
    code = runner_main(["explore-gen", "--out", str(out_file), "--count", "12", "--seed", "123"])
    assert code == 0
    assert out_file.is_file()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(data["tasks"]) == 12


# -----------------------------------------------------------------------------
# 2. reference_shim.py Explorer Mode & Safety
# -----------------------------------------------------------------------------


def test_is_allowed_explorer_model() -> None:
    # Claude provider
    assert is_allowed_explorer_model("opus", "claude")
    assert is_allowed_explorer_model("claude-opus-4", "claude")
    assert is_allowed_explorer_model("sonnet", "claude")
    assert is_allowed_explorer_model("claude-sonnet-4", "claude")
    assert is_allowed_explorer_model("haiku", "claude")
    assert is_allowed_explorer_model("claude-haiku-4", "claude")
    assert not is_allowed_explorer_model("gemini-4-neon-medium", "claude")
    assert not is_allowed_explorer_model("gpt-4", "claude")

    # Agy provider
    assert is_allowed_explorer_model("gemini-4-neon-medium", "agy")
    assert is_allowed_explorer_model("gemini-3.8-flash-high", "agy")
    assert is_allowed_explorer_model("claude-opus-4", "agy")
    assert is_allowed_explorer_model("claude-sonnet-4", "agy")
    assert not is_allowed_explorer_model("haiku", "agy")
    assert not is_allowed_explorer_model("gpt-4", "agy")

    # Unknown provider
    assert not is_allowed_explorer_model("opus", "unknown")


def test_reference_mode_strictly_fail_closed() -> None:
    # Reference mode only permits haiku
    assert is_allowed_reference_model("haiku")
    assert is_allowed_reference_model("claude-haiku-3.5")
    assert not is_allowed_reference_model("opus")
    assert not is_allowed_reference_model("gemini-4-neon-medium")

    # Creating a reference server with agy provider must fail with ValueError
    with pytest.raises(ValueError, match="not supported in reference mode"):
        make_reference_shim_server(mode="reference", provider="agy")

    with pytest.raises(ValueError, match="not an allowed reference model"):
        make_reference_shim_server(mode="reference", provider="claude", model="opus")


def test_build_agy_command_flags() -> None:
    cmd = build_agy_command("Hello model", "gemini-4-neon-medium")
    assert cmd == [
        "agy",
        "-p",
        "Hello model",
        "--model",
        "gemini-4-neon-medium",
        "--output-format",
        "json",
        "--disable-slash-commands",
        "--mode",
        "plan",
    ]

    # With system prompt and json schema
    cmd_full = build_agy_command(
        "Generate JSON",
        "gemini-4-neon-medium",
        system_prompt="You are an engineer.",
        json_schema='{"type":"object"}',
    )
    assert cmd_full[2] == "You are an engineer.\n\nGenerate JSON"
    assert "--json-schema" in cmd_full
    assert cmd_full[cmd_full.index("--json-schema") + 1] == '{"type":"object"}'


def test_default_agy_runner_env_stripping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def mock_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result":"ok"}', stderr="")

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-anthropic")
    monkeypatch.setenv("GH_TOKEN", "secret-gh")
    monkeypatch.setenv("GIT_CONFIG", "secret-git")
    monkeypatch.setenv("SAFE_VAR", "safe-value")

    default_agy_runner(["agy", "-p", "test"], timeout=10.0)

    env = captured["env"]
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "GIT_CONFIG" not in env
    assert env.get("SAFE_VAR") == "safe-value"


def test_zone_a_task_validation(tmp_path: Path) -> None:
    task_file = tmp_path / "local_tasks.json"
    task_file.write_text(json.dumps({"set_id": "zone_a", "cloud_ok": False, "tasks": []}), encoding="utf-8")

    # Regular validation requires cloud_ok: true
    with pytest.raises(ValueError, match="cloud_ok != True"):
        validate_tasks_file(task_file, allow_zone_a=False)

    # zone_a=True allows non-vault local task set
    data = validate_tasks_file(task_file, allow_zone_a=True)
    assert data["set_id"] == "zone_a"

    # Vault path must still fail-closed regardless of zone_a
    vault_root = vault_guard.vault_root()
    if vault_root:
        vault_fake = vault_root / "tasks.json"
        with pytest.raises(PermissionError):
            validate_tasks_file(vault_fake, allow_zone_a=True)


@contextmanager
def running_explorer_shim(runner_fn: Any, model: str = "gemini-4-neon-medium") -> Iterator[str]:
    server = make_reference_shim_server(
        host="127.0.0.1",
        port=0,
        model=model,
        mode="explorer",
        provider="agy",
        runner=runner_fn,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_reference_shim_explorer_http_completions() -> None:
    # Stub runner returning agy-style JSON with 'response' field
    def stub_runner(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
        payload = {
            "response": "Here is the calculation for the heat exchanger.",
            "usage": {"input_tokens": 120, "output_tokens": 45},
        }
        return 0, json.dumps(payload), ""

    with running_explorer_shim(stub_runner) as base_url:
        req_data = {
            "model": "gemini-4-neon-medium",
            "messages": [{"role": "user", "content": "Size the heat exchanger"}],
        }
        opener = _http_opener()
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps(req_data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        assert body["object"] == "chat.completion"
        assert body["model"] == "gemini-4-neon-medium"
        assert "heat exchanger" in body["choices"][0]["message"]["content"]
        assert body["usage"]["prompt_tokens"] == 120
        assert body["usage"]["completion_tokens"] == 45


# -----------------------------------------------------------------------------
# 3. Tool Trace Extraction from SQLite DB
# -----------------------------------------------------------------------------


def _create_test_proof_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            workspace_id TEXT,
            event_type TEXT,
            actor TEXT,
            target_type TEXT,
            target_id TEXT,
            payload TEXT,
            created_at TEXT
        )
        """
    )

    events = [
        # Thread 1, call 1 (started then completed)
        (
            "e1", "w1", "hermes.tool_started", "jarvis", "agent_tool_call", "call-001",
            json.dumps({"call_id": "call-001", "tool_name": "mcp__jarvis__jarvis_paper_search"}),
            "2026-11-27T10:00:00.000Z",
        ),
        (
            "e2", "w1", "hermes.tool_result", "jarvis", "agent_tool_call", "call-001",
            json.dumps({
                "call_id": "call-001",
                "tool_name": "mcp__jarvis__jarvis_paper_search",
                "capability_id": "jarvis.paper_search",
                "status": "succeeded",
                "error_code": None,
                "session_ref": {"jarvis_thread_id": "thread-target"},
                "arguments": {"query": "creep kinetics"},
                "result_digest": "sha256:1111",
                "evidence_refs": ["ev-1"],
            }),
            "2026-11-27T10:00:01.500Z",
        ),
        # Thread 1, call 2 (started then completed)
        (
            "e3", "w1", "hermes.tool_started", "jarvis", "agent_tool_call", "call-002",
            json.dumps({"call_id": "call-002", "tool_name": "mcp__jarvis__jarvis_bench_run"}),
            "2026-11-27T10:00:02.000Z",
        ),
        (
            "e4", "w1", "hermes.tool_result", "jarvis", "agent_tool_call", "call-002",
            json.dumps({
                "call_id": "call-002",
                "tool_name": "mcp__jarvis__jarvis_bench_run",
                "capability_id": "jarvis.bench_run",
                "status": "succeeded",
                "error_code": None,
                "session_ref": {"jarvis_thread_id": "thread-target"},
                "arguments": {"solver": "foam"},
                "result_digest": "sha256:2222",
                "evidence_refs": [],
            }),
            "2026-11-27T10:00:05.000Z",
        ),
        # Different thread (thread-other) - should be ignored
        (
            "e5", "w1", "hermes.tool_started", "jarvis", "agent_tool_call", "call-999",
            json.dumps({"call_id": "call-999", "tool_name": "mcp__jarvis__jarvis_repo_read"}),
            "2026-11-27T10:00:06.000Z",
        ),
        (
            "e6", "w1", "hermes.tool_result", "jarvis", "agent_tool_call", "call-999",
            json.dumps({
                "call_id": "call-999",
                "tool_name": "mcp__jarvis__jarvis_repo_read",
                "capability_id": "jarvis.repo_read",
                "status": "succeeded",
                "session_ref": {"jarvis_thread_id": "thread-other"},
            }),
            "2026-11-27T10:00:07.000Z",
        ),
    ]

    for ev in events:
        conn.execute(
            "INSERT INTO events (id, workspace_id, event_type, actor, target_type, target_id, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ev,
        )
    conn.commit()
    conn.close()


def test_extract_thread_tool_trace(tmp_path: Path) -> None:
    db_file = tmp_path / "jarvisos.db"
    _create_test_proof_db(db_file)

    trace = extract_thread_tool_trace(tmp_path, "thread-target")
    assert len(trace) == 2

    # Check call 1
    c1 = trace[0]
    assert c1["seq"] == 1
    assert c1["call_id"] == "call-001"
    assert c1["tool_name"] == "jarvis_paper_search"  # stripped mcp__jarvis__
    assert c1["capability_id"] == "jarvis.paper_search"
    assert c1["status"] == "succeeded"
    assert c1["error_code"] is None
    assert c1["duration_ms"] == 1500.0
    assert c1["result_digest"] == "sha256:1111"
    assert c1["evidence_refs"] == ["ev-1"]
    assert c1["arguments"] == {"query": "creep kinetics"}

    # Check call 2
    c2 = trace[1]
    assert c2["seq"] == 2
    assert c2["call_id"] == "call-002"
    assert c2["tool_name"] == "jarvis_bench_run"
    assert c2["duration_ms"] == 3000.0


# -----------------------------------------------------------------------------
# 4. Work Report & Clarification Heuristics
# -----------------------------------------------------------------------------


def test_wrap_explorer_prompt() -> None:
    wrapped = wrap_explorer_prompt("Design the pipe network.")
    assert "Design the pipe network." in wrapped
    assert "STEPS_TAKEN:" in wrapped
    assert "WORKAROUNDS:" in wrapped
    assert "WISH_LIST:" in wrapped
    assert "work report for engineering telemetry" in wrapped


def test_parse_work_report_full() -> None:
    text = (
        "I have performed the analysis.\n\n"
        "STEPS_TAKEN:\n"
        "- searched literature for laminar flow models\n"
        "- calculated friction factor using Colebrook formula\n"
        "\n"
        "WORKAROUNDS:\n"
        "- solved friction factor iteratively in python due to missing implicit solver\n"
        "\n"
        "WISH_LIST:\n"
        "- dedicated pipe network pressure drop solver tool\n"
        "- automated fluids property lookup capability\n"
    )
    parsed = parse_work_report(text)
    assert len(parsed["steps_taken"]) == 2
    assert "friction factor" in parsed["steps_taken"][1]
    assert len(parsed["workarounds"]) == 1
    assert "implicit solver" in parsed["workarounds"][0]
    assert len(parsed["wish_list"]) == 2
    assert "pipe network" in parsed["wish_list"][0]


def test_parse_work_report_partial_and_plain() -> None:
    # Partial
    partial_text = "STEPS_TAKEN:\n- step 1\n- step 2\n"
    parsed_partial = parse_work_report(partial_text)
    assert len(parsed_partial["steps_taken"]) == 2
    assert parsed_partial["workarounds"] == []
    assert parsed_partial["wish_list"] == []

    # Plain text / refusal (never raises)
    plain_text = "I cannot complete this request because the data is unavailable."
    parsed_plain = parse_work_report(plain_text)
    assert parsed_plain == {"steps_taken": [], "workarounds": [], "wish_list": []}

    empty_parsed = parse_work_report("")
    assert empty_parsed == {"steps_taken": [], "workarounds": [], "wish_list": []}


def test_needs_clarification() -> None:
    assert needs_clarification("Should I use 316L stainless steel or titanium?")
    assert needs_clarification("Do you want me to proceed with the high-resolution mesh?")
    assert needs_clarification("Vuoi che utilizzi una tolleranza piu stretta?")
    assert needs_clarification("Which one do you prefer: k-epsilon or k-omega?")
    assert needs_clarification("Can you please confirm the operating temperature?")

    # Not clarifying
    assert not needs_clarification("I have computed the pressure drop to be 14.2 kPa.")
    assert not needs_clarification("Analysis complete. See results below:\n| Val | Unit |\n| 10 | mm |")
    assert not needs_clarification("")


def test_simulated_maintainer() -> None:
    maintainer = SimulatedMaintainer()
    task = {
        "user_intent": "verify M6 concentricity",
        "preferences": "prefer 316L stainless",
    }
    turns = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "Should I use M6 or M8?"}]
    reply = maintainer.respond(task, turns)
    assert "verify M6 concentricity" in reply
    assert "prefer 316L stainless" in reply

    # Custom responder
    custom_maintainer = SimulatedMaintainer(responder=lambda t, turns: "Use M6 please.")
    assert custom_maintainer.respond(task, turns) == "Use M6 please."


# -----------------------------------------------------------------------------
# 5. Exploration Execution Loop & Resumability
# -----------------------------------------------------------------------------


class _StubProofHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        body = json.loads(raw) if raw else {}

        if self.path.startswith("/ai/threads") and "interactions" not in self.path:
            self.server.thread_counter += 1  # type: ignore[attr-defined]
            self._send_json(200, {"id": f"th-{self.server.thread_counter}"})  # type: ignore[attr-defined]
            return

        if "/interactions" in self.path:
            self.server.interaction_counter += 1  # type: ignore[attr-defined]
            prompt = str(body.get("prompt", ""))
            # If prompt has not been clarified yet, ask clarification
            p_lower = prompt.lower()
            if "should i use" not in p_lower and "clarif" not in p_lower and self.server.allow_clarify:  # type: ignore[attr-defined]
                assistant_text = "Should I use material A or material B for this sizing?"
            else:
                assistant_text = (
                    "Sizing completed successfully using chosen material.\n\n"
                    "STEPS_TAKEN:\n- ran sizing calculation\n"
                    "WORKAROUNDS:\n- manual lookup\n"
                    "WISH_LIST:\n- automated material database\n"
                )

            interaction = {
                "id": f"int-{self.server.interaction_counter}",  # type: ignore[attr-defined]
                "flow_state": "completed",
                "assistant_text": assistant_text,
                "terminal_reason": None,
            }
            self.server.last_interaction = interaction  # type: ignore[attr-defined]
            self._send_json(200, {"interaction": interaction})
            return

        self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if "/ai/threads/" in self.path:
            self._send_json(200, {
                "id": "th-1",
                "interactions": [self.server.last_interaction],  # type: ignore[attr-defined]
            })
            return
        self._send_json(404, {"error": "not found"})


@contextmanager
def running_stub_proof_backend(*, allow_clarify: bool = True) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _StubProofHandler)
    server.thread_counter = 0  # type: ignore[attr-defined]
    server.interaction_counter = 0  # type: ignore[attr-defined]
    server.allow_clarify = allow_clarify  # type: ignore[attr-defined]
    server.last_interaction = {}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_run_exploration_with_clarification_and_resume(tmp_path: Path) -> None:
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    _create_test_proof_db(data_root / "jarvisos.db")

    tasks_file = tmp_path / "requests.json"
    tasks_data = {
        "set_id": "test_explore",
        "tasks": [
            {
                "id": "t1",
                "prompt": "Size the pipe.",
                "family": "cfd",
                "interactive": True,
                "user_intent": "use standard pipe",
                "preferences": "prefer schedule 40",
            },
            {
                "id": "t2",
                "prompt": "Compute stress.",
                "family": "fem",
                "interactive": False,
            },
        ],
    }
    tasks_file.write_text(json.dumps(tasks_data), encoding="utf-8")

    out_dir = tmp_path / "out"

    maintainer = SimulatedMaintainer(
        responder=lambda t, turns: f"Clarification: {t.get('user_intent', 'proceed')}"
    )

    with running_stub_proof_backend(allow_clarify=True) as base_url:
        res = run_exploration(
            requests_path=tasks_file,
            base_url=base_url,
            out_dir=out_dir,
            data_root=data_root,
            simulated_maintainer=maintainer,
            resume=False,
        )

        assert res["complete"] is True
        assert len(res["requests"]) == 2

        # Check multi-turn transcript on interactive request t1
        t1_record = res["requests"][0]
        assert t1_record["request_id"] == "t1"
        assert len(t1_record["transcript"]) >= 4  # user -> assistant ask -> user clarify -> assistant answer
        assert "Clarification: use standard pipe" in t1_record["transcript"][2]["content"]
        assert len(t1_record["work_report"]["steps_taken"]) == 1

        # Check files created
        log_file = out_dir / "exploration_log.json"
        traces_file = out_dir / "traces.jsonl"
        assert log_file.is_file()
        assert traces_file.is_file()

        # Test resume skips already-completed requests
        res2 = run_exploration(
            requests_path=tasks_file,
            base_url=base_url,
            out_dir=out_dir,
            data_root=data_root,
            simulated_maintainer=maintainer,
            resume=True,
        )
        assert len(res2["requests"]) == 2


def test_run_exploration_safety_refusals(tmp_path: Path) -> None:
    dummy_tasks = tmp_path / "t.json"
    dummy_tasks.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    # Port 8000 refusal
    with pytest.raises(ValueError, match="Port 8000 is refused"):
        run_exploration(
            requests_path=dummy_tasks,
            base_url="http://127.0.0.1:8000",
            out_dir=tmp_path / "out",
            data_root=tmp_path,
        )

    # Non-loopback host refusal
    with pytest.raises(ValueError, match="not a loopback interface"):
        run_exploration(
            requests_path=dummy_tasks,
            base_url="http://example.com:8080",
            out_dir=tmp_path / "out",
            data_root=tmp_path,
        )

    # Vault path refusal
    vault_root = vault_guard.vault_root()
    if vault_root:
        with pytest.raises(PermissionError):
            run_exploration(
                requests_path=vault_root / "tasks.json",
                base_url="http://127.0.0.1:8088",
                out_dir=tmp_path / "out",
                data_root=tmp_path,
            )


# -----------------------------------------------------------------------------
# 6. Process Mining & Capability Reuse Check
# -----------------------------------------------------------------------------


def test_load_known_capabilities() -> None:
    caps = load_known_capabilities()
    assert isinstance(caps, dict)
    assert "jarvis_paper_search" in caps or "paper_search" in caps
    assert "jarvis.paper_search" in caps.values()
    assert "jarvis.bench_run" in caps.values()


def test_mine_exploration_logs(tmp_path: Path) -> None:
    # Synthetic Model 1 log
    log1 = {
        "model": "model-alpha",
        "requests": [
            {
                "request_id": "req-1",
                "tool_trace": [
                    {"tool_name": "jarvis_paper_search", "status": "succeeded", "started_at": 1000, "completed_at": 2000},
                    {"tool_name": "jarvis_python", "status": "succeeded", "started_at": 2100, "completed_at": 3000},
                    {"tool_name": "jarvis_bench_run", "status": "succeeded", "started_at": 3100, "completed_at": 4000},
                ],
                "work_report": {
                    "workarounds": ["approximated fluid density using python"],
                    "wish_list": ["missing paper search tool with full-text search", "pipe friction database"],
                },
            },
            {
                "request_id": "req-2",
                "tool_trace": [
                    {"tool_name": "jarvis_dev_read", "status": "succeeded", "started_at": 5000, "completed_at": 6000},
                    # Repeated tool call
                    {"tool_name": "jarvis_dev_read", "status": "succeeded", "started_at": 6100, "completed_at": 7000},
                ],
                "work_report": {
                    "workarounds": [],
                    "wish_list": ["automatic bench run orchestrator"],
                },
            },
        ],
    }

    # Synthetic Model 2 log
    log2 = {
        "model": "model-beta",
        "requests": [
            {
                "request_id": "req-1",
                "tool_trace": [
                    # Same paper_search and python, but divergent CAD tool
                    {"tool_name": "jarvis_paper_search", "status": "succeeded", "started_at": 1000, "completed_at": 2000},
                    # Overlapping / parallel burst (next started before prev completed)
                    {"tool_name": "jarvis_python", "status": "failed", "error_code": "runtime_err", "started_at": 1800, "completed_at": 2500},
                    {"tool_name": "jarvis_bluecad_read", "status": "succeeded", "started_at": 2600, "completed_at": 3500},
                ],
                "work_report": {
                    "workarounds": ["manual script calculation"],
                    "wish_list": ["automated bench test runner"],
                },
            },
        ],
    }

    p1 = tmp_path / "log_alpha.json"
    p2 = tmp_path / "log_beta.json"
    p1.write_text(json.dumps(log1), encoding="utf-8")
    p2.write_text(json.dumps(log2), encoding="utf-8")

    report = mine_exploration_logs([p1, p2], min_support=2)

    # 1. Transitions
    transitions = report["transitions"]
    assert len(transitions) > 0
    t_map = {(t["from"], t["to"]): t for t in transitions}
    assert ("jarvis_paper_search", "jarvis_python") in t_map
    assert t_map[("jarvis_paper_search", "jarvis_python")]["count"] == 2

    # 2. Frequent chains
    chains = report["frequent_chains"]
    assert any(c["chain"] == ["jarvis_paper_search", "jarvis_python"] for c in chains)

    # 3. Cross-model comparison
    cross_model = report["cross_model"]
    per_req = {r["request_id"]: r for r in cross_model["per_request"]}
    r1 = per_req["req-1"]
    assert "jarvis_paper_search" in r1["common_tools"]
    assert "jarvis_python" in r1["common_tools"]
    # jarvis_bench_run (alpha) and jarvis_bluecad_read (beta) are divergent
    assert "jarvis_bench_run" in r1["divergent_tools"]
    assert "jarvis_bluecad_read" in r1["divergent_tools"]
    assert 0.0 < cross_model["mean_agreement_rate"] <= 1.0

    # 4. Loops & retries
    loops = report["loops_and_retries"]
    repeats = loops["consecutive_repeats"]
    assert any(r["tool"] == "jarvis_dev_read" and r["repeats"] == 2 for r in repeats)

    failed_retries = loops["failed_retries"]
    assert any(fr["tool"] == "jarvis_python" and fr["retried_with"] == "jarvis_bluecad_read" for fr in failed_retries)

    # 5. Parallel bursts
    bursts = report["parallel_bursts"]
    assert any(b["tools"] == ["jarvis_paper_search", "jarvis_python"] and b["overlap_ms"] == 200.0 for b in bursts)

    # 6. Wish list & reuse check
    wishes = report["wish_list"]
    # "missing paper search tool with fulltext search" matches jarvis.paper_search
    paper_wish = next((w for w in wishes if "paper search" in w["wish"]), None)
    assert paper_wish is not None
    assert "jarvis.paper_search" in paper_wish["reuse_check"]

    # Bench run wish matches jarvis.bench_run
    bench_wish = next((w for w in wishes if "bench" in w["wish"]), None)
    assert bench_wish is not None
    assert "jarvis.bench_run" in bench_wish["reuse_check"]

    # 7. Formatted markdown output
    md = format_mining_report(report)
    assert "# Agent Eval Exploration Mining Report" in md
    assert "jarvis.paper_search" in md
    assert "Mean Agreement Rate" in md


def test_mine_cli(tmp_path: Path) -> None:
    log = {
        "model": "model-1",
        "requests": [
            {
                "request_id": "r1",
                "tool_trace": [
                    {"tool_name": "tool_a", "status": "succeeded"},
                    {"tool_name": "tool_b", "status": "succeeded"},
                ],
                "work_report": {"workarounds": [], "wish_list": []},
            }
        ],
    }
    log_path = tmp_path / "exp_log.json"
    log_path.write_text(json.dumps(log), encoding="utf-8")
    out_dir = tmp_path / "mining_out"

    code = runner_main(["mine", "--logs", str(log_path), "--out", str(out_dir), "--min-support", "1"])
    assert code == 0
    assert (out_dir / "mining_report.json").is_file()
    assert (out_dir / "mining_report.md").is_file()
