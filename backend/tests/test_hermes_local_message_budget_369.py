"""Offline acceptance tests for the local Hermes per-step message budget (spec 369)."""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.modules.agents.hermes.supervisor as supervisor_module
from app.modules.agents.hermes.message_budget import (
    ARGUMENTS_MARKER,
    LOCAL_STEP_TOKEN_BUDGET,
    PLAN_RESULT_MARKER,
    compact_local_messages,
    digest_older_steps,
    estimate_tokens,
)
from app.modules.agents.hermes.supervisor import (
    RELAY_TOOL_PROTOCOL,
    UPSTREAM_REVISION,
    HermesSupervisor,
    _local_tool_prompt,
    compact_system_message,
    compact_tool_schemas,
    infer_envelope,
)
from app.modules.agents.hermes.worker_shim import inject_saved_plan
from app.modules.ai.agent_contracts import AgentSessionRef

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmarks.agent_eval.runner import read_job_metrics  # noqa: E402

SESSION = AgentSessionRef(jarvis_thread_id="thread-1", hermes_session_id="hermes-1", profile_id="profile-1",
                          workspace_id="workspace-1", generation=1, upstream_revision=UPSTREAM_REVISION)
PLAN = {"steps": [{"id": f"s{n}", "status": "pending", "title": "t" * 150, "note": "n" * 190} for n in range(10)],
        "findings": [{"key": f"k{n}", "value": "v" * 120} for n in range(24)]}
TOOL_NAMES = ["mcp__jarvis__jarvis_retrieval_query", "mcp__jarvis__jarvis_turn_plan"]
TOOLS = [{"type": "function", "function": {
    "name": name, "description": "Do the thing.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "op": {"type": "string"}},
                   "required": ["query"]}}} for name in TOOL_NAMES]
PLAN_ARGS = json.dumps({"op": "write_plan", "steps": [{"id": "x", "title": "y" * 700}],
                        "findings": [{"key": "k", "value": "z" * 600}]})
SUMMARIES = [_local_tool_prompt(tool) for tool in compact_tool_schemas(TOOLS)]


def _system() -> dict[str, str]:
    base = [{"role": "system", "content": "upstream"}]
    return inject_saved_plan(compact_system_message(base, "compact rules"), PLAN)[0]


def _transcript(steps: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [_system(), {"role": "user", "content": "What is the result?\n\n" + "q" * 1500}]
    for n in range(steps):
        is_plan = n % 4 == 0
        name = TOOL_NAMES[1] if is_plan else TOOL_NAMES[0]
        arguments = PLAN_ARGS if is_plan else json.dumps({"query": f"look up {n} " + "w" * 220, "limit": 5})
        messages.append({"role": "assistant", "content": "a" * 400, "tool_calls": [
            {"id": f"call-{n}", "type": "function", "function": {"name": name, "arguments": arguments}}]})
        messages.append({"role": "tool", "tool_call_id": f"call-{n}", "content": "r" * 2000})
    return messages


def _tokens(messages: list[dict[str, Any]]) -> int:
    frame = {"id": "r", "session_ref": SESSION.model_dump(mode="json"), "messages": messages,
             "tools": compact_tool_schemas(TOOLS), "allowed_tools": TOOL_NAMES}
    return estimate_tokens(len(infer_envelope(frame, route_class="local:llamacpp").prompt))


def test_older_steps_become_digests_and_latest_step_is_whole() -> None:
    messages = _transcript(3)
    digested = digest_older_steps(messages)
    first_call = digested[2]["tool_calls"][0]
    assert first_call["id"] == "call-0" and first_call["function"]["name"] == TOOL_NAMES[1]
    arguments = first_call["function"]["arguments"]
    assert len(arguments) <= 200 and arguments.startswith("op, steps") and arguments.endswith(ARGUMENTS_MARKER)
    assert len(digested[2]["content"]) <= 300 and digested[2]["content"] != messages[2]["content"]
    second = digested[4]["tool_calls"][0]["function"]["arguments"]
    assert len(second) <= 200 and second.startswith("query, limit")
    assert digested[-2:] == messages[-2:] and digested[:2] == messages[:2]


def test_older_plan_results_and_short_arguments_are_cut_even_when_short() -> None:
    short = json.dumps({"op": "read_plan"})
    messages = [_system(), {"role": "user", "content": "q"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "p", "type": "function", "function": {"name": TOOL_NAMES[1], "arguments": short}},
                    {"id": "r", "type": "function", "function": {"name": TOOL_NAMES[0], "arguments": short}}]},
                {"role": "tool", "tool_call_id": "p", "content": "plan: ok"},
                {"role": "tool", "tool_call_id": "r", "content": "short result"},
                {"role": "assistant", "content": "x", "tool_calls": [
                    {"id": "p2", "type": "function", "function": {"name": TOOL_NAMES[1], "arguments": short}}]},
                {"role": "tool", "tool_call_id": "p2", "content": "plan: ok"}]
    digested = digest_older_steps(messages)
    calls = digested[2]["tool_calls"]
    assert calls[0]["function"]["arguments"].endswith(ARGUMENTS_MARKER)
    assert calls[1]["function"]["arguments"] == short
    assert digested[3]["content"] == PLAN_RESULT_MARKER and digested[4]["content"] == "short result"
    assert digested[-2:] == messages[-2:]


