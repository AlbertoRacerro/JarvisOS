"""Hermes ``jarvis_python``: short Python scripts in the offline calculation sandbox (spec 371).

The only execution path is ``studio_runtime.runtime.run`` with the ``calculation`` profile, with
empty inputs, the profile's maximum limits and no artifact directory.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from app.core.database import open_sqlite_connection
from app.modules.ai.agent_contracts import StructuredToolCall
from app.modules.events.service import log_event

MAX_CODE_CHARS = 16_384
MAX_TEXT_CHARS = 4_000
MAX_STDERR_CHARS = 1_500
CALC_RUN_EVENT = "hermes.calc_run"
_MAIN_SUFFIX = "\n\ndef main(inputs):\n    return None\n"
_NOTHING_PRINTED = "(nothing printed: use print())"
_FILE_URL = re.compile(r"(?i)file://[^\s,;\"'<>(){}\[\]]+")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9:/])/[A-Za-z0-9._~+-]+(?:/[A-Za-z0-9._~+-]+)*")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s,;\"'<>(){}\[\]]+")
_UNIT_COMPONENTS = frozenset({"A", "J", "K", "L", "N", "Pa", "V", "W", "bar", "cm", "g", "h", "kg", "km",
                              "kPa", "m", "mg", "min", "mL", "mm", "mol", "s"})


def _redact_absolute_path(match: re.Match[str]) -> str:
    path = match.group()
    if all(component in _UNIT_COMPONENTS for component in path.strip("/").split("/")):
        return path
    return "<path>"


def _scrub(text: str) -> str:
    """Remove host paths from sandbox output before the model sees it."""
    from app.modules.studio_runtime import runtime

    text = _FILE_URL.sub("<path>", text)
    prefixes = {str(Path(sys.prefix).resolve()), sys.prefix, str(Path(runtime.__file__).resolve().parent)}
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix and prefix != "/":
            text = text.replace(prefix, "<venv>")
    text = _ABSOLUTE_PATH.sub(_redact_absolute_path, text)
    return _WINDOWS_PATH.sub("<path>", text)


def _limit_text(limit: str | None, value: int | None) -> str:
    if limit is None:
        return "error: resource limit reached"
    if limit.endswith("_seconds"):
        detail = f"{limit[:-len('_seconds')]} {value} s" if value is not None else limit
    else:
        detail = f"{limit} {value}" if value is not None else limit
    return f"error: {limit} limit reached ({detail})"


def _result_text(record: Any) -> str:
    stdout = _scrub(record.stdout or "")
    suffix = ""
    if record.classification == "error":
        suffix = "[stderr]\n" + _scrub(record.stderr or "")[-MAX_STDERR_CHARS:]
    elif record.classification in {"timeout", "limit"}:
        suffix = _limit_text(record.limit, record.limit_value)
    elif record.classification != "ok":
        suffix = f"error: {record.classification}"
    if not stdout and not suffix:
        return _NOTHING_PRINTED
    if not suffix:
        return stdout[:MAX_TEXT_CHARS]
    separator = "\n" if stdout else ""
    return stdout[:MAX_TEXT_CHARS - len(separator) - len(suffix)] + separator + suffix


def _log_run(call: StructuredToolCall, workspace_id: str, record: Any) -> None:
    """Metadata only: classification, limit, duration and the source digest, never the source."""
    try:
        with open_sqlite_connection() as connection:
            log_event(connection, event_type=CALC_RUN_EVENT, actor="jarvis", target_type="agent_tool_call",
                      target_id=call.call_id, workspace_id=workspace_id,
                      payload={"classification": record.classification, "limit": record.limit,
                               "duration_seconds": round(float(record.duration_seconds), 3),
                               "source_sha256": record.source_sha256})
            connection.commit()
    except Exception:
        return


def dispatch_calc_run_tool(call: StructuredToolCall, workspace_id: str,
                           error: str) -> tuple[dict[str, Any] | None, str]:
    """Run the script; refusals return (None, code) and run nothing. Never raises."""
    from app.modules.studio_runtime import runtime

    code = call.arguments.get("code")
    if set(call.arguments) != {"code"} or not isinstance(code, str) or len(code) > MAX_CODE_CHARS:
        return None, "invalid_arguments"
    source = code + _MAIN_SUFFIX
    try:
        if len(source.encode("utf-8")) > runtime.MAX_SOURCE_BYTES:
            return None, "invalid_arguments"
    except UnicodeEncodeError:
        return None, "invalid_arguments"
    try:
        table = runtime.PROFILES["calculation"]
        limits = runtime.Limits(**{name: table[name] for name in runtime.AppliedLimits.model_fields})
        record = runtime.run(runtime.RunRequest(source=source, inputs={}, limits=limits), profile="calculation")
    except Exception:
        return None, "calc_unavailable"
    _log_run(call, workspace_id, record)
    if record.classification in {"disabled", "unavailable"}:
        return None, "calc_unavailable"
    return {"text": _result_text(record)}, error
