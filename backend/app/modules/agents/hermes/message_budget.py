"""Deterministic message budget for local Hermes relay steps (specs 369, 370).

Pure functions over the OpenAI-shaped message list: older assistant steps become short digests, the saved plan
is not repeated, earlier tool results stay whole while the step fits and are shortened oldest first when it does
not, and only then are the oldest complete steps dropped.
"""
from __future__ import annotations

import json
import math
from typing import Any

from app.modules.agents.hermes.worker_shim import _PLAN_HEADER, shrink_tool_history

LOCAL_STEP_TOKEN_BUDGET = 6000  # target of actual input tokens: estimated envelope + measured overhead
DEFAULT_STEP_OVERHEAD = 2500  # tokens the runtime adds outside the envelope, until one is measured
MAX_STEP_OVERHEAD = 4000
STEP_CONTENT_LIMIT = 300
STEP_ARGUMENTS_LIMIT = 200
CONTENT_MARKER = " [older step shortened]"
ARGUMENTS_MARKER = " [arguments shortened]"
PLAN_RESULT_MARKER = "[plan result omitted; the saved plan in the system message carries it]"
_PLAN_TOOL = "jarvis_turn_plan"


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / 3.5)


def measured_overhead(actual_input_tokens: int, estimated_tokens: int) -> int:
    """Tokens the runtime added beyond the estimated envelope (chat template, schema, wrapping), clamped."""
    return min(MAX_STEP_OVERHEAD, max(0, actual_input_tokens - estimated_tokens))


