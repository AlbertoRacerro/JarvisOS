"""Offline acceptance tests for budgeted local Hermes tool history (spec 370)."""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

import app.modules.agents.hermes.supervisor as supervisor_module
from app.modules.agents.hermes.message_budget import (
    DEFAULT_STEP_OVERHEAD,
    LOCAL_STEP_TOKEN_BUDGET,
    compact_local_messages,
    digest_older_steps,
    estimate_tokens,
)
from app.modules.agents.hermes.supervisor import (
    RELAY_TOOL_PROTOCOL,
    UPSTREAM_REVISION,
    HermesSupervisor,
    _local_tool_prompt,
    compact_tool_schemas,
    infer_envelope,
)
from app.modules.agents.hermes.worker_shim import (
    _PLAN_HEADER,
    Worker,
    shrink_tool_history,
)
from app.modules.ai.agent_contracts import AgentSessionRef

SESSION = AgentSessionRef(jarvis_thread_id="thread-1", hermes_session_id="hermes-1", profile_id="profile-1",
                          workspace_id="workspace-1", generation=1, upstream_revision=UPSTREAM_REVISION)
TOOL_NAME = "mcp__jarvis__jarvis_retrieval_query"
TOOLS = [{"type": "function", "function": {
    "name": TOOL_NAME, "description": "Do the thing.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]
SUMMARIES = [_local_tool_prompt(tool) for tool in compact_tool_schemas(TOOLS)]
MARKER = "[prior tool output shrunk]"


def _transcript(steps: int, result_size: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "upstream"},
                                      {"role": "user", "content": "What is the result?"}]
    for n in range(steps):
        messages.append({"role": "assistant", "content": "a" * 100, "tool_calls": [
            {"id": f"call-{n}", "type": "function",
             "function": {"name": TOOL_NAME, "arguments": json.dumps({"query": f"q{n}"})}}]})
        messages.append({"role": "tool", "tool_call_id": f"call-{n}", "content": f"{n}" + "r" * (result_size - 1)})
    return messages


def _tool_contents(messages: list[dict[str, Any]]) -> dict[str, str]:
    return {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}


def _trimmed_contents(messages: list[dict[str, Any]]) -> dict[str, str]:
    contents = _tool_contents(messages)
    older = list(contents)[:-4]
    return {k: (v[:700] + " …[trimmed]" if k in older and len(v) > 700 else v) for k, v in contents.items()}


def _pairs_match(messages: list[dict[str, Any]]) -> bool:
    calls = {c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])}
    return calls == {m["tool_call_id"] for m in messages if m["role"] == "tool"}


def test_fitting_local_transcript_keeps_earlier_tool_results_whole() -> None:
    messages = _transcript(6, 1500)
    compacted, payload = compact_local_messages(messages, SUMMARIES, RELAY_TOOL_PROTOCOL)
    assert payload is not None and payload["shrunk_tool_results"] == 0 and payload["compacted_steps"] == 0
    # Spec 372 item 5: the last 4 results stay whole, older ones are cut to 700 chars plus the marker.
    assert _tool_contents(compacted) == _trimmed_contents(messages)


def test_tool_results_are_shortened_oldest_first_before_any_step_is_dropped() -> None:
    messages = _transcript(12, 2000)
    seen_shrunk: list[int] = []
    for steps in range(1, 13):
        compacted, payload = compact_local_messages(_transcript(steps, 2000), SUMMARIES, RELAY_TOOL_PROTOCOL)
        assert payload is not None and payload["over_budget"] is False
        assert payload["estimated_tokens"] + DEFAULT_STEP_OVERHEAD <= LOCAL_STEP_TOKEN_BUDGET
        assert _pairs_match(compacted) and compacted[-2:] == _transcript(steps, 2000)[-2:]
        contents = list(_tool_contents(compacted).values())[:-1]
        flags = [MARKER in c for c in contents]
        # Shortened results form a prefix (oldest first); the newest earlier results are the last to be shortened.
        assert flags == sorted(flags, reverse=True)
        assert payload["shrunk_tool_results"] == sum(flags)
        if payload["compacted_steps"]:
            assert all(flags)
        seen_shrunk.append(payload["shrunk_tool_results"])
    assert seen_shrunk[0] == 0 and max(seen_shrunk) > 0
    assert messages[-1]["content"] == compact_local_messages(messages, SUMMARIES, RELAY_TOOL_PROTOCOL)[0][-1]["content"]