@pytest.mark.parametrize("steps", [*range(1, 13), 25, 40])
def test_twelve_step_transcript_stays_under_budget_and_cloud_is_unchanged(steps: int) -> None:
    messages = _transcript(steps)
    original = json.dumps(messages)
    compacted, payload = compact_local_messages(messages, SUMMARIES, RELAY_TOOL_PROTOCOL)
    assert json.dumps(messages) == original
    assert _tokens(compacted) <= LOCAL_STEP_TOKEN_BUDGET
    assert payload is not None and payload["over_budget"] is False
    assert payload["estimated_tokens"] == _tokens(compacted)
    # Anchors: system with plan, first user message and the whole latest step.
    assert compacted[:2] == messages[:2]
    assert compacted[-2:] == messages[-2:]
    calls = {c["id"] for m in compacted if m["role"] == "assistant" for c in m.get("tool_calls", [])}
    results = {m["tool_call_id"] for m in compacted if m["role"] == "tool"}
    assert results == calls
    # Cloud routes never go through the helper; infer_envelope keeps the transcript byte for byte.
    frame = {"id": "r", "session_ref": SESSION.model_dump(mode="json"), "messages": messages}
    cloud = infer_envelope(frame, route_class="external:cheap")
    assert cloud.prompt == json.dumps(messages_after_shrink(messages), ensure_ascii=False)