def _call_name(call: Any) -> str:
    function = call.get("function") if isinstance(call, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    return name if isinstance(name, str) else ""


def _arguments_digest(arguments: str) -> str:
    keys: list[str] = []
    try:
        parsed = json.loads(arguments)
    except ValueError:
        parsed = None
    room = STEP_ARGUMENTS_LIMIT - len(ARGUMENTS_MARKER)
    if isinstance(parsed, dict):
        head = ""
        for key in parsed:
            candidate = ", ".join([*keys, str(key)])
            if len(candidate) > room:
                break
            keys.append(str(key))
            head = candidate
    else:
        head = arguments[:room]
    return head + ARGUMENTS_MARKER


def _digest_assistant(message: dict[str, Any], plan_ids: set[str]) -> dict[str, Any]:
    result = dict(message)
    content = result.get("content")
    if isinstance(content, str) and len(content) > STEP_CONTENT_LIMIT:
        result["content"] = content[: STEP_CONTENT_LIMIT - len(CONTENT_MARKER)] + CONTENT_MARKER
    calls = result.get("tool_calls")
    if isinstance(calls, list):
        digested = []
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if isinstance(function, dict) and isinstance(arguments, str) and (
                    len(arguments) > STEP_ARGUMENTS_LIMIT or _PLAN_TOOL in _call_name(call)):
                if _PLAN_TOOL in _call_name(call) and isinstance(call.get("id"), str):
                    plan_ids.add(call["id"])
                call = {**call, "function": {**function, "arguments": _arguments_digest(arguments)}}
            digested.append(call)
        result["tool_calls"] = digested
    return result


def digest_older_steps(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items 1-2: shorten every assistant step older than the latest one and its saved-plan results."""
    latest = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    plan_ids: set[str] = set()
    result = [_digest_assistant(m, plan_ids) if i < latest and m.get("role") == "assistant" else m
              for i, m in enumerate(messages)]
    for i in range(latest):
        message = result[i]
        if message.get("role") == "tool" and message.get("tool_call_id") in plan_ids:
            result[i] = {**message, "content": PLAN_RESULT_MARKER}
    return result


def _prompt_chars(messages: list[dict[str, Any]], tools: list[dict[str, Any]], protocol: str) -> int:
    # Mirrors infer_envelope's local serialisation.
    if not tools:
        return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    return len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                          separators=(",", ":"))) + len(protocol)


def _steps(messages: list[dict[str, Any]], start: int, stop: int) -> list[tuple[int, int]]:
    """Complete steps in [start, stop): an assistant message with the tool messages that follow it."""
    steps = []
    i = start
    while i < stop:
        if messages[i].get("role") == "assistant":
            end = i + 1
            while end < stop and messages[end].get("role") == "tool":
                end += 1
            steps.append((i, end))
            i = end
        else:
            i += 1
    return steps


def _shorten_tool_results(messages: list[dict[str, Any]], count: int, stop: int) -> tuple[list[dict[str, Any]], set[int]]:
    """Item 3: shorten the first `count` tool results before index `stop` that are long enough to change.

    Returns the messages and the ids of the replaced ones. Each result is cut by `shrink_tool_history` with
    `keep_recent=0`, so the form and marker are the existing 180-char one.
    """
    result = list(messages)
    changed: set[int] = set()
    for i, message in enumerate(messages[:stop]):
        if len(changed) >= count:
            break
        if message.get("role") == "tool":
            shortened = shrink_tool_history([message], keep_recent=0)[0]
            if shortened is not message:
                result[i] = shortened
                changed.add(id(shortened))
    return result, changed


def _compaction_line(count: int) -> dict[str, str]:
    return {"role": "system",
            "content": f"[{count} earlier steps were compacted to fit the local budget; see the saved plan above]"}


def _length(value: Any) -> int:
    return len(value) if isinstance(value, str) else 0


def size_breakdown(messages: list[dict[str, Any]], tools: list[dict[str, Any]], protocol: str) -> dict[str, int]:
    """Char counts by category of what infer_envelope will send; counts only, never text."""
    sizes = dict.fromkeys(("system", "plan", "user", "assistant", "tool_args", "tool_results"), 0)
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role in ("system", "developer"):
            text = content if isinstance(content, str) else ""
            head, header, plan = text.partition(_PLAN_HEADER)
            sizes["system"] += len(head)
            sizes["plan"] += len(header) + len(plan)
        elif role == "user":
            sizes["user"] += _length(content)
        elif role == "assistant":
            sizes["assistant"] += _length(content)
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                sizes["tool_args"] += _length(function.get("arguments") if isinstance(function, dict) else None)
        elif role == "tool":
            sizes["tool_results"] += _length(content)
    sizes["tools"] = len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))) if tools else 0
    sizes["protocol"] = len(protocol) if tools else 0
    return sizes


def compact_local_messages(
    messages: Any, tools: list[dict[str, Any]], protocol: str, overhead: int = DEFAULT_STEP_OVERHEAD,
    budget: int = LOCAL_STEP_TOKEN_BUDGET,
) -> tuple[Any, dict[str, Any] | None]:
    """Items 1-4: returns the messages to send and the `hermes.step_size` payload (None when not a message list).

    `tools` are the local tool summaries and `protocol` the suffix the envelope appends when tools are admitted.
    `overhead` is the measured (or default) token count the runtime adds outside the envelope; the budget is a
    target of actual input tokens, so steps are compacted while estimated + overhead exceeds it.
    """
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        return messages, None
    working = digest_older_steps(messages)
    first_user = next((i for i, m in enumerate(working) if m.get("role") == "user"), None)
    latest = max((i for i, m in enumerate(working) if m.get("role") == "assistant"), default=None)
    steps = _steps(working, first_user + 1, latest) if first_user is not None and latest is not None else []

    def without_oldest(count: int) -> list[dict[str, Any]]:
        if not count or first_user is None:
            return working
        dropped = {i for start, end in steps[:count] for i in range(start, end)}
        kept = [m for i, m in enumerate(working) if i not in dropped]
        return [*kept[: first_user + 1], _compaction_line(count), *kept[first_user + 1:]]

    def over(candidate: list[dict[str, Any]]) -> bool:
        return estimate_tokens(_prompt_chars(candidate, tools, protocol)) + overhead > budget

    shortenable = sum(1 for m in working[: latest or 0] if m.get("role") == "tool")
    shrunk_ids: set[int] = set()
    for shortened in range(shortenable + 1):
        candidate, shrunk_ids = _shorten_tool_results(working, shortened, latest or 0)
        if not over(candidate):
            break
    working = candidate
    compacted = 0
    while compacted < len(steps) and over(without_oldest(compacted)):
        compacted += 1
    working = without_oldest(compacted)
    estimated = estimate_tokens(_prompt_chars(working, tools, protocol))
    payload: dict[str, Any] = {
        **size_breakdown(working, tools, protocol),
        "estimated_tokens": estimated, "compacted_steps": compacted, "overhead": overhead,
        "over_budget": estimated + overhead > budget,
        "shrunk_tool_results": sum(1 for m in working if id(m) in shrunk_ids),
    }
    return working, payload

