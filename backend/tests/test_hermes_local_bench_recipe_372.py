from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.modules.agents.hermes.message_budget import (
    LOCAL_STEP_TOKEN_BUDGET,
    TRIMMED_MARKER,
    compact_local_messages,
)
from app.modules.agents.hermes.supervisor import (
    CONTINUATION_REQUEST,
    RELAY_TOOL_PROTOCOL,
    UPSTREAM_REVISION,
    infer_envelope,
    run_governed_inference,
    tool_response_schema,
)
from app.modules.agents.hermes.worker_shim import (
    COMPACT_METHODOLOGY,
    DEFAULT_LOCAL_OUTPUT_CAP,
    RESEARCH_BUDGET_NOTICE,
    STEP_CAP_MESSAGE,
    Worker,
    repeated_call_notice,
)
from app.modules.ai.agent_contracts import AgentSessionRef

HERMES = Path(__file__).resolve().parents[1] / "app" / "modules" / "agents" / "hermes"
SESSION = AgentSessionRef(jarvis_thread_id="thread-1", hermes_session_id="hermes-1", profile_id="profile-1",
                          workspace_id="workspace-1", generation=1, upstream_revision=UPSTREAM_REVISION)
TOOL = {"type": "function", "function": {"name": "mcp__jarvis__jarvis_python", "parameters": {"type": "object"}}}


def _frame() -> dict[str, Any]:
    return {"type": "relay_request", "id": "request-1", "session_ref": SESSION.model_dump(mode="json"),
            "messages": [{"role": "user", "content": "hello"}], "tools": [TOOL],
            "allowed_tools": [TOOL["function"]["name"]]}


def _run(route: str, replies: list[str], max_continuations: int = 2) -> tuple[dict[str, Any], list[Any], list[str]]:
    envelope = infer_envelope(_frame(), route_class=route)
    calls: list[Any] = []
    guards: list[str] = []

    def runner(**kwargs: Any) -> Any:
        calls.append(kwargs)
        text = replies[min(len(calls) - 1, len(replies) - 1)]
        return SimpleNamespace(status="success", response=SimpleNamespace(text=text, finish_reason="stop"))

    result = run_governed_inference(envelope, runner=runner, structured_output_schema=tool_response_schema([TOOL]),
                                    on_guard=lambda kind, _parsed: guards.append(kind),
                                    max_continuations=max_continuations)
    return result, calls, guards


def test_local_methodology_has_format_and_compute_rule() -> None:
    assert "never compute numbers in your head" in COMPACT_METHODOLOGY.lower()
    for header in ("Answer:", "Numbers:", "Sources:", "Caveats:"):
        assert header in COMPACT_METHODOLOGY
    assert "Use jarvis_turn_plan for multi-step work" not in COMPACT_METHODOLOGY


def test_unfinished_local_steps_get_the_continuation_request_twice_then_are_accepted() -> None:
    texts = ['{"answer":"Let me pull the gathered evidence before answering"}',
             '{"answer":"Need to recompute carefully: $P = \\\\rho g h$"}',
             '{"answer":"Still no format here"}']
    result, calls, guards = _run("local:llamacpp", texts)
    assert guards == ["continue", "continue"] and len(calls) == 3
    assert CONTINUATION_REQUEST in calls[1]["user_prompt"] and CONTINUATION_REQUEST in calls[2]["user_prompt"]
    assert result["status"] == "success" and "Still no format" in result["text"]


def test_answer_format_is_final_at_once_and_cloud_keeps_its_guard() -> None:
    result, calls, guards = _run("local:llamacpp", ['{"answer":"Answer: 4 kg.\\nNumbers: 4 kg"}'])
    assert len(calls) == 1 and guards == [] and result["status"] == "success"
    _, calls, guards = _run("openai:external", ['{"answer":"Let me pull the gathered evidence"}'])
    assert "continue" not in guards and all(CONTINUATION_REQUEST not in c["user_prompt"] for c in calls)


def test_tool_call_step_is_not_asked_to_continue() -> None:
    reply = '{"tool_calls":[{"name":"mcp__jarvis__jarvis_python","arguments":{"code":"print(1)"}}]}'
    _, calls, guards = _run("local:llamacpp", [reply])
    assert len(calls) == 1 and guards == []


