#!/usr/bin/env python3
"""Repeatable agent regression eval runner and comparator for JarvisOS.

Stdlib only. Drives the chat -> Hermes path, scores answers against deterministic
checks, and compares candidate runs with baselines to fail on regressions.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import vault_guard

TERMINAL_FLOW_STATES = frozenset(
    {"complete", "completed", "failed", "failed_terminal", "cancelled_terminal", "timed_out"}
)
COMPLETED_FLOW_STATES = frozenset({"complete", "completed"})
ARTIFACT_CHECK_KINDS = frozenset({"tool_called", "studio_draft"})
SPLITS = ("dev", "heldout", "all")

# Type for injectable Claude runner: (prompt, model) -> (answer, flow_state, terminal_reason)
ClaudeRunner = Callable[[str, str], tuple[str, str, str | None]]

_INJECTED_CLAUDE_RUNNER: ClaudeRunner | None = None


def set_claude_runner(runner: ClaudeRunner | None) -> None:
    """Inject a mock/stub Claude runner for testing."""
    global _INJECTED_CLAUDE_RUNNER
    _INJECTED_CLAUDE_RUNNER = runner


def is_loopback_host(hostname: str) -> bool:
    """Return True if hostname resolves to a loopback address."""
    clean = hostname.strip().lower()
    if clean in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        ip = ipaddress.ip_address(clean)
        return ip.is_loopback
    except ValueError:
        return False


def validate_base_url(url_str: str) -> urllib.parse.ParseResult:
    """Validate loopback host and refuse port 8000."""
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}'; expected http or https")
    hostname = (parsed.hostname or "").lower()
    if not hostname or not is_loopback_host(hostname):
        raise ValueError(
            f"Host '{hostname}' is not a loopback interface; only loopback is permitted"
        )
    if parsed.port == 8000:
        raise ValueError(
            "Port 8000 is refused; proof backend must run on an isolated proof port"
        )
    return parsed


def count_thread_tool_calls(data_root: Path | str, thread_id: str) -> int:
    """Count hermes.agent_event tool calls for a thread in a read-only SQLite database."""
    root = Path(data_root)
    db_file = root if root.is_file() else root / "jarvisos.db"
    if not db_file.exists():
        return 0

    uri = f"file:{urllib.parse.quote(db_file.resolve().as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM events WHERE event_type = 'hermes.agent_event'")
        count = 0
        for (payload_raw,) in cursor.fetchall():
            if not payload_raw:
                continue
            try:
                payload = json.loads(payload_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            session_ref = payload.get("session_ref")
            if (isinstance(session_ref, dict) and session_ref.get("jarvis_thread_id") == thread_id
                    and payload.get("kind") == "tool.call"):
                count += 1
        return count
    finally:
        conn.close()


def _open_proof_db(data_root: Path | str) -> sqlite3.Connection | None:
    root = Path(data_root)
    db_file = root if root.is_file() else root / "jarvisos.db"
    if not db_file.exists():
        return None
    return sqlite3.connect(f"file:{urllib.parse.quote(db_file.resolve().as_posix())}?mode=ro", uri=True)


def _bare_tool_name(name: Any) -> str:
    return str(name).removeprefix("mcp__jarvis__")


def count_named_tool_calls(data_root: Path | str, thread_id: str, tool: str) -> int:
    """Count a thread's `tool.call` events whose matching `hermes.tool_started` row names `tool`."""
    conn = _open_proof_db(data_root)
    if conn is None:
        return 0
    try:
        rows = conn.execute(
            "SELECT event_type, target_id, payload FROM events "
            "WHERE event_type IN ('hermes.agent_event', 'hermes.tool_started')").fetchall()
    finally:
        conn.close()
    call_ids: set[str] = set()
    started: dict[str, str] = {}
    for event_type, target_id, payload_raw in rows:
        try:
            payload = json.loads(payload_raw) if payload_raw else None
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if event_type == "hermes.tool_started":
            started[str(target_id)] = _bare_tool_name(payload.get("tool_name", ""))
            continue
        session_ref = payload.get("session_ref")
        if (payload.get("kind") == "tool.call" and isinstance(session_ref, dict)
                and session_ref.get("jarvis_thread_id") == thread_id and payload.get("correlation_id")):
            call_ids.add(str(payload["correlation_id"]))
    wanted = _bare_tool_name(tool)
    return sum(1 for call_id in call_ids if started.get(call_id) == wanted)


def studio_draft_matches(data_root: Path | str, workspace_id: str, started_at: float, ended_at: float,
                         terms: list[str]) -> bool:
    """True when a studio_drafts row of the workspace changed in the window and its source holds every term."""
    conn = _open_proof_db(data_root)
    if conn is None:
        return False
    try:
        rows = conn.execute(
            "SELECT source FROM studio_drafts WHERE workspace_id = ? AND "
            "(julianday(created_at) BETWEEN julianday(?, 'unixepoch') AND julianday(?, 'unixepoch') "
            "OR julianday(updated_at) BETWEEN julianday(?, 'unixepoch') AND julianday(?, 'unixepoch'))",
            (workspace_id, started_at, ended_at, started_at, ended_at)).fetchall()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return any(all(str(term).lower() in str(source or "").lower() for term in terms) for (source,) in rows)


