"""Offline acceptance tests for local Hermes prompt, deadline and relay guards."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.agents.hermes.broker_mcp import _TOOLS
from app.modules.agents.hermes.roles import get_role_instructions
from app.modules.agents.hermes.supervisor import (
    RELAY_TOOL_PROTOCOL,
    HermesSupervisor,
    compact_tool_schemas,
    infer_envelope,
    run_governed_inference,
    step_deadline_from_rows,
    tool_response_schema,
    turn_deadline,
    turn_guard_kind,
)
from app.modules.agents.hermes.supervisor import (
    is_dead_turn as supervisor_is_dead_turn,
)
from app.modules.agents.hermes.worker_shim import COMPACT_METHODOLOGY
from app.modules.agents.hermes.worker_shim import is_dead_turn as worker_is_dead_turn
from app.modules.ai.thread_service import _turn_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmarks.agent_eval.runner import build_parser, main, read_job_metrics  # noqa: E402


@pytest.mark.parametrize("surface", ["none", "process", "bluecad", "studio"])
def test_first_step_uses_real_broker_schemas_size_guard(surface: str) -> None:
    names = _turn_tools(surface, "What is the result?")
    assert len(names) <= 7
    tools = [
        {"type": "function", "function": {"name": "mcp__jarvis__" + tool["name"],
                                      "description": tool["description"], "parameters": tool["inputSchema"]}}
        for tool in _TOOLS if "mcp__jarvis__" + tool["name"] in names
    ]
    assert len(tools) == len(names)
    compact = compact_tool_schemas(tools)
    prompt = json.dumps({"messages": [{"role": "system", "content": get_role_instructions("isaac") +
                                       "\n\n" + COMPACT_METHODOLOGY},
                                      {"role": "user", "content": "What is the result?\n\n" + "x" * 2500}],
                         "tools": compact}, separators=(",", ":")) + RELAY_TOOL_PROTOCOL
    # This is a size regression guard, not a model token count.
    assert len(prompt) < 14400
    session = {"jarvis_thread_id": "thread-1", "hermes_session_id": "session-1", "profile_id": "isaac",
               "workspace_id": "default", "generation": 1, "upstream_revision": "test"}
    frame = {"id": "relay-1", "session_ref": session, "messages": [{"role": "user", "content": "Hi"}],
             "tools": compact, "allowed_tools": names}
    envelope = infer_envelope(frame, route_class="local:llamacpp")
    payload = json.loads(envelope.prompt.removesuffix(RELAY_TOOL_PROTOCOL))
    for prompted, tool in zip(payload["tools"], compact, strict=True):
        function = tool["function"]
        schema = function["parameters"]
        assert prompted == {
            "name": function["name"], "description": function["description"],
            "parameters": {name: prop.get("type", "object") for name, prop in schema["properties"].items()},
            "required": schema.get("required", []),
        }
    assert "maxLength" not in json.dumps(payload["tools"])
    assert "additionalProperties" not in json.dumps(payload["tools"])
    first_step = frame | {"messages": [
        {"role": "system", "content": get_role_instructions("isaac") + "\n\n" + COMPACT_METHODOLOGY},
        {"role": "user", "content": "What is the result?\n\n" + "x" * 2500},
    ]}
    # Character guard for the real local prompt shape, not an actual model token count.
    assert len(infer_envelope(first_step, route_class="local:llamacpp").prompt) < 6500
    cloud_envelope = infer_envelope(frame, route_class="external:cheap")
    cloud_payload = json.loads(cloud_envelope.prompt.removesuffix(RELAY_TOOL_PROTOCOL))
    assert cloud_payload["tools"] == compact
    assert len(envelope.prompt) < len(json.dumps({"messages": frame["messages"], "tools": compact},
                                                   separators=(",", ":")) + RELAY_TOOL_PROTOCOL)
    branches = tool_response_schema(compact)["oneOf"][1]["properties"]["tool_calls"]["items"]["anyOf"]
    assert len(branches) == len(names)
    for branch, tool in zip(branches, compact, strict=True):
        assert branch["properties"]["name"] == {"const": tool["function"]["name"]}
        assert branch["properties"]["arguments"] == tool["function"]["parameters"]
    assert all("description" not in prop for tool in compact
               for prop in tool["function"]["parameters"].get("properties", {}).values())


def test_keyword_router() -> None:
    assert "mcp__jarvis__jarvis_paper_search" in _turn_tools("none", "Find research papers")
    assert "mcp__jarvis__jarvis_patent_read" in _turn_tools("none", "Read this patent")
    assert "mcp__jarvis__jarvis_repo_read" in _turn_tools("none", "Inspect repository code")
    assert "mcp__jarvis__jarvis_paper_search" in _turn_tools("none", "Hello")


def test_speed_deadline() -> None:
    rows = [{"input_tokens": 100, "output_tokens": n, "latency_ms": (20 + n / 10) * 1000}
            for n in (100, 200, 300, 400)]
    step = step_deadline_from_rows(rows, 2048)
    assert step > 225
    assert turn_deadline(step, 10) >= step * 10
    assert step_deadline_from_rows([], 2048) == 240


@pytest.mark.parametrize("guard", [turn_guard_kind])
def test_announcement_guard_is_pinned_in_both_processes(guard: object) -> None:
    assert guard("Let me verify the numbers before reporting.", tools_offered=True) == "announcement"
    assert guard("I'll make the actual calls now.", tools_offered=True) == "announcement"
    assert guard("The flow rate is 2.4 L/min.", tools_offered=True) is None
    assert guard("Let me verify the numbers.", tools_offered=True, capped=True) is None


@pytest.mark.parametrize("text", ["", "   ", '{"answer":""}', '{"answer":"Done."}',
                                  '{"tool_calls":[{"name":"jarvis_turn_plan","arguments":{}}]}'])
def test_dead_turn_copies_agree(text: str) -> None:
    assert supervisor_is_dead_turn(text) == worker_is_dead_turn(text)


def _envelope() -> tuple[object, dict[str, object]]:
    session = {"jarvis_thread_id": "thread-1", "hermes_session_id": "session-1", "profile_id": "isaac",
               "workspace_id": "default", "generation": 1, "upstream_revision": "test"}
    tool = {"type": "function", "function": {"name": "mcp__jarvis__jarvis_turn_plan", "parameters": {}}}
    frame = {"id": "relay-1", "session_ref": session, "task_kind": "general",
             "messages": [{"role": "user", "content": "Question"}], "tools": [tool],
             "allowed_tools": [tool["function"]["name"]], "max_output_tokens": 2048}
    return infer_envelope(frame, deadline_seconds=900), tool_response_schema([tool])


def test_second_announcement_forces_final() -> None:
    envelope, schema = _envelope()
    answers = iter(['{"answer":"Let me verify the numbers."}',
                    '{"answer":"I will check the result."}', '{"answer":"Final result."}'])
    calls = []
    events = []

    def runner(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(status="success", response=SimpleNamespace(text=next(answers), finish_reason="stop"))

    result = run_governed_inference(envelope, runner=runner, structured_output_schema=schema,
                                    on_guard=lambda kind, parsed: events.append((kind, parsed)))
    assert result["admitted_tools"] == []
    assert calls[2]["structured_output_schema"] is None
    assert [kind for kind, _ in events] == ["nudge", "forced_final"]


@pytest.mark.parametrize("text,expected_calls", [("garbled", 2), ('{"answer":"usable"}', 1)])
def test_length_guard_retries_only_unparseable(text: str, expected_calls: int) -> None:
    envelope, schema = _envelope()
    calls = []
    events = []

    def runner(**kwargs: object) -> object:
        calls.append(kwargs)
        answer = text if len(calls) == 1 else '{"answer":"recovered"}'
        return SimpleNamespace(status="success", response=SimpleNamespace(
            text=answer, finish_reason="length" if len(calls) == 1 else "stop"))

    run_governed_inference(envelope, runner=runner, structured_output_schema=schema,
                           on_guard=lambda kind, parsed: events.append((kind, parsed)))
    assert len(calls) == expected_calls
    assert events[0] == ("length", expected_calls == 1)
    if expected_calls == 2:
        assert calls[1]["max_output_tokens"] == 4096


def test_length_retry_stops_after_second_unparseable_body() -> None:
    envelope, schema = _envelope()
    caps = []

    def runner(**kwargs: object) -> object:
        caps.append(kwargs["max_output_tokens"])
        return SimpleNamespace(status="success", response=SimpleNamespace(text="garbled", finish_reason="length"))

    run_governed_inference(envelope, runner=runner, structured_output_schema=schema)
    assert caps == [2048, 4096]


def test_timeout_interrupts_kills_and_recovers_before_next_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.agent_contracts import AgentSessionRef

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE ai_jobs (input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, "
               "status TEXT, model_id TEXT, created_at TEXT, route_reason_json TEXT)")
    monkeypatch.setattr("app.modules.agents.hermes.supervisor.open_sqlite_connection", lambda: db)
    monkeypatch.setattr("app.modules.local_ai.runtime.model_registry.get_active_model_entry", lambda: None)
    supervisor = HermesSupervisor(sys.executable, network_isolation=False)
    supervisor.session = AgentSessionRef(jarvis_thread_id="thread-1", hermes_session_id="session-1",
                                         profile_id="isaac", workspace_id="default", generation=1,
                                         upstream_revision="test")
    stalled = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.PIPE, text=True)
    supervisor.process = stalled
    calls = []

    def await_frame(_request_id: str, *, timeout: float = 180) -> dict[str, object]:
        calls.append(timeout)
        if len(calls) <= 2:
            raise RuntimeError("Hermes worker timed out")
        supervisor.last_flow["session-1"] = "flow-next"
        return {"status": "success", "completed": True, "final_response": "Recovered."}

    monkeypatch.setattr(supervisor, "_await", await_frame)
    recovered = []

    def recover(_old: AgentSessionRef) -> None:
        recovered.append(True)
        supervisor.worker_lost = False
        supervisor.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                              stdin=subprocess.PIPE, text=True)

    monkeypatch.setattr(supervisor, "_recover", recover)
    try:
        result = supervisor.turn("First", turn_timeout=0.01)
        assert result["error"] == "turn_deadline"
        assert calls[:2] == [0.01, 30]
        assert stalled.poll() is not None or supervisor.worker_lost
        next_result = supervisor.turn("Second", turn_timeout=0.01)
        assert recovered and next_result["final_response"] == "Recovered."
    finally:
        for process in (stalled, supervisor.process):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        db.close()


def test_eval_cli_timeout_default_and_help(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    args = build_parser().parse_args(["run", "--tasks", "missing.json", "--label", "sample", "--out", "out"])
    assert args.timeout == 3600
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--help"])
    assert "Hermes venv" in capsys.readouterr().out
    tasks = tmp_path / "tasks.json"
    tasks.write_text('{"tasks": []}', encoding="utf-8")
    assert main(["run", "--tasks", str(tasks), "--label", "sample", "--out", str(tmp_path / "out"),
                 "--base-url", "http://127.0.0.1:8001", "--timeout", "300"]) == 1
    assert "at least 660" in capsys.readouterr().err


def test_eval_metrics_read_proof_jobs_only(tmp_path: Path) -> None:
    db_file = tmp_path / "jarvisos.db"
    with sqlite3.connect(db_file) as db:
        db.execute("CREATE TABLE ai_jobs (created_at TEXT, status TEXT, selected_route_class TEXT, "
                   "normalized_usage_source TEXT, input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER)")
        for n in (100, 200, 300):
            db.execute("INSERT INTO ai_jobs VALUES ('2026-01-01T00:00:00Z', 'success', 'local:fake', ?, ?, ?, ?)",
                       ("actual", n * 10, n, (20 + n / 10) * 1000))
    import datetime

    start = datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC).timestamp()
    end = datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC).timestamp()
    metrics = read_job_metrics(tmp_path, start, end)
    assert metrics == {"first_actual_input_tokens": 1000, "max_actual_input_tokens": 3000, "input_tokens": 6000,
                       "output_tokens": 600, "decode_tps": 10.0, "steps_over_6000_input_tokens": 0}
    with sqlite3.connect(db_file) as db:
        db.execute("UPDATE ai_jobs SET normalized_usage_source = 'estimated' WHERE output_tokens = 100")
    assert read_job_metrics(tmp_path, start, end)["first_actual_input_tokens"] is None
    assert read_job_metrics(tmp_path, start, end)["max_actual_input_tokens"] is None


def test_local_system_message_is_replaced_and_keeps_saved_plan() -> None:
    from app.modules.agents.hermes.supervisor import compact_system_message

    upstream = "long upstream Hermes system prompt " * 400 + "\n\n[Active turn plan]\n1. step"
    messages = [{"role": "system", "content": upstream}, {"role": "user", "content": "q"}]
    replaced = compact_system_message(messages, "compact rules")
    assert replaced[0]["content"].startswith("compact rules")
    assert replaced[0]["content"].endswith("[Active turn plan]\n1. step")
    assert "long upstream" not in replaced[0]["content"]
    assert replaced[1:] == messages[1:]
    assert compact_system_message([{"role": "user", "content": "q"}], "x") == [{"role": "user", "content": "q"}]