def test_local_output_cap_defaults_to_8192() -> None:
    assert DEFAULT_LOCAL_OUTPUT_CAP == 8192
    source = (HERMES / "supervisor.py").read_text(encoding="utf-8")
    assert "DEFAULT_LOCAL_OUTPUT_CAP if is_local_route(selected_route) else 2048" in source


def _shim() -> Worker:
    shim = Worker.__new__(Worker)
    shim.tool_results, shim.tool_results_lock = {}, threading.Lock()
    shim.turn_research_count, shim.research_budget = 0, 8
    return shim


def test_repeated_call_is_answered_from_the_stored_result_and_read_plan_is_exempt() -> None:
    shim = _shim()
    action, claim = shim.claim_tool_call("jarvis_python:{}", "mcp__jarvis__jarvis_python", False)
    assert action == "run" and claim is not None
    claim["text"] = "r" * 1000
    claim["done"].set()
    action, stored = shim.claim_tool_call("jarvis_python:{}", "mcp__jarvis__jarvis_python", False)
    assert action == "repeat" and stored is claim
    notice = repeated_call_notice(stored["text"])
    assert notice == ("You already made this exact call. Its result was: " + "r" * 600
                      + " Do not repeat it: change the input or answer.")
    for _ in range(2):
        assert shim.claim_tool_call("turn_plan:read", "jarvis_turn_plan", True) == ("run", None)


def test_research_budget_and_step_cap_texts() -> None:
    shim = _shim()
    shim.research_budget = 1
    assert shim.claim_tool_call("a", "mcp__jarvis__web_search", False)[0] == "run"
    assert shim.claim_tool_call("b", "mcp__jarvis__web_search", False) == ("budget", None)
    assert RESEARCH_BUDGET_NOTICE.format(budget=8) == (
        "Research budget used up (8 searches/reads). Do not search again: answer now with what you have, "
        "and say clearly what is unconfirmed.")
    assert STEP_CAP_MESSAGE == "Stop using tools now and give your final answer in the format."


def test_only_the_shim_emits_tool_call_frames() -> None:
    """Spec 372 item 4: every relay tool call passes through the shim, so the supervisor needs no second guard."""
    pattern = re.compile(r"""["']type["']\s*:\s*["']tool_call["']""")
    emitters = sorted(path.name for path in HERMES.glob("*.py") if pattern.search(path.read_text(encoding="utf-8")))
    assert emitters == ["worker_shim.py"]
    supervisor = (HERMES / "supervisor.py").read_text(encoding="utf-8")
    assert "turn_tool_hashes" not in supervisor and "identical call was already executed" not in supervisor


def _transcript(results: int, size: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}]
    for i in range(results):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call-{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"call-{i}", "content": str(i) + "r" * (size - 1)})
    messages.append({"role": "assistant", "content": "next"})
    return messages


def test_older_tool_results_are_trimmed_and_the_last_four_kept_whole() -> None:
    messages = _transcript(8, 2000)
    sent, payload = compact_local_messages(messages, [], RELAY_TOOL_PROTOCOL, overhead=0, budget=100_000)
    assert payload is not None and payload["trimmed_tool_results"] == 4 and payload["compacted_steps"] == 0
    tools = [m for m in sent if m["role"] == "tool"]
    originals = [m["content"] for m in messages if m["role"] == "tool"]
    for m, original in zip(tools[:4], originals[:4], strict=True):
        assert m["content"] == original[:700] + TRIMMED_MARKER
    assert [m["content"] for m in tools[4:]] == originals[4:]
    assert sent[0] == messages[0] and sent[1] == messages[1]
    assert [m.get("content") for m in sent if m["role"] == "assistant"] == [
        m.get("content") for m in messages if m["role"] == "assistant"]


def test_over_budget_transcript_falls_back_within_budget() -> None:
    messages = _transcript(8, 6000)
    sent, payload = compact_local_messages(messages, [], RELAY_TOOL_PROTOCOL, overhead=0, budget=3000)
    assert payload is not None and payload["estimated_tokens"] <= 3000
    assert {m["tool_call_id"] for m in sent if m["role"] == "tool"} <= {
        c["id"] for m in sent if m["role"] == "assistant" for c in m.get("tool_calls", [])}
    assert LOCAL_STEP_TOKEN_BUDGET == 6000