def check_outcome(answer: str, check: dict[str, Any], context: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """Evaluate one check; the second value is the reason an artifact check failed."""
    kind = check.get("kind")
    if kind not in ARTIFACT_CHECK_KINDS:
        return check_passes(answer, check), None
    if not context or not context.get("data_root") or not context.get("thread_id"):
        return False, f"{kind}: requires --data-root and a backend thread"
    if kind == "tool_called":
        tool = str(check.get("tool", ""))
        minimum = int(check.get("min", 1))
        seen = count_named_tool_calls(context["data_root"], context["thread_id"], tool)
        return (True, None) if seen >= minimum else (False, f"tool_called: {tool} seen {seen}, need {minimum}")
    terms = [str(term) for term in check.get("terms", [])]
    if studio_draft_matches(context["data_root"], context["workspace_id"], context["started_at"],
                            context["ended_at"], terms):
        return True, None
    return False, "studio_draft: no matching draft created or updated during the repetition"


def read_job_metrics(data_root: Path | str, started_at: float, ended_at: float) -> dict[str, float | int | None]:
    """Read token use from the isolated proof database for one serial repetition."""
    root = Path(data_root)
    db_file = root if root.is_file() else root / "jarvisos.db"
    empty: dict[str, float | int | None] = {"first_actual_input_tokens": None, "max_actual_input_tokens": None, "input_tokens": None,
                                           "output_tokens": None, "decode_tps": None, "steps_over_6000_input_tokens": None}
    if not db_file.exists():
        return empty
    uri = f"file:{urllib.parse.quote(db_file.resolve().as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        try:
            rows = conn.execute(
                "SELECT normalized_usage_source, input_tokens, output_tokens, latency_ms FROM ai_jobs "
                "WHERE julianday(created_at) BETWEEN julianday(?, 'unixepoch') AND julianday(?, 'unixepoch') "
                "AND selected_route_class LIKE 'local:%' AND status = 'success' ORDER BY created_at, rowid",
                (started_at, ended_at),
            ).fetchall()
        except sqlite3.OperationalError:
            return empty
        first_actual_input_tokens = (int(rows[0][1]) if rows and rows[0][0] == "actual"
                                     and rows[0][1] is not None else None)
        max_actual_input_tokens = (max(int(row[1]) for row in rows)
                                   if rows and all(row[0] == "actual" and row[1] is not None for row in rows)
                                   else None)
        steps_over_6000 = (sum(int(row[1]) > 6000 for row in rows)
                           if all(row[0] == "actual" and row[1] is not None for row in rows) else None)
        input_tokens = sum(int(row[1] or 0) for row in rows)
        output_tokens = sum(int(row[2] or 0) for row in rows)
        latency_s = sum(float(row[3] or 0) for row in rows) / 1000
        samples = [(float(row[2]), float(row[3]) / 1000) for row in rows if row[2] and row[3]]
        decode_tps = output_tokens / latency_s if latency_s > 0 else None
        if len(samples) >= 3:
            mean_x = sum(x for x, _ in samples) / len(samples)
            mean_y = sum(y for _, y in samples) / len(samples)
            spread = sum((x - mean_x) ** 2 for x, _ in samples)
            if spread > 0:
                seconds_per_token = sum((x - mean_x) * (y - mean_y) for x, y in samples) / spread
                if seconds_per_token > 0:
                    decode_tps = 1 / seconds_per_token
        return {"first_actual_input_tokens": first_actual_input_tokens,
                "max_actual_input_tokens": max_actual_input_tokens,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "decode_tps": round(decode_tps, 2) if decode_tps is not None else None,
                "steps_over_6000_input_tokens": steps_over_6000}
    finally:
        conn.close()


def extract_numbers(text: str) -> list[float]:
    """Extract all numeric literals from text."""
    # Remove comma thousand separators within numbers (e.g. 20,000 -> 20000)
    normalized = re.sub(r"(?<=\d),(?=\d)", "", text)
    matches = re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", normalized)
    results: list[float] = []
    for match in matches:
        try:
            val = float(match)
            if math.isfinite(val):
                results.append(val)
        except ValueError:
            continue
    return results


def _check_number(answer: str, check: dict[str, Any]) -> bool:
    target = float(check["value"])
    rel_tol = float(check.get("rel_tol", 0.05))
    scale = check.get("scale")

    def is_close(val: float) -> bool:
        if target == 0.0:
            return abs(val) <= 1e-9
        return abs(val - target) <= rel_tol * abs(target)

    # 1. Check raw extracted numbers
    raw_numbers = extract_numbers(answer)
    for num in raw_numbers:
        if is_close(num):
            return True

    # 2. Check scaled numbers if scale mapping provided
    if isinstance(scale, dict):
        normalized = re.sub(r"(?<=\d),(?=\d)", "", answer)
        # Match number optionally followed by unit text
        pattern = re.compile(
            r"([-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z/%^]+)?",
            re.IGNORECASE,
        )
        scale_lower = {k.lower(): float(v) for k, v in scale.items()}
        for match in pattern.finditer(normalized):
            num_str, unit = match.groups()
            try:
                num = float(num_str)
            except ValueError:
                continue
            if unit and unit.lower() in scale_lower:
                scaled = num * scale_lower[unit.lower()]
                if is_close(scaled):
                    return True

    return False


def _check_contains_any(answer: str, check: dict[str, Any]) -> bool:
    terms = check.get("terms", [])
    answer_lower = answer.lower()
    return any(isinstance(term, str) and term.lower() in answer_lower for term in terms)


def check_passes(answer: str, check: dict[str, Any]) -> bool:
    """Evaluate whether an answer satisfies a check."""
    kind = check.get("kind")
    if kind == "number":
        return _check_number(answer, check)
    if kind == "contains_any":
        return _check_contains_any(answer, check)
    return False


def score_task_detailed(answer: str | None, flow_state: str, task: dict[str, Any],
                        context: dict[str, Any] | None = None) -> tuple[float, int, int, list[str]]:
    """Score a task execution; the last value lists why artifact checks failed.

    Points = share of checks passed * max_points, plus a hard zero when
    flow_state is not completed or the answer is empty.
    """
    checks = task.get("checks", [])
    max_points = float(task.get("max_points", 2.0))
    if not checks:
        return (0.0, 0, 0, [])

    # Hard zero if flow_state is not completed or answer is empty
    if flow_state not in COMPLETED_FLOW_STATES or not answer or not answer.strip():
        return (0.0, 0, len(checks), [])

    outcomes = [check_outcome(answer, check, context) for check in checks]
    passed = sum(1 for ok, _ in outcomes if ok)
    points = (passed / len(checks)) * max_points
    return (points, passed, len(checks), [reason for ok, reason in outcomes if not ok and reason])


def score_task(answer: str | None, flow_state: str, task: dict[str, Any],
               context: dict[str, Any] | None = None) -> tuple[float, int, int]:
    """Score a task execution as (points, checks passed, checks total)."""
    return score_task_detailed(answer, flow_state, task, context)[:3]


def execute_claude_cli(prompt: str, model: str, *, timeout: float) -> tuple[str, str, str | None]:
    """Execute reference query via Claude CLI without paid API keys."""
    if _INJECTED_CLAUDE_RUNNER is not None:
        return _INJECTED_CLAUDE_RUNNER(prompt, model)

    # Real Claude CLI invocation with sanitized environment
    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
    cmd = ["claude", "-m", model, "-p", prompt]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout.strip(), "completed", None
        return proc.stderr.strip(), "failed_terminal", f"claude_cli_exit_{proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("reference task deadline expired") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        return "", "failed_terminal", type(exc).__name__


def _http_request(
    url: str, method: str = "GET", payload: dict[str, Any] | None = None,
    *, deadline: float,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("task deadline expired")
    with urllib.request.urlopen(req, timeout=remaining) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for_model_idle(
    slots_url: str, *, quiet_seconds: float, opener: Callable[..., Any] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 2.0,
) -> tuple[float, bool] | None:
    """Block until no model slot has been processing for `quiet_seconds`.

    Returns (seconds waited, whether a busy slot was seen), or None when `/slots` is unreachable or
    malformed (do not wait).
    """
    start = clock()
    quiet_since: float | None = None
    saw_busy = False
    while True:
        try:
            with opener(slots_url.rstrip("/") + "/slots", timeout=5) as resp:
                slots = json.loads(resp.read().decode("utf-8"))
            if not isinstance(slots, list) or not slots or not all(
                    isinstance(slot, dict) and isinstance(slot.get("is_processing"), bool) for slot in slots):
                return None
            busy = any(slot["is_processing"] for slot in slots)
        except (OSError, ValueError, AttributeError, TypeError):
            return None
        now = clock()
        if busy:
            saw_busy = True
            quiet_since = None
        else:
            quiet_since = now if quiet_since is None else quiet_since
            if now - quiet_since >= quiet_seconds:
                return now - start, saw_busy
        sleep(poll_seconds)


def _std_error(values: list[float]) -> float:
    """Sample standard deviation over sqrt(n); 0 for fewer than two values."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1)) / math.sqrt(len(values))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def rep_passed(rep: dict[str, Any]) -> bool:
    """A repetition passes when every check passed; earlier results are judged by full points."""
    if isinstance(rep.get("passed"), bool):
        return rep["passed"]
    return (rep.get("flow_state") in COMPLETED_FLOW_STATES and float(rep.get("max_points") or 0) > 0
            and float(rep.get("points", 0)) >= float(rep["max_points"]) - 1e-9)


def task_pass_stats(reps: list[dict[str, Any]]) -> dict[str, Any]:
    passes = sum(1 for rep in reps if rep_passed(rep))
    return {"passes": passes, "n": len(reps), "pass_at_1": round(passes / len(reps), 4),
            "pass_at_k": 1.0 if passes == len(reps) else 0.0}


def run_summary(task_results: dict[str, Any]) -> dict[str, Any]:
    """Mean points, pass^1 and pass^k over tasks, each with its standard error over tasks."""
    points = [float(t["mean_score"]) for t in task_results.values()]
    stats = [task_pass_stats(t["repetitions"]) for t in task_results.values()]
    pass1 = [s["pass_at_1"] for s in stats]
    passk = [s["pass_at_k"] for s in stats]
    return {"tasks": len(points), "k": max((s["n"] for s in stats), default=0),
            "mean_points": round(_mean(points), 4), "mean_points_se": round(_std_error(points), 4),
            "mean_pass_at_1": round(_mean(pass1), 4), "pass_at_1_se": round(_std_error(pass1), 4),
            "mean_pass_at_k": round(_mean(passk), 4), "pass_at_k_se": round(_std_error(passk), 4)}


def _task_entry(task_id: str, rep_results: list[dict[str, Any]], category: str = "uncategorized") -> dict[str, Any]:
    points_list = [r["points"] for r in rep_results]
    calls_list = [r["tool_calls"] for r in rep_results]
    walls_list = [r["wall_time_s"] for r in rep_results]
    return {
        "task_id": task_id,
        "category": category,
        "mean_score": round(sum(points_list) / len(points_list), 4),
        "min_score": round(min(points_list), 4),
        **task_pass_stats(rep_results),
        "mean_tool_calls": round(sum(calls_list) / len(calls_list), 2),
        "total_tool_calls": sum(calls_list),
        "mean_wall_time_s": round(sum(walls_list) / len(walls_list), 3),
        "total_wall_time_s": round(sum(walls_list), 3),
        "mean_first_actual_input_tokens": _mean_present(rep_results, "first_actual_input_tokens"),
        "mean_max_actual_input_tokens": _mean_present(rep_results, "max_actual_input_tokens"),
        "mean_input_tokens": _mean_present(rep_results, "input_tokens"),
        "mean_output_tokens": _mean_present(rep_results, "output_tokens"),
        "mean_decode_tps": _mean_present(rep_results, "decode_tps"),
        "repetitions": rep_results,
    }


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def format_summary(result: dict[str, Any]) -> str:
    s = result["summary"]
    return (f"set={result['set_id']} split={result['split']} tasks={s['tasks']} k={s['k']} "
            f"points={s['mean_points']:.3f}±{s['mean_points_se']:.3f} "
            f"pass^1={s['mean_pass_at_1']:.3f}±{s['pass_at_1_se']:.3f} "
            f"pass^k={s['mean_pass_at_k']:.3f}±{s['pass_at_k_se']:.3f} "
            f"reps={result['completed_repetitions']} tool_calls={result['total_tool_calls']} "
            f"wall={result['total_wall_time_s']:.1f}s" + ("" if result["complete"] else " INCOMPLETE"))


def run_eval(
    *,
    tasks_path: Path | str,
    label: str,
    reps: int = 3,
    base_url: str | None = None,
    out_dir: Path | str,
    data_root: Path | str | None = None,
    reference: str | None = None,
    timeout: float = 3600.0,
    workspace_id: str = "default",
    route_class: str | None = "hermes:agent",
    role_id: str | None = None,
    yield_to_model: str | None = None,
    yield_quiet_seconds: float = 120.0,
    split: str = "dev",
    allow_heldout: bool = False,
    resume: bool = False,
    slots_opener: Callable[..., Any] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run evaluation for task set across repetitions."""
    tasks_file = Path(tasks_path)
    if not tasks_file.is_file():
        raise FileNotFoundError(f"Tasks file not found: {tasks_file}")

    tasks_bytes = tasks_file.read_bytes()
    tasks_sha256 = hashlib.sha256(tasks_bytes).hexdigest()
    task_data = json.loads(tasks_bytes.decode("utf-8"))
    cloud_ok = task_data.get("cloud_ok") is True
    all_tasks = task_data.get("tasks", [])
    if split not in SPLITS:
        raise ValueError(f"split must be one of {', '.join(SPLITS)}")
    if split != "dev" and not allow_heldout:
        raise ValueError(f"--split {split} reads held-out tasks and requires --allow-heldout")
    for task in all_tasks:
        if task.get("split", "dev") not in ("dev", "heldout"):
            raise ValueError(f"Task {task.get('id')}: split must be 'dev' or 'heldout'")
        tags = task.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"Task {task.get('id')}: tags must be a list of strings")
        if "category" in task:
            cat = task["category"]
            if not isinstance(cat, str) or not cat.strip():
                raise ValueError(f"Task {task.get('id')}: category must be a non-empty string")
    tasks = [task for task in all_tasks if split == "all" or task.get("split", "dev") == split]
    if not tasks:
        raise ValueError(f"No tasks in split '{split}'")
    set_id = task_data.get("set_id", tasks_file.stem)

    if reps < 1 or timeout <= 0:
        raise ValueError("reps and timeout must be positive")
    if reference:
        if reference != "haiku":
            raise ValueError("Only the haiku reference is supported")
        if not cloud_ok:
            raise ValueError(
                "Reference runner requires cloud_ok: true in tasks file; task content must be cloud-safe"
            )
    else:
        if not base_url:
            raise ValueError("--base-url is required when not using --reference")
        validate_base_url(base_url)
    if yield_to_model:
        validate_base_url(yield_to_model)
        if not math.isfinite(yield_quiet_seconds) or yield_quiet_seconds < 0:
            raise ValueError("yield_quiet_seconds must be a finite number >= 0")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    task_ids = [task["id"] for task in tasks]
    results_file = out_path / "results.json"
    label_file = out_path / f"{label}.json"
    prior: dict[str, Any] = {}
    if results_file.is_file():
        prior = json.loads(results_file.read_text(encoding="utf-8"))
        if resume:
            expected = {"label": label, "tasks_sha256": tasks_sha256, "split": split, "reps": reps}
            for key, value in expected.items():
                if prior.get(key) != value:
                    raise ValueError(f"Cannot resume: {key} differs ({prior.get(key)!r} vs {value!r})")
        elif prior.get("complete") is False:
            raise ValueError(f"{results_file} holds an unfinished run; use --resume or another --out")
    elif resume:
        raise ValueError(f"Cannot resume: no {results_file}")

    task_results: dict[str, Any] = {}
    yield_pauses = int(prior.get("yield_pauses", 0)) if resume else 0
    yield_paused_s = float(prior.get("yield_paused_s", 0.0)) if resume else 0.0
    yield_logged = False
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def write_results(complete: bool) -> dict[str, Any]:
        entries = list(task_results.values())
        cat_names = sorted({t.get("category", "uncategorized") for t in entries})
        category_summary = {
            cat: run_summary({tid: t for tid, t in task_results.items() if t.get("category", "uncategorized") == cat})
            for cat in cat_names
        }
        data = {
            "label": label,
            "set_id": set_id,
            "split": split,
            "reps": reps,
            "task_ids": task_ids,
            "tasks_sha256": tasks_sha256,
            "complete": complete,
            "completed_repetitions": sum(len(t["repetitions"]) for t in entries),
            "timestamp": started_iso,
            "total_mean_score": round(sum(t["mean_score"] for t in entries), 4),
            "total_tool_calls": sum(t["total_tool_calls"] for t in entries),
            "total_wall_time_s": round(sum(t["total_wall_time_s"] for t in entries), 3),
            "yield_pauses": yield_pauses,
            "yield_paused_s": round(yield_paused_s, 3),
            "summary": run_summary(task_results),
            "category_summary": category_summary,
            "tasks": task_results,
        }
        encoded = json.dumps(data, indent=2)
        _write_atomic(results_file, encoded)
        _write_atomic(label_file, encoded)
        return data

    for task in tasks:
        task_id = task["id"]
        category = task.get("category") or "uncategorized"
        prompt = task["prompt"]
        rep_results: list[dict[str, Any]] = []
        if resume:
            kept = prior.get("tasks", {}).get(task_id, {}).get("repetitions", [])
            rep_results = [r for r in kept if isinstance(r, dict) and r.get("rep", 0) < reps]
            if rep_results:
                task_results[task_id] = _task_entry(task_id, rep_results, category=category)

        for rep in range(len(rep_results), reps):
            yield_wait_s = 0.0
            if yield_to_model:
                waited = wait_for_model_idle(
                    yield_to_model, quiet_seconds=yield_quiet_seconds, opener=slots_opener, clock=clock, sleep=sleep,
                )
                if waited is None:
                    if not yield_logged:
                        print(f"yield: {yield_to_model}/slots unreachable or malformed; not waiting", file=sys.stderr)
                        yield_logged = True
                else:
                    yield_wait_s, saw_busy = waited
                    yield_paused_s += yield_wait_s
                    if saw_busy:
                        yield_pauses += 1
            started_at = time.time()
            start_wall = time.monotonic()
            deadline = start_wall + timeout
            thread_id: str | None = None
            answer = ""
            flow_state = "failed_terminal"
            terminal_reason: str | None = None
            model_id: str | None = None
            resp_role_id: str | None = role_id
            tool_calls = 0

            if reference:
                model_id = reference
                try:
                    answer, flow_state, terminal_reason = execute_claude_cli(prompt, reference, timeout=timeout)
                except TimeoutError:
                    flow_state, terminal_reason = "timed_out", "timeout"
                elapsed_wall = time.monotonic() - start_wall
            else:
                assert base_url is not None
                clean_base = base_url.rstrip("/")
                try:
                    thread_payload = {
                        "workspace_id": workspace_id,
                        "title": f"eval-{label}-{task_id}-rep{rep}",
                    }
                    thread_res = _http_request(
                        f"{clean_base}/ai/threads", method="POST", payload=thread_payload, deadline=deadline,
                    )
                    thread_id = str(thread_res["id"])
                    submit_payload: dict[str, Any] = {
                        "request_id": f"eval-{uuid.uuid4().hex[:12]}",
                        "prompt": prompt,
                        "task_kind": "general",
                    }
                    if route_class:
                        submit_payload["route_class"] = route_class
                    if role_id:
                        submit_payload["role_id"] = role_id
                    query = urllib.parse.urlencode({"workspace_id": workspace_id})
                    submit_url = f"{clean_base}/ai/threads/{thread_id}/interactions?{query}"
                    submit_res = _http_request(
                        submit_url, method="POST", payload=submit_payload, deadline=deadline,
                    )
                    interaction = submit_res["interaction"]
                    interaction_id = interaction["id"]
                    flow_state = str(interaction.get("flow_state", "running"))
                    while flow_state not in TERMINAL_FLOW_STATES:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("task deadline expired")
                        time.sleep(min(0.5, remaining))
                        read_url = f"{clean_base}/ai/threads/{thread_id}?{query}"
                        read_res = _http_request(read_url, deadline=deadline)
                        interaction = next(
                            (item for item in read_res["interactions"] if item["id"] == interaction_id),
                            interaction,
                        )
                        flow_state = str(interaction.get("flow_state", "running"))
                except (TimeoutError, urllib.error.URLError) as exc:
                    if isinstance(exc, TimeoutError) or time.monotonic() >= deadline:
                        flow_state, terminal_reason = "timed_out", "timeout"
                    else:
                        raise

                elapsed_wall = time.monotonic() - start_wall
                if flow_state == "timed_out":
                    answer = ""
                    terminal_reason = "timeout"
                else:
                    answer = str(interaction.get("assistant_text") or "")
                    terminal_reason = interaction.get("terminal_reason")
                    model_id = interaction.get("model_id")
                    resp_role_id = interaction.get("role_id")

                if data_root and thread_id:
                    tool_calls = count_thread_tool_calls(data_root, thread_id)

            ended_at = time.time()
            artifact_context = ({"data_root": data_root, "thread_id": thread_id, "workspace_id": workspace_id,
                                 "started_at": started_at, "ended_at": ended_at}
                                if data_root and thread_id and not reference else None)
            points, passed, total_chk, notes = score_task_detailed(answer, flow_state, task, artifact_context)

            rep_results.append({
                "rep": rep,
                "thread_id": thread_id,
                "answer": answer,
                "flow_state": flow_state,
                "terminal_reason": terminal_reason,
                "model_id": model_id,
                "role_id": resp_role_id,
                "wall_time_s": round(elapsed_wall, 3),
                "yield_wait_s": round(yield_wait_s, 3),
                "tool_calls": tool_calls,
                "points": round(points, 4),
                "max_points": float(task.get("max_points", 2.0)),
                "checks_passed": passed,
                "checks_total": total_chk,
                "passed": total_chk > 0 and passed == total_chk,
                "check_notes": notes,
                **(read_job_metrics(data_root, started_at, ended_at) if data_root and not reference else
                   {"first_actual_input_tokens": None, "max_actual_input_tokens": None, "input_tokens": None,
                    "output_tokens": None, "decode_tps": None, "steps_over_6000_input_tokens": None}),
            })

            task_results[task_id] = _task_entry(task_id, rep_results, category=category)
            write_results(False)

        task_results[task_id] = _task_entry(task_id, rep_results, category=category)

    return write_results(True)


def load_run_results(path_input: str | Path) -> dict[str, Any]:
    """Load results dictionary from a run directory or JSON file."""
    path = Path(path_input)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if path.is_dir():
        results_file = path / "results.json"
        if results_file.is_file():
            return json.loads(results_file.read_text(encoding="utf-8"))
        json_candidates = list(path.glob("*.json"))
        if json_candidates:
            return json.loads(json_candidates[0].read_text(encoding="utf-8"))
    raise FileNotFoundError(f"Cannot load eval results from {path}")


def paired_stats(baseline_tasks: dict[str, Any], candidate_tasks: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    """Per-task paired differences (candidate - baseline) of mean points and pass^1 over `ids`."""
    out: dict[str, Any] = {"tasks": len(ids)}
    for name, value in (("points", lambda t: float(t["mean_score"])),
                        ("pass_at_1", lambda t: task_pass_stats(t["repetitions"])["pass_at_1"])):
        diffs = [value(candidate_tasks[i]) - value(baseline_tasks[i]) for i in ids]
        out[name] = {"mean_diff": round(_mean(diffs), 4), "se": round(_std_error(diffs), 4),
                     "better": sum(d > 1e-9 for d in diffs), "worse": sum(d < -1e-9 for d in diffs),
                     "equal": sum(abs(d) <= 1e-9 for d in diffs)}
    return out


def compare_runs(baseline_data: dict[str, Any], candidate_data: dict[str, Any],
                 *, allow_task_drift: bool = False) -> tuple[int, str]:
    """Compare candidate evaluation run against baseline; see `compare_report`."""
    code, report, _ = compare_report(baseline_data, candidate_data, allow_task_drift=allow_task_drift)
    return code, report


def compare_report(baseline_data: dict[str, Any], candidate_data: dict[str, Any],
                   *, allow_task_drift: bool = False) -> tuple[int, str, dict[str, Any]]:
    """Compare candidate evaluation run against baseline.

    Exits 1 when:
    1. Candidate total mean is more than 0.5 points below baseline.
    2. Any task's mean drops by 1 point or more.
    3. Any candidate repetition ends without a completed answer where the baseline had none.
    """
    baseline_tasks = baseline_data.get("tasks", {})
    candidate_tasks = candidate_data.get("tasks", {})
    if not isinstance(baseline_tasks, dict) or not isinstance(candidate_tasks, dict) or not baseline_tasks:
        raise ValueError("Both runs must contain a nonempty tasks mapping")
    for name, run in (("baseline", baseline_data), ("candidate", candidate_data)):
        if run.get("complete", True) is not True:
            raise ValueError(f"The {name} run is incomplete; finish it with run --resume")
    notes: list[str] = []
    drift = []
    if baseline_data.get("set_id") != candidate_data.get("set_id"):
        drift.append("task set ids differ")
    b_sha, c_sha = baseline_data.get("tasks_sha256"), candidate_data.get("tasks_sha256")
    if b_sha and c_sha and b_sha != c_sha:
        drift.append("task-file SHA-256 differs")
    if set(baseline_tasks) != set(candidate_tasks):
        drift.append("task ids differ")
    if drift and not allow_task_drift:
        raise ValueError("Runs are not comparable: " + ", ".join(drift) + " (--allow-task-drift overrides)")
    if drift:
        notes.append("TASK DRIFT ALLOWED: " + ", ".join(drift) + "; comparing the common tasks only")
    common_ids = sorted(set(baseline_tasks) & set(candidate_tasks))
    if not common_ids:
        raise ValueError("Runs share no task ids")
    for name, run in (("baseline", baseline_data), ("candidate", candidate_data)):
        missing = [f for f in ("split", "tasks_sha256", "complete") if f not in run]
        if missing:
            notes.append(f"The {name} run predates {', '.join(missing)}: treated as split dev, complete, SHA unverified")
    for task_id in common_ids:
        baseline_reps = baseline_tasks[task_id].get("repetitions")
        candidate_reps = candidate_tasks[task_id].get("repetitions")
        if (not isinstance(baseline_reps, list) or not baseline_reps
                or not isinstance(candidate_reps, list)
                or len(baseline_reps) != len(candidate_reps)):
            raise ValueError(f"Runs have missing or mismatched repetitions for {task_id}")
        for rep in baseline_reps + candidate_reps:
            if not isinstance(rep, dict) or "flow_state" not in rep or "answer" not in rep:
                raise ValueError(f"Run has an incomplete repetition for {task_id}")
        for info in (baseline_tasks[task_id], candidate_tasks[task_id]):
            for field in ("mean_score", "min_score"):
                if not isinstance(info.get(field), (int, float)) or not math.isfinite(info[field]):
                    raise ValueError(f"Run has invalid {field} for {task_id}")
    for run in (baseline_data, candidate_data):
        value = run.get("total_mean_score")
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Run has invalid total_mean_score")
        task_sum = sum(info["mean_score"] for info in run["tasks"].values())
        if not math.isclose(value, task_sum, abs_tol=0.001):
            raise ValueError("Run total_mean_score does not match task means")

    b_total_mean = sum(float(baseline_tasks[i]["mean_score"]) for i in common_ids)
    c_total_mean = sum(float(candidate_tasks[i]["mean_score"]) for i in common_ids)

    regressions: list[str] = []

    # Condition 1: total mean drops > 0.5 points
    if (b_total_mean - c_total_mean) > 0.5:
        regressions.append(
            f"Total mean regression: candidate {c_total_mean:.4f} is >0.5 points below baseline {b_total_mean:.4f} (drop of {b_total_mean - c_total_mean:.4f})"
        )

    # Condition 2: any task mean drops >= 1.0 point
    for task_id in common_ids:
        b_info = baseline_tasks[task_id]
        b_mean = float(b_info.get("mean_score", 0.0))
        c_info = candidate_tasks.get(task_id)
        c_mean = float(c_info.get("mean_score", 0.0)) if c_info else 0.0
        if (b_mean - c_mean) >= 1.0:
            regressions.append(
                f"Task '{task_id}' mean regression: candidate {c_mean:.4f} dropped >=1.0 from baseline {b_mean:.4f} (drop of {b_mean - c_mean:.4f})"
            )

    # Condition 3: candidate uncompleted repetition where baseline had none
    def count_uncompleted(data: dict[str, Any]) -> int:
        count = 0
        for task_id in common_ids:
            for rep in data["tasks"][task_id].get("repetitions", []):
                state = rep.get("flow_state")
                ans = rep.get("answer")
                if state not in COMPLETED_FLOW_STATES or not (ans or "").strip():
                    count += 1
        return count

    b_uncompleted = count_uncompleted(baseline_data)
    c_uncompleted = count_uncompleted(candidate_data)
    if b_uncompleted == 0 and c_uncompleted > 0:
        regressions.append(
            f"Uncompleted answer regression: candidate has {c_uncompleted} uncompleted turn(s) while baseline had 0"
        )

    # Build comparison table
    lines: list[str] = []
    lines.append(f"{'Task ID':<30} | {'Base Mean':<10} {'Min':<6} {'Calls':<6} {'Wall(s)':<8} | {'Cand Mean':<10} {'Min':<6} {'Calls':<6} {'Wall(s)':<8} | {'Delta':<7}")
    lines.append("-" * 105)

    for tid in common_ids:
        b = baseline_tasks.get(tid, {})
        c = candidate_tasks.get(tid, {})
        b_m = b.get("mean_score", 0.0)
        b_min = b.get("min_score", 0.0)
        b_calls = b.get("mean_tool_calls", 0.0)
        b_wall = b.get("mean_wall_time_s", 0.0)

        c_m = c.get("mean_score", 0.0)
        c_min = c.get("min_score", 0.0)
        c_calls = c.get("mean_tool_calls", 0.0)
        c_wall = c.get("mean_wall_time_s", 0.0)

        delta = c_m - b_m
        lines.append(
            f"{tid:<30} | {b_m:<10.2f} {b_min:<6.2f} {b_calls:<6.1f} {b_wall:<8.2f} | {c_m:<10.2f} {c_min:<6.2f} {c_calls:<6.1f} {c_wall:<8.2f} | {delta:+6.2f}"
        )

    lines.append("-" * 105)
    total_delta = c_total_mean - b_total_mean
    lines.append(
        f"{'TOTAL':<30} | {b_total_mean:<10.2f} {'':<6} {baseline_data.get('total_tool_calls', 0):<6} {baseline_data.get('total_wall_time_s', 0):<8.2f} | {c_total_mean:<10.2f} {'':<6} {candidate_data.get('total_tool_calls', 0):<6} {candidate_data.get('total_wall_time_s', 0):<8.2f} | {total_delta:+6.2f}"
    )

    paired = paired_stats(baseline_tasks, candidate_tasks, common_ids)
    lines.append(f"\nPaired over {paired['tasks']} tasks (candidate - baseline, mean difference ± paired SE):")
    for key, label in (("points", "points"), ("pass_at_1", "pass^1")):
        p = paired[key]
        lines.append(f"  {label:<7} {p['mean_diff']:+.4f} ± {p['se']:.4f}   "
                     f"better {p['better']}  worse {p['worse']}  equal {p['equal']}")
    lines.extend(notes)

    code = 1 if regressions else 0
    if regressions:
        lines.append("\nREGRESSION FAILURES DETECTED:")
        for reg in regressions:
            lines.append(f"  [X] {reg}")
        lines.append("\nResult: FAIL (exit 1)")
    else:
        lines.append("\nResult: PASS (within regression bounds, exit 0)")
    return (code, "\n".join(lines),
            {"exit_code": code, "regressions": regressions, "notes": notes, "paired": paired,
             "baseline_total_mean": round(b_total_mean, 4), "candidate_total_mean": round(c_total_mean, 4)})


def _mean_present(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(sum(values) / len(values), 2) if values else None


def _non_negative_seconds(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return value


def _compute_ratio_stats(
    num_mean: float, num_se: float, den_mean: float, den_se: float
) -> dict[str, Any]:
    """Delta-method standard error and 95% confidence interval for ratio num/den."""
    if den_mean <= 0:
        return {
            "ratio": None,
            "se": None,
            "ci_low": None,
            "ci_high": None,
            "ci_includes_one": False,
        }
    if num_mean <= 0:
        return {
            "ratio": round(num_mean / den_mean, 4),
            "se": None,
            "ci_low": None,
            "ci_high": None,
            "ci_includes_one": False,
        }
    ratio = num_mean / den_mean
    rel_num = (num_se / num_mean) ** 2
    rel_den = (den_se / den_mean) ** 2
    se = ratio * math.sqrt(rel_num + rel_den)
    ci_low = ratio - 1.96 * se
    ci_high = ratio + 1.96 * se
    ci_includes_one = bool(ci_low <= 1.0 <= ci_high)
    return {
        "ratio": round(ratio, 4),
        "se": round(se, 4),
        "ci_low": round(ci_low, 4),
        "ci_high": round(ci_high, 4),
        "ci_includes_one": ci_includes_one,
    }


def parity_report(
    arm_a: dict[str, Any] | str | Path,
    arm_b: dict[str, Any] | str | Path,
    arm_c: dict[str, Any] | str | Path | None = None,
    *,
    allow_task_drift: bool = False,
) -> dict[str, Any]:
    """Three-arm parity comparator between reference (A), harness (B), and local (C)."""
    vault = vault_guard.vault_root()
    for arg in (arm_a, arm_b, arm_c):
        if isinstance(arg, (str, Path)) and vault_guard.touches_vault(arg, vault):
            raise PermissionError(vault_guard.REFUSED_MESSAGE)

    a_data = load_run_results(arm_a) if isinstance(arm_a, (str, Path)) else arm_a
    b_data = load_run_results(arm_b) if isinstance(arm_b, (str, Path)) else arm_b
    c_data = load_run_results(arm_c) if isinstance(arm_c, (str, Path)) else arm_c

    if not isinstance(a_data, dict) or not isinstance(b_data, dict):
        raise ValueError("arm_a and arm_b must be valid run data dictionaries or paths")
    if c_data is not None and not isinstance(c_data, dict):
        raise ValueError("arm_c must be a valid run data dictionary or path if provided")

    arms: list[tuple[str, dict[str, Any]]] = [("arm_a", a_data), ("arm_b", b_data)]
    if c_data is not None:
        arms.append(("arm_c", c_data))

    for name, run in arms:
        tasks = run.get("tasks")
        if not isinstance(tasks, dict) or not tasks:
            raise ValueError(f"Run {name} must contain a nonempty tasks mapping")
        if run.get("complete", True) is not True:
            raise ValueError(f"The {name} run is incomplete; finish it with run --resume")

    drift: list[str] = []
    set_ids = {run.get("set_id") for _, run in arms if "set_id" in run}
    if len(set_ids) > 1:
        drift.append("task set ids differ")

    shas = {run.get("tasks_sha256") for _, run in arms if run.get("tasks_sha256")}
    if len(shas) > 1:
        drift.append("task-file SHA-256 differs")

    task_id_sets = [set(run["tasks"].keys()) for _, run in arms]
    if not all(s == task_id_sets[0] for s in task_id_sets[1:]):
        drift.append("task ids differ")

    if drift and not allow_task_drift:
        raise ValueError("Runs are not comparable: " + ", ".join(drift) + " (--allow-task-drift overrides)")

    common_ids = sorted(set.intersection(*task_id_sets))
    if not common_ids:
        raise ValueError("Runs share no task ids")

    for task_id in common_ids:
        for name, run in arms:
            t = run["tasks"][task_id]
            reps = t.get("repetitions")
            if not isinstance(reps, list) or not reps:
                raise ValueError(f"Run {name} has missing repetitions for {task_id}")
            for rep in reps:
                if not isinstance(rep, dict) or "flow_state" not in rep or "answer" not in rep:
                    raise ValueError(f"Run {name} has an incomplete repetition for {task_id}")
            for field in ("mean_score", "min_score"):
                if not isinstance(t.get(field), (int, float)) or not math.isfinite(t[field]):
                    raise ValueError(f"Run {name} has invalid {field} for {task_id}")

    def _task_category(task_id: str) -> str:
        for _, run in arms:
            cat = run["tasks"][task_id].get("category")
            if isinstance(cat, str) and cat.strip():
                return cat
        return "uncategorized"

    categories = sorted({_task_category(tid) for tid in common_ids})

    a_tasks = a_data["tasks"]
    b_tasks = b_data["tasks"]
    c_tasks = c_data["tasks"] if c_data is not None else None

    arm_a_overall = run_summary({tid: a_tasks[tid] for tid in common_ids})
    arm_b_overall = run_summary({tid: b_tasks[tid] for tid in common_ids})
    arm_c_overall = run_summary({tid: c_tasks[tid] for tid in common_ids}) if c_tasks is not None else None

    overall_gz = paired_stats(a_tasks, b_tasks, common_ids)
    overall_alarm = bool(arm_b_overall["mean_points"] < arm_a_overall["mean_points"])

    worst_b_minus_a_tasks = []
    for tid in common_ids:
        a_score = float(a_tasks[tid]["mean_score"])
        b_score = float(b_tasks[tid]["mean_score"])
        worst_b_minus_a_tasks.append({
            "task_id": tid,
            "category": _task_category(tid),
            "a_mean": round(a_score, 4),
            "b_mean": round(b_score, 4),
            "diff": round(b_score - a_score, 4),
        })
    worst_b_minus_a_tasks.sort(key=lambda x: (x["diff"], x["task_id"]))

    category_reports: dict[str, dict[str, Any]] = {}
    any_cat_alarm = False

    for cat in categories:
        cat_ids = [tid for tid in common_ids if _task_category(tid) == cat]
        cat_a_sub = {tid: a_tasks[tid] for tid in cat_ids}
        cat_b_sub = {tid: b_tasks[tid] for tid in cat_ids}
        cat_a_summary = run_summary(cat_a_sub)
        cat_b_summary = run_summary(cat_b_sub)
        cat_gz = paired_stats(a_tasks, b_tasks, cat_ids)

        cat_alarm = bool(cat_b_summary["mean_points"] < cat_a_summary["mean_points"])
        if cat_alarm:
            any_cat_alarm = True

        cat_b_over_a = _compute_ratio_stats(
            cat_b_summary["mean_points"],
            cat_b_summary["mean_points_se"],
            cat_a_summary["mean_points"],
            cat_a_summary["mean_points_se"],
        )

        cat_entry: dict[str, Any] = {
            "tasks": len(cat_ids),
            "task_ids": cat_ids,
            "arm_a": cat_a_summary,
            "arm_b": cat_b_summary,
            "gate_zero": cat_gz,
            "gate_zero_alarm": cat_alarm,
            "b_over_a": cat_b_over_a,
        }

        if c_tasks is not None:
            cat_c_sub = {tid: c_tasks[tid] for tid in cat_ids}
            cat_c_summary = run_summary(cat_c_sub)
            cat_entry["arm_c"] = cat_c_summary

            cat_c_over_a = _compute_ratio_stats(
                cat_c_summary["mean_points"],
                cat_c_summary["mean_points_se"],
                cat_a_summary["mean_points"],
                cat_a_summary["mean_points_se"],
            )
            cat_entry["c_over_a"] = cat_c_over_a

            if cat_b_summary["mean_points"] >= cat_a_summary["mean_points"]:
                max_ab_m = cat_b_summary["mean_points"]
                max_ab_se = cat_b_summary["mean_points_se"]
            else:
                max_ab_m = cat_a_summary["mean_points"]
                max_ab_se = cat_a_summary["mean_points_se"]

            cat_c_over_max_ab = _compute_ratio_stats(
                cat_c_summary["mean_points"],
                cat_c_summary["mean_points_se"],
                max_ab_m,
                max_ab_se,
            )
            cat_entry["c_over_max_ab"] = cat_c_over_max_ab
        else:
            cat_entry["arm_c"] = None

        category_reports[cat] = cat_entry

    gate_zero_alarm = overall_alarm or any_cat_alarm

    b_over_a_overall = _compute_ratio_stats(
        arm_b_overall["mean_points"],
        arm_b_overall["mean_points_se"],
        arm_a_overall["mean_points"],
        arm_a_overall["mean_points_se"],
    )

    c_over_a_overall = None
    c_over_max_ab_overall = None
    parity_met = False

    if arm_c_overall is not None:
        c_over_a_overall = _compute_ratio_stats(
            arm_c_overall["mean_points"],
            arm_c_overall["mean_points_se"],
            arm_a_overall["mean_points"],
            arm_a_overall["mean_points_se"],
        )
        if arm_b_overall["mean_points"] >= arm_a_overall["mean_points"]:
            ov_max_ab_m = arm_b_overall["mean_points"]
            ov_max_ab_se = arm_b_overall["mean_points_se"]
        else:
            ov_max_ab_m = arm_a_overall["mean_points"]
            ov_max_ab_se = arm_a_overall["mean_points_se"]

        c_over_max_ab_overall = _compute_ratio_stats(
            arm_c_overall["mean_points"],
            arm_c_overall["mean_points_se"],
            ov_max_ab_m,
            ov_max_ab_se,
        )
        parity_met = bool(category_reports) and all(
            bool(cat_info.get("c_over_max_ab", {}).get("ci_includes_one"))
            for cat_info in category_reports.values()
        )

    set_id = a_data.get("set_id") or b_data.get("set_id") or "unknown"
    split = a_data.get("split") or b_data.get("split") or "dev"
    tasks_sha256 = a_data.get("tasks_sha256") or b_data.get("tasks_sha256")

    report: dict[str, Any] = {
        "set_id": set_id,
        "split": split,
        "tasks_sha256": tasks_sha256,
        "task_ids": common_ids,
        "tasks_count": len(common_ids),
        "arm_a": arm_a_overall,
        "arm_b": arm_b_overall,
        "arm_c": arm_c_overall,
        "gate_zero": overall_gz,
        "gate_zero_alarm": gate_zero_alarm,
        "worst_b_minus_a_tasks": worst_b_minus_a_tasks,
        "b_over_a": b_over_a_overall,
        "parity_met": parity_met,
        "categories": category_reports,
    }
    if arm_c_overall is not None:
        report["c_over_a"] = c_over_a_overall
        report["c_over_max_ab"] = c_over_max_ab_overall

    report["overall"] = {
        "arm_a": arm_a_overall,
        "arm_b": arm_b_overall,
        "arm_c": arm_c_overall,
        "gate_zero": overall_gz,
        "gate_zero_alarm": overall_alarm,
        "b_over_a": b_over_a_overall,
        "c_over_a": c_over_a_overall,
        "c_over_max_ab": c_over_max_ab_overall,
        "parity_met": parity_met,
    }
    return report


def format_parity_table(report: dict[str, Any]) -> str:
    """Format three-arm parity comparison table with Gate Zero alarm banner."""
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append(f"AGENT EVAL PARITY REPORT: set={report['set_id']} split={report['split']} tasks={report['tasks_count']}")
    lines.append("=" * 80)

    if report["gate_zero_alarm"]:
        lines.append("********************************************************************************")
        lines.append("[!] GATE ZERO ALARM: Reference-in-harness (Arm B) regressed below Arm A!")
        lines.append("********************************************************************************")
    else:
        lines.append("Gate Zero: PASS (Arm B >= Arm A across all categories and overall)")

    has_c = report.get("arm_c") is not None
    if has_c:
        if report["parity_met"]:
            lines.append("Parity Status: PARITY MET (All category 95% CIs include 1.0)")
        else:
            lines.append("Parity Status: PARITY NOT MET (One or more category CIs exclude 1.0)")

    lines.append("")
    if has_c:
        lines.append(f"{'Category':<22} | {'Tasks':<5} | {'Arm A':<14} | {'Arm B':<14} | {'B - A (SE)':<16} | {'B / A':<10} | {'Arm C':<14} | {'C / max(A,B)':<18}")
        lines.append("-" * 125)
    else:
        lines.append(f"{'Category':<22} | {'Tasks':<5} | {'Arm A':<14} | {'Arm B':<14} | {'B - A (SE)':<16} | {'B / A':<10}")
        lines.append("-" * 95)

    def _fmt_mean(arm_dict: dict[str, Any] | None) -> str:
        if not arm_dict:
            return "n/d"
        return f"{arm_dict['mean_points']:.3f}±{arm_dict['mean_points_se']:.3f}"

    def _fmt_ratio(r_dict: dict[str, Any] | None) -> str:
        if not r_dict or r_dict.get("ratio") is None:
            return "n/d"
        se_str = f"±{r_dict['se']:.3f}" if r_dict.get("se") is not None else ""
        return f"{r_dict['ratio']:.3f}{se_str}"

    def _fmt_gz(gz: dict[str, Any]) -> str:
        pts = gz.get("points", {})
        return f"{pts.get('mean_diff', 0.0):+.3f}±{pts.get('se', 0.0):.3f}"

    for cat_name, cat in report.get("categories", {}).items():
        row = f"{cat_name:<22} | {cat['tasks']:<5} | {_fmt_mean(cat['arm_a']):<14} | {_fmt_mean(cat['arm_b']):<14} | {_fmt_gz(cat['gate_zero']):<16} | {_fmt_ratio(cat['b_over_a']):<10}"
        if has_c:
            row += f" | {_fmt_mean(cat.get('arm_c')):<14} | {_fmt_ratio(cat.get('c_over_max_ab')):<18}"
        lines.append(row)

    if has_c:
        lines.append("-" * 125)
    else:
        lines.append("-" * 95)

    ov_row = f"{'OVERALL':<22} | {report['tasks_count']:<5} | {_fmt_mean(report['arm_a']):<14} | {_fmt_mean(report['arm_b']):<14} | {_fmt_gz(report['gate_zero']):<16} | {_fmt_ratio(report['b_over_a']):<10}"
    if has_c:
        ov_row += f" | {_fmt_mean(report.get('arm_c')):<14} | {_fmt_ratio(report.get('c_over_max_ab')):<18}"
    lines.append(ov_row)

    worst = report.get("worst_b_minus_a_tasks", [])
    if worst:
        lines.append("\nWorst B - A task differences (worst first):")
        lines.append(f"  {'Task ID':<30} | {'Category':<18} | {'Arm A':<8} | {'Arm B':<8} | {'Diff':<8}")
        lines.append("  " + "-" * 80)
        for t in worst[:10]:
            lines.append(f"  {t['task_id']:<30} | {t['category']:<18} | {t['a_mean']:<8.2f} | {t['b_mean']:<8.2f} | {t['diff']:<+8.2f}")

    return "\n".join(lines)


def update_scoreboard(
    scoreboard_path: Path | str,
    report: dict[str, Any],
    *,
    date: str | None = None,
    change_label: str | None = None,
) -> Path:
    """Atomically create or append a row to a plain-Italian Markdown scoreboard table."""
    path = Path(scoreboard_path)
    vault = vault_guard.vault_root()
    if vault_guard.touches_vault(path, vault):
        raise PermissionError(vault_guard.REFUSED_MESSAGE)

    date_str = date or time.strftime("%Y-%m-%d", time.gmtime())
    label_str = (change_label or "-").replace("|", "/").replace("\n", " ").strip()
    split_str = str(report.get("split", "dev"))
    gz_status = "ALARM" if report.get("gate_zero_alarm") else "PASS"

    has_c = report.get("arm_c") is not None
    categories = sorted(report.get("categories", {}).keys())

    def _cat_val(cat_name: str) -> str:
        if not has_c:
            return "n/d"
        cat_data = report.get("categories", {}).get(cat_name, {})
        ratio = cat_data.get("c_over_max_ab", {}).get("ratio")
        return f"{ratio:.2f}" if ratio is not None else "n/d"

    overall_val = "n/d"
    if has_c:
        ov_ratio = report.get("c_over_max_ab", {}).get("ratio")
        if ov_ratio is not None:
            overall_val = f"{ov_ratio:.2f}"

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        existing_text = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in existing_text.splitlines() if line.strip()]
        if lines and lines[0].startswith("|"):
            header_cells = [cell.strip() for cell in lines[0].split("|")[1:-1]]
            if (
                len(header_cells) >= 5
                and header_cells[0].lower() in ("data", "date")
                and header_cells[-1].lower() in ("totale", "overall")
            ):
                cat_cols = header_cells[4:-1]
                cat_vals = [_cat_val(c) for c in cat_cols]
                row = f"| {date_str} | {label_str} | {split_str} | {gz_status} | " + " | ".join(cat_vals) + f" | {overall_val} |"
                new_text = existing_text.rstrip("\n") + "\n" + row + "\n"
                _write_atomic(path, new_text)
                return path

    header_cols = ["Data", "Modifica", "Split", "Gate Zero (B/A)"] + categories + ["Totale"]
    header_line = "| " + " | ".join(header_cols) + " |"
    sep_line = "| " + " | ".join(["---"] * len(header_cols)) + " |"
    cat_vals = [_cat_val(c) for c in categories]
    row_line = f"| {date_str} | {label_str} | {split_str} | {gz_status} | " + " | ".join(cat_vals) + f" | {overall_val} |"

    content = header_line + "\n" + sep_line + "\n" + row_line + "\n"
    _write_atomic(path, content)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_eval",
        description="Start a proof backend on an isolated data-root copy and proof port before running. "
        "Repeatable agent eval gate runner and comparator for JarvisOS",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # run subcommand
    run_parser = subparsers.add_parser(
        "run", help="Run agent evaluation on a task set",
        description="The proof backend must run with the operator environment that provides the Hermes venv; "
                    "otherwise every turn fails immediately.")
    run_parser.add_argument("--tasks", required=True, help="Path to task set JSON file")
    run_parser.add_argument("--label", required=True, help="Run label identifier")
    run_parser.add_argument("--reps", type=int, default=3, help="Repetitions per task (default 3)")
    run_parser.add_argument("--base-url", help="Backend base URL (e.g. http://127.0.0.1:8001)")
    run_parser.add_argument("--out", required=True, help="Output directory for results")
    run_parser.add_argument("--data-root", help="Path to proof data-root copy")
    run_parser.add_argument("--reference", help="Reference model (e.g. haiku)")
    run_parser.add_argument("--timeout", type=float, default=3600.0, help="Per-task wall timeout in seconds")
    run_parser.add_argument("--workspace-id", default="default", help="Workspace id (default 'default')")
    run_parser.add_argument("--route-class", default="hermes:agent", help="Route class (default 'hermes:agent')")
    run_parser.add_argument("--role-id", help="Hermes role id (e.g. 'isaac')")
    run_parser.add_argument("--yield-to-model", help="Loopback model server base URL; wait for its /slots to be idle "
                            "before every repetition so the maintainer's own turns go first")
    run_parser.add_argument("--yield-quiet-seconds", type=_non_negative_seconds, default=120.0,
                            help="Continuous idle window required before a repetition (default 120)")

    run_parser.add_argument("--split", choices=SPLITS, default="dev",
                            help="Task split to run (default dev); heldout and all need --allow-heldout")
    run_parser.add_argument("--allow-heldout", action="store_true", help="Permit reading held-out tasks")
    run_parser.add_argument("--resume", action="store_true",
                            help="Continue an unfinished run in --out (same label, task file, split and reps)")

    # compare subcommand
    cmp_parser = subparsers.add_parser("compare", help="Compare candidate run against baseline")
    cmp_parser.add_argument("--baseline", required=True, help="Baseline run directory or results JSON")
    cmp_parser.add_argument("--candidate", required=True, help="Candidate run directory or results JSON")
    cmp_parser.add_argument("--allow-task-drift", action="store_true",
                            help="Compare common tasks although set id, task file or task ids differ")
    cmp_parser.add_argument("--json", help="Also write the comparison numbers to this JSON file")

    # parity subcommand
    parity_parser = subparsers.add_parser(
        "parity",
        help="Compare standalone reference (Arm A), reference-in-harness (Arm B), and optional local-in-harness (Arm C)",
    )
    parity_parser.add_argument("--arm-a", required=True, help="Arm A (standalone reference) run directory or results JSON")
    parity_parser.add_argument("--arm-b", required=True, help="Arm B (reference in harness) run directory or results JSON")
    parity_parser.add_argument("--arm-c", help="Arm C (local model in harness) run directory or results JSON")
    parity_parser.add_argument("--allow-task-drift", action="store_true",
                               help="Compare common tasks although set id, task file or task ids differ")
    parity_parser.add_argument("--json", help="Path to write structured parity report JSON")
    parity_parser.add_argument("--scoreboard", help="Path to plain-Italian Markdown scoreboard table to create or append")
    parity_parser.add_argument("--change-label", help="Label for change in scoreboard (e.g. 'prompt-tune-v2')")
    parity_parser.add_argument("--date", help="Date string for scoreboard row (YYYY-MM-DD, defaults to today UTC)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "run":
        if (not args.reference and args.route_class and args.route_class.startswith("hermes:")
                and args.timeout < 660):
            print("Error: Hermes --timeout must be at least 660 seconds (longer than the worker turn deadline)",
                  file=sys.stderr)
            return 1
        vault = vault_guard.vault_root()
        if any(vault_guard.touches_vault(p, vault) for p in (args.data_root, args.out) if p):
            print(f"Error: {vault_guard.REFUSED_MESSAGE}", file=sys.stderr)
            return 1
        try:
            result = run_eval(
                tasks_path=args.tasks,
                label=args.label,
                reps=args.reps,
                base_url=args.base_url,
                out_dir=args.out,
                data_root=args.data_root,
                reference=args.reference,
                timeout=args.timeout,
                workspace_id=args.workspace_id,
                route_class=args.route_class,
                role_id=args.role_id,
                yield_to_model=args.yield_to_model,
                yield_quiet_seconds=args.yield_quiet_seconds,
                split=args.split,
                allow_heldout=args.allow_heldout,
                resume=args.resume,
            )
            print(format_summary(result))
            print(f"Eval run completed successfully. Results written to {args.out}")
            return 0
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, urllib.error.URLError) as exc:
            print(f"Error executing eval run: {exc}", file=sys.stderr)
            return 1

    if args.subcommand == "compare":
        try:
            baseline_data = load_run_results(args.baseline)
            candidate_data = load_run_results(args.candidate)
            code, report, numbers = compare_report(
                baseline_data, candidate_data, allow_task_drift=args.allow_task_drift)
            print(report)
            if args.json:
                Path(args.json).write_text(json.dumps(numbers, indent=2), encoding="utf-8")
            return code
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Error comparing runs: {exc}", file=sys.stderr)
            return 1

    if args.subcommand == "parity":
        vault = vault_guard.vault_root()
        parity_paths = [args.arm_a, args.arm_b, args.arm_c, args.json, args.scoreboard]
        if any(p and vault_guard.touches_vault(p, vault) for p in parity_paths):
            print(vault_guard.REFUSED_MESSAGE, file=sys.stderr)
            return vault_guard.REFUSED_EXIT
        try:
            report = parity_report(
                args.arm_a,
                args.arm_b,
                args.arm_c,
                allow_task_drift=args.allow_task_drift,
            )
            print(format_parity_table(report))
            if args.json:
                json_path = Path(args.json)
                json_path.parent.mkdir(parents=True, exist_ok=True)
                json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            if args.scoreboard:
                update_scoreboard(
                    args.scoreboard,
                    report,
                    date=args.date,
                    change_label=args.change_label,
                )
            return 0
        except (OSError, ValueError, KeyError, TypeError, PermissionError) as exc:
            print(f"Error generating parity report: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