def test_shortened_result_uses_the_existing_180_char_form_and_latest_step_is_untouched() -> None:
    messages = _transcript(12, 2000)
    compacted, payload = compact_local_messages(messages, SUMMARIES, RELAY_TOOL_PROTOCOL)
    assert payload is not None and payload["shrunk_tool_results"] > 0
    shortened = [m for m in compacted if m["role"] == "tool" and MARKER in m["content"]]
    original = {m["tool_call_id"]: m["content"] for m in digest_older_steps(messages) if m["role"] == "tool"}
    for m in shortened:
        assert len(m["content"]) <= 180 and m["content"] == shrink_tool_history(
            [{"role": "tool", "content": original[m["tool_call_id"]]}], keep_recent=0)[0]["content"]
    assert compacted[-1] == messages[-1] and compacted[-2] == messages[-2]


def _frame(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "r", "session_ref": SESSION.model_dump(mode="json"), "messages": messages}


def test_infer_envelope_shrinks_only_non_local_routes() -> None:
    messages = _transcript(6, 1500)
    assert infer_envelope(_frame(messages), route_class="external:cheap").prompt == json.dumps(
        shrink_tool_history(messages), ensure_ascii=False)
    local = infer_envelope(_frame(messages), route_class="local:llamacpp").prompt
    assert local == json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


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


def _relay(monkeypatch: pytest.MonkeyPatch, messages: list[dict[str, Any]], route: str) -> tuple[
        list[str], list[dict[str, Any]], sqlite3.Connection]:
    connection = _db()
    monkeypatch.setattr(supervisor_module, "open_sqlite_connection", lambda: _Keep(connection))
    prompts: list[str] = []

    def runner(**kwargs: Any) -> Any:
        prompts.append(kwargs["user_prompt"])
        return SimpleNamespace(status="success", response=SimpleNamespace(
            text=json.dumps({"answer": "done"}), finish_reason="stop"), flow_id=None)

    supervisor = HermesSupervisor("unused", runner=runner, route_for_task=lambda _task: route)
    supervisor.session = SESSION
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(supervisor, "_send", sent.append)
    supervisor._handle_relay({"type": "relay_request", "id": "request-1", "session_ref": SESSION.model_dump(mode="json"),
                              "messages": messages, "tools": TOOLS, "allowed_tools": [TOOL_NAME]})
    return prompts, sent, connection


def test_relay_local_route_forwards_whole_results_and_counts_only_event(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = _transcript(6, 1500)
    prompts, sent, connection = _relay(monkeypatch, messages, "local:llamacpp")
    assert sent[-1]["status"] == "success"
    sent_messages = json.loads(prompts[0].split(RELAY_TOOL_PROTOCOL)[0])["messages"]
    assert _tool_contents(sent_messages) == _trimmed_contents(messages)
    payload = json.loads(connection.execute(
        "SELECT payload FROM events WHERE event_type = 'hermes.step_size'").fetchone()["payload"])
    assert payload["shrunk_tool_results"] == 0 and payload["trimmed_tool_results"] == 2
    assert all(isinstance(v, (int, bool)) for v in payload.values())
    assert estimate_tokens(len(prompts[0])) + DEFAULT_STEP_OVERHEAD <= LOCAL_STEP_TOKEN_BUDGET


def test_relay_local_route_shortens_when_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts, sent, connection = _relay(monkeypatch, _transcript(12, 2000), "local:llamacpp")
    assert sent[-1]["status"] == "success"
    payload = json.loads(connection.execute("SELECT payload FROM events").fetchone()["payload"])
    assert payload["shrunk_tool_results"] > 0 and payload["over_budget"] is False
    assert all(isinstance(v, (int, bool)) for v in payload.values())


def test_relay_cloud_route_is_refused_and_emits_no_step_size(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts, sent, connection = _relay(monkeypatch, _transcript(3, 1500), "external:cheap")
    assert sent[-1]["status"] == "refused" and not prompts
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_shim_forwards_unshrunk_tool_results_and_injects_the_plan() -> None:
    worker = Worker()
    worker.session = SESSION.model_dump(mode="json")
    worker.active_turn_plan = {"steps": [{"id": "s1", "status": "pending", "title": "t"}], "findings": []}
    forwarded: list[dict[str, Any]] = []

    def send(frame: dict[str, Any]) -> None:
        forwarded.append(frame)
        with worker.pending_lock:
            worker.pending[frame["id"]].put({"status": "success", "text": json.dumps({"answer": "done"}),
                                             "finish_reason": "stop"})

    worker.send = send  # type: ignore[method-assign]
    messages = _transcript(40, 1500)
    request = urllib.request.Request(
        f"{worker.base_url}/chat/completions", data=json.dumps({"model": "m", "messages": messages}).encode(),
        headers={"Authorization": f"Bearer {worker.token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
    finally:
        worker.server.shutdown()
        worker.server.server_close()
    sent_messages = forwarded[0]["messages"]
    assert _tool_contents(sent_messages) == _tool_contents(messages)
    assert len(sent_messages) == len(messages)
    assert _PLAN_HEADER in sent_messages[0]["content"] and "s1" in sent_messages[0]["content"]