def messages_after_shrink(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.modules.agents.hermes.worker_shim import shrink_tool_history

    return shrink_tool_history(messages)


def test_long_transcript_reports_compaction_and_names_the_saved_plan() -> None:
    compacted, payload = compact_local_messages(_transcript(40), SUMMARIES, RELAY_TOOL_PROTOCOL)
    assert payload is not None and payload["compacted_steps"] > 0
    line = compacted[2]
    assert line["role"] == "system" and str(payload["compacted_steps"]) in line["content"]
    assert "saved plan" in line["content"]


def test_under_budget_transcript_changes_only_by_digests() -> None:
    messages = _transcript(2)
    compacted, payload = compact_local_messages(messages, SUMMARIES, RELAY_TOOL_PROTOCOL)
    assert payload is not None and payload["compacted_steps"] == 0 and payload["over_budget"] is False
    assert compacted == digest_older_steps(messages) and len(compacted) == len(messages)


def test_unfittable_transcript_is_sent_as_over_budget() -> None:
    messages = _transcript(3)
    messages[-1]["content"] = "r" * 40_000
    compacted, payload = compact_local_messages(messages, SUMMARIES, RELAY_TOOL_PROTOCOL)
    assert payload is not None and payload["over_budget"] is True and payload["estimated_tokens"] > 6000
    assert compacted[0] == messages[0] and compacted[1] == messages[1] and compacted[-2:] == messages[-2:]
    assert not any(m["role"] == "tool" for m in compacted[2:-2])


def test_non_message_lists_pass_through() -> None:
    assert compact_local_messages("x", [], "") == ("x", None)
    assert compact_local_messages([], [], "") == ([], None)


def test_size_breakdown_counts_categories_without_text() -> None:
    _, payload = compact_local_messages(_transcript(2), SUMMARIES, RELAY_TOOL_PROTOCOL)
    assert payload is not None
    assert set(payload) == {"system", "plan", "user", "assistant", "tool_args", "tool_results", "tools",
                            "protocol", "estimated_tokens", "compacted_steps", "over_budget"}
    assert all(isinstance(v, bool if k == "over_budget" else int) for k, v in payload.items())
    assert payload["protocol"] == len(RELAY_TOOL_PROTOCOL) and payload["plan"] > 1000
    assert payload["system"] > 0 and payload["user"] > 1500


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events (id TEXT PRIMARY KEY, workspace_id TEXT, event_type TEXT, "
                       "actor TEXT, target_type TEXT, target_id TEXT, payload TEXT, created_at TEXT)")
    return connection


class _Keep:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


def _relay(monkeypatch: pytest.MonkeyPatch, messages: list[dict[str, Any]], prompts: list[str]) -> tuple[
        list[dict[str, Any]], sqlite3.Connection]:
    connection = _db()
    monkeypatch.setattr(supervisor_module, "open_sqlite_connection", lambda: _Keep(connection))

    def runner(**kwargs: Any) -> Any:
        prompts.append(kwargs["user_prompt"])
        return SimpleNamespace(status="success", response=SimpleNamespace(
            text=json.dumps({"answer": "done"}), finish_reason="stop"), flow_id=None)

    supervisor = HermesSupervisor("unused", runner=runner)
    supervisor.session = SESSION
    supervisor.active_turn_plan = PLAN
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(supervisor, "_send", sent.append)
    frame = {"type": "relay_request", "id": "request-1", "session_ref": SESSION.model_dump(mode="json"),
             "messages": messages, "tools": TOOLS, "allowed_tools": TOOL_NAMES}
    supervisor._handle_relay(frame)
    return sent, connection


def test_relay_compacts_local_route_and_writes_counts_only_event(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    sent, connection = _relay(monkeypatch, _transcript(40), prompts)
    assert sent[-1]["status"] == "success", sent[-1]
    assert estimate_tokens(len(prompts[0])) <= LOCAL_STEP_TOKEN_BUDGET
    rows = connection.execute("SELECT event_type, payload FROM events WHERE event_type = 'hermes.step_size'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert set(payload) == {"system", "plan", "user", "assistant", "tool_args", "tool_results", "tools", "protocol",
                            "estimated_tokens", "compacted_steps", "over_budget", "step"}
    assert payload["step"] == 1 and payload["compacted_steps"] > 0 and payload["over_budget"] is False
    assert all(isinstance(v, (int, bool)) for v in payload.values())
    assert "What is the result" not in rows[0]["payload"]


def test_relay_over_budget_is_sent_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = _transcript(3)
    messages[-1]["content"] = "r" * 40_000
    prompts: list[str] = []
    sent, connection = _relay(monkeypatch, messages, prompts)
    assert sent[-1]["status"] == "success" and prompts
    payload = json.loads(connection.execute("SELECT payload FROM events").fetchone()["payload"])
    assert payload["over_budget"] is True


def test_cloud_route_is_refused_by_relay_before_any_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(supervisor_module, "compact_local_messages", lambda *a, **k: calls.append(1))
    supervisor = HermesSupervisor("unused", route_for_task=lambda _task: "external:cheap")
    supervisor.session = SESSION
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(supervisor, "_send", sent.append)
    supervisor._handle_relay({"type": "relay_request", "id": "r", "session_ref": SESSION.model_dump(mode="json"),
                              "messages": _transcript(3)})
    assert sent[-1]["status"] == "refused" and not calls


def test_agent_eval_reports_steps_over_6000_actual_input_tokens(tmp_path: Path) -> None:
    db_file = tmp_path / "jarvisos.db"
    with sqlite3.connect(db_file) as db:
        db.execute("CREATE TABLE ai_jobs (created_at TEXT, status TEXT, selected_route_class TEXT, "
                   "normalized_usage_source TEXT, input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER)")
        for tokens in (3900, 6000, 6001, 9000):
            db.execute("INSERT INTO ai_jobs VALUES ('2026-01-01T00:00:00Z', 'success', 'local:fake', 'actual', ?, 10, 1000)",
                       (tokens,))
    start = datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC).timestamp()
    end = datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC).timestamp()
    assert read_job_metrics(tmp_path, start, end)["steps_over_6000_input_tokens"] == 2
    with sqlite3.connect(db_file) as db:
        db.execute("UPDATE ai_jobs SET normalized_usage_source = 'estimated' WHERE input_tokens = 9000")
    assert read_job_metrics(tmp_path, start, end)["steps_over_6000_input_tokens"] is None
    assert read_job_metrics(tmp_path / "missing", start, end)["steps_over_6000_input_tokens"] is None
