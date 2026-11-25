#!/usr/bin/env python3
"""Repeatable agent regression eval runner and comparator for JarvisOS.

Stdlib only. Drives the chat -> Hermes path, scores answers against deterministic
checks, and compares candidate runs with baselines to fail on regressions.
"""

from __future__ import annotations

import argparse
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

TERMINAL_FLOW_STATES = frozenset(
    {"complete", "completed", "failed", "failed_terminal", "cancelled_terminal", "timed_out"}
)
COMPLETED_FLOW_STATES = frozenset({"complete", "completed"})

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


def score_task(answer: str | None, flow_state: str, task: dict[str, Any]) -> tuple[float, int, int]:
    """Score a task execution.

    Points = share of checks passed * max_points, plus a hard zero when
    flow_state is not completed or the answer is empty.
    """
    checks = task.get("checks", [])
    max_points = float(task.get("max_points", 2.0))
    if not checks:
        return (0.0, 0, 0)

    # Hard zero if flow_state is not completed or answer is empty
    if flow_state not in COMPLETED_FLOW_STATES or not answer or not answer.strip():
        return (0.0, 0, len(checks))

    passed = sum(1 for check in checks if check_passes(answer, check))
    points = (passed / len(checks)) * max_points
    return (points, passed, len(checks))


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
) -> dict[str, Any]:
    """Run evaluation for task set across repetitions."""
    tasks_file = Path(tasks_path)
    if not tasks_file.is_file():
        raise FileNotFoundError(f"Tasks file not found: {tasks_file}")

    task_data = json.loads(tasks_file.read_text(encoding="utf-8"))
    cloud_ok = task_data.get("cloud_ok") is True
    tasks = task_data.get("tasks", [])
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

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    task_results: dict[str, Any] = {}
    total_tool_calls_overall = 0
    total_wall_time_overall = 0.0

    for task in tasks:
        task_id = task["id"]
        prompt = task["prompt"]
        rep_results: list[dict[str, Any]] = []

        for rep in range(reps):
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

            points, passed, total_chk = score_task(answer, flow_state, task)
            total_tool_calls_overall += tool_calls
            total_wall_time_overall += elapsed_wall

            rep_results.append({
                "rep": rep,
                "thread_id": thread_id,
                "answer": answer,
                "flow_state": flow_state,
                "terminal_reason": terminal_reason,
                "model_id": model_id,
                "role_id": resp_role_id,
                "wall_time_s": round(elapsed_wall, 3),
                "tool_calls": tool_calls,
                "points": round(points, 4),
                "max_points": float(task.get("max_points", 2.0)),
                "checks_passed": passed,
                "checks_total": total_chk,
                **(read_job_metrics(data_root, started_at, time.time()) if data_root and not reference else
                   {"first_actual_input_tokens": None, "max_actual_input_tokens": None, "input_tokens": None,
                    "output_tokens": None, "decode_tps": None, "steps_over_6000_input_tokens": None}),
            })

        points_list = [r["points"] for r in rep_results]
        calls_list = [r["tool_calls"] for r in rep_results]
        walls_list = [r["wall_time_s"] for r in rep_results]

        task_results[task_id] = {
            "task_id": task_id,
            "mean_score": round(sum(points_list) / len(points_list), 4),
            "min_score": round(min(points_list), 4),
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

    total_mean_score = sum(t["mean_score"] for t in task_results.values())
    result_data = {
        "label": label,
        "set_id": set_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_mean_score": round(total_mean_score, 4),
        "total_tool_calls": total_tool_calls_overall,
        "total_wall_time_s": round(total_wall_time_overall, 3),
        "tasks": task_results,
    }

    results_file = out_path / "results.json"
    label_file = out_path / f"{label}.json"
    encoded = json.dumps(result_data, indent=2)
    results_file.write_text(encoded, encoding="utf-8")
    label_file.write_text(encoded, encoding="utf-8")
    return result_data


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


def compare_runs(baseline_data: dict[str, Any], candidate_data: dict[str, Any]) -> tuple[int, str]:
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
    if baseline_data.get("set_id") != candidate_data.get("set_id"):
        raise ValueError("Runs have different task set ids")
    if set(baseline_tasks) != set(candidate_tasks):
        raise ValueError("Runs have different task ids")
    for task_id in baseline_tasks:
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

    b_total_mean = float(baseline_data.get("total_mean_score", 0.0))
    c_total_mean = float(candidate_data.get("total_mean_score", 0.0))

    regressions: list[str] = []

    # Condition 1: total mean drops > 0.5 points
    if (b_total_mean - c_total_mean) > 0.5:
        regressions.append(
            f"Total mean regression: candidate {c_total_mean:.4f} is >0.5 points below baseline {b_total_mean:.4f} (drop of {b_total_mean - c_total_mean:.4f})"
        )

    # Condition 2: any task mean drops >= 1.0 point
    for task_id, b_info in baseline_tasks.items():
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
        for task in data.get("tasks", {}).values():
            for rep in task.get("repetitions", []):
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

    all_task_ids = sorted(set(baseline_tasks) | set(candidate_tasks))
    for tid in all_task_ids:
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

    if regressions:
        lines.append("\nREGRESSION FAILURES DETECTED:")
        for reg in regressions:
            lines.append(f"  [X] {reg}")
        lines.append("\nResult: FAIL (exit 1)")
        return (1, "\n".join(lines))

    lines.append("\nResult: PASS (within regression bounds, exit 0)")
    return (0, "\n".join(lines))


def _mean_present(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(sum(values) / len(values), 2) if values else None


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

    # compare subcommand
    cmp_parser = subparsers.add_parser("compare", help="Compare candidate run against baseline")
    cmp_parser.add_argument("--baseline", required=True, help="Baseline run directory or results JSON")
    cmp_parser.add_argument("--candidate", required=True, help="Candidate run directory or results JSON")

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
        try:
            run_eval(
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
            )
            print(f"Eval run completed successfully. Results written to {args.out}")
            return 0
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, urllib.error.URLError) as exc:
            print(f"Error executing eval run: {exc}", file=sys.stderr)
            return 1

    if args.subcommand == "compare":
        try:
            baseline_data = load_run_results(args.baseline)
            candidate_data = load_run_results(args.candidate)
            code, report = compare_runs(baseline_data, candidate_data)
            print(report)
            return code
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Error comparing runs: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
