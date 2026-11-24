"""Tests for Spec 354 — AGENT-EVAL-GATE-1.

Repeatable agent regression eval for the local default and the Hermes harness.
Criteria 1–5:
1. Stub HTTP server: run creates one thread per task and rep, records listed fields,
   marks timeout or non-completed turn as zero points.
2. Scorer: number checks pass within tolerance and fail outside it; contains_any is
   case-insensitive; empty answer scores zero.
3. Compare: exits 1 on each of the 3 regression conditions and 0 within bounds.
4. Refusals: port 8000 and non-loopback hosts refused; reference runner refuses tasks
   without cloud_ok: true.
5. Example tasks: loads example set and reproduces every reference value by independent calculation.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmarks.agent_eval.runner import (  # noqa: E402
    _http_request,
    compare_runs,
    count_thread_tool_calls,
    extract_numbers,
    is_loopback_host,
    run_eval,
    score_task,
    set_claude_runner,
    validate_base_url,
)

EXAMPLE_TASKS_PATH = REPO_ROOT / "scripts" / "benchmarks" / "agent_eval" / "example_tasks.json"


# --- Criterion 1: Stub HTTP server & run execution ----------------------------


class StubBackendHandler(BaseHTTPRequestHandler):
    """Stub HTTP server standing in for the backend thread & interaction API."""

    thread_count = 0
    interactions_received: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len else {}

        if self.path == "/ai/threads":
            StubBackendHandler.thread_count += 1
            thread_id = f"thread-stub-{StubBackendHandler.thread_count}"
            resp = {
                "id": thread_id,
                "workspace_id": body.get("workspace_id", "default"),
                "title": body.get("title"),
                "created_at": "2026-11-24T00:00:00Z",
                "last_activity_at": "2026-11-24T00:00:00Z",
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if "/interactions" in self.path:
            StubBackendHandler.interactions_received.append(body)
            prompt = body.get("prompt", "")

            # Simulate responses based on prompt keywords
            if "timeout" in prompt:
                # Return non-terminal running state to trigger timeout handling
                flow_state = "running"
                assistant_text = ""
                terminal_reason = None
            elif "fail" in prompt:
                flow_state = "failed_terminal"
                assistant_text = "Encountered an internal error"
                terminal_reason = "model_error"
            else:
                flow_state = "completed"
                assistant_text = "The calculated pressure drop is 20000 Pa."
                terminal_reason = None

            resp = {
                "interaction": {
                    "id": "interaction-1",
                    "request_id": body.get("request_id", "req-1"),
                    "interaction_index": 0,
                    "user_text": prompt,
                    "assistant_text": assistant_text,
                    "assistant_text_truncated": False,
                    "flow_id": "flow-1",
                    "persistence_state": "captured",
                    "flow_state": flow_state,
                    "terminal_reason": terminal_reason,
                    "attempt_count": 1,
                    "route_class": "hermes:agent",
                    "model_id": "mock-model",
                    "role_id": "isaac",
                    "created_at": "2026-11-24T00:00:00Z",
                    "updated_at": "2026-11-24T00:00:00Z",
                }
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if "/ai/threads/" in self.path:
            # Poll endpoint for reading thread interactions
            resp = {
                "id": "thread-1",
                "workspace_id": "default",
                "interactions": [
                    {
                        "id": "interaction-1",
                        "request_id": "req-1",
                        "interaction_index": 0,
                        "user_text": "",
                        "assistant_text": "",
                        "assistant_text_truncated": False,
                        "flow_id": "flow-1",
                        "persistence_state": "captured",
                        "flow_state": "running",
                        "terminal_reason": None,
                        "attempt_count": 1,
                        "created_at": "2026-11-24T00:00:00Z",
                        "updated_at": "2026-11-24T00:00:00Z",
                    }
                ],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


def test_run_with_stub_server_and_field_recording(tmp_path: Path) -> None:
    """Criterion 1: With a stub HTTP server, run creates one thread per task and rep,

    records all listed fields, and marks timeout or non-completed turn as zero points.
    """
    server = HTTPServer(("127.0.0.1", 0), StubBackendHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Create dummy data root with an events table containing tool calls
    data_root = tmp_path / "proof_data_root"
    data_root.mkdir()
    db_file = data_root / "jarvisos.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, workspace_id TEXT, event_type TEXT NOT NULL, actor TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT, payload TEXT, created_at TEXT NOT NULL)"
    )
    # Projected agent tool-call events identify the thread through session_ref.
    payload_1 = {
        "event_id": "e-1",
        "kind": "tool.call",
        "session_ref": {"jarvis_thread_id": "thread-stub-1"},
    }
    conn.execute(
        "INSERT INTO events VALUES ('1', 'default', 'hermes.agent_event', 'hermes', 'agent_event', '1', ?, '2026-11-24T00:00:00Z')",
        (json.dumps(payload_1),),
    )
    # A second call for the same thread.
    payload_2 = {
        "event_id": "e-2",
        "kind": "tool.call",
        "session_ref": {"jarvis_thread_id": "thread-stub-1"},
    }
    conn.execute(
        "INSERT INTO events VALUES ('2', 'default', 'hermes.agent_event', 'hermes', 'agent_event', '2', ?, '2026-11-24T00:00:00Z')",
        (json.dumps(payload_2),),
    )
    # Event 3: non-tool event
    payload_3 = {
        "event_id": "e-3",
        "kind": "turn.completed",
        "session_ref": {"jarvis_thread_id": "thread-stub-1"},
    }
    conn.execute(
        "INSERT INTO events VALUES ('3', 'default', 'hermes.agent_event', 'hermes', 'agent_event', '3', ?, '2026-11-24T00:00:00Z')",
        (json.dumps(payload_3),),
    )
    conn.commit()
    conn.close()

    tasks_content = {
        "set_id": "test_stub_set",
        "cloud_ok": True,
        "tasks": [
            {
                "id": "normal_task",
                "prompt": "Calculate pipe pressure drop",
                "reference": 20000.0,
                "max_points": 2.0,
                "checks": [{"kind": "number", "value": 20000.0, "rel_tol": 0.05}],
            },
            {
                "id": "failed_task",
                "prompt": "This will fail turn",
                "reference": 10.0,
                "max_points": 2.0,
                "checks": [{"kind": "number", "value": 10.0, "rel_tol": 0.05}],
            },
            {
                "id": "timeout_task",
                "prompt": "This will timeout turn",
                "reference": 5.0,
                "max_points": 2.0,
                "checks": [{"kind": "number", "value": 5.0, "rel_tol": 0.05}],
            },
        ],
    }
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_content), encoding="utf-8")

    out_dir = tmp_path / "run_out"
    StubBackendHandler.thread_count = 0

    try:
        results = run_eval(
            tasks_path=tasks_file,
            label="test_candidate",
            reps=2,
            base_url=f"http://127.0.0.1:{port}",
            out_dir=out_dir,
            data_root=data_root,
            timeout=1.0,  # Short timeout for fast test
        )

        # 3 tasks * 2 reps = 6 threads created
        assert StubBackendHandler.thread_count == 6
        assert (out_dir / "results.json").is_file()
        assert (out_dir / "test_candidate.json").is_file()

        # Check recorded fields on normal task
        normal_rep0 = results["tasks"]["normal_task"]["repetitions"][0]
        assert normal_rep0["thread_id"] == "thread-stub-1"
        assert normal_rep0["answer"] == "The calculated pressure drop is 20000 Pa."
        assert normal_rep0["flow_state"] == "completed"
        assert normal_rep0["terminal_reason"] is None
        assert normal_rep0["model_id"] == "mock-model"
        assert normal_rep0["role_id"] == "isaac"
        assert normal_rep0["tool_calls"] == 2  # exactly events 1 and 2
        assert normal_rep0["points"] == 2.0
        assert normal_rep0["wall_time_s"] >= 0.0

        # Check failed task scored zero
        failed_rep = results["tasks"]["failed_task"]["repetitions"][0]
        assert failed_rep["flow_state"] == "failed_terminal"
        assert failed_rep["points"] == 0.0

        # Check timeout task scored zero
        timeout_rep = results["tasks"]["timeout_task"]["repetitions"][0]
        assert timeout_rep["flow_state"] == "timed_out"
        assert timeout_rep["terminal_reason"] == "timeout"
        assert timeout_rep["points"] == 0.0

    finally:
        server.shutdown()
        server.server_close()


# --- Criterion 2: Scorer tests ------------------------------------------------


def test_scorer_number_tolerance_and_scale() -> None:
    """Number check passes within tolerance and fails outside it; handles unit scale."""
    task = {
        "max_points": 2.0,
        "checks": [
            {
                "kind": "number",
                "value": 1000.0,
                "rel_tol": 0.05,
                "scale": {"kw": 1000.0, "w": 1.0},
            }
        ],
    }

    # Within tolerance (raw)
    pts, passed, total = score_task("Result is 1020", "completed", task)
    assert passed == 1 and pts == 2.0

    # Outside tolerance (raw)
    pts, passed, total = score_task("Result is 1100", "completed", task)
    assert passed == 0 and pts == 0.0

    # Scale alias: "1.0 kW" -> 1000.0
    pts, passed, total = score_task("Result is 1.0 kW", "completed", task)
    assert passed == 1 and pts == 2.0

    # Number with comma separators: "1,000 W" -> 1000.0
    pts, passed, total = score_task("Result is 1,000 W", "completed", task)
    assert passed == 1 and pts == 2.0


def test_scorer_contains_any_case_insensitive() -> None:
    """contains_any check is case-insensitive."""
    task = {
        "max_points": 2.0,
        "checks": [{"kind": "contains_any", "terms": ["Pascal", "KPA"]}],
    }

    # Matches Pascal with different casing
    pts, passed, _ = score_task("The value is in pascal", "completed", task)
    assert passed == 1 and pts == 2.0

    pts, passed, _ = score_task("Result is 20 kpa", "completed", task)
    assert passed == 1 and pts == 2.0

    pts, passed, _ = score_task("Result is 20 bars", "completed", task)
    assert passed == 0 and pts == 0.0


def test_scorer_empty_answer_and_non_completed_flow_state() -> None:
    """Empty answer or non-completed flow_state scores zero."""
    task = {
        "max_points": 2.0,
        "checks": [{"kind": "number", "value": 50.0, "rel_tol": 0.1}],
    }

    # Empty answer
    pts, passed, _ = score_task("", "completed", task)
    assert pts == 0.0 and passed == 0

    pts, passed, _ = score_task("   ", "completed", task)
    assert pts == 0.0 and passed == 0

    # Non-completed flow_state
    pts, passed, _ = score_task("Answer is 50.0", "running", task)
    assert pts == 0.0 and passed == 0

    pts, passed, _ = score_task("Answer is 50.0", "failed_terminal", task)
    assert pts == 0.0 and passed == 0

    pts, passed, _ = score_task("Answer is 50.0", "timed_out", task)
    assert pts == 0.0 and passed == 0


# --- Criterion 3: Compare regression conditions ------------------------------


def test_compare_exits_zero_when_within_bounds() -> None:
    """compare exits 0 when candidate is within bounds of baseline."""
    baseline = {
        "total_mean_score": 4.0,
        "tasks": {
            "t1": {
                "mean_score": 2.0,
                "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
            "t2": {
                "mean_score": 2.0,
                "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
        },
    }
    candidate = {
        "total_mean_score": 3.8,  # drop is 0.2 (< 0.5)
        "tasks": {
            "t1": {
                "mean_score": 1.9,  # drop 0.1 (< 1.0)
                "min_score": 1.8,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
            "t2": {
                "mean_score": 1.9,  # drop 0.1 (< 1.0)
                "min_score": 1.8,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
        },
    }

    code, report = compare_runs(baseline, candidate)
    assert code == 0
    assert "Result: PASS" in report


def test_compare_condition_1_total_mean_drop_over_half_point() -> None:
    """compare exits 1 when candidate total mean is > 0.5 points below baseline."""
    baseline = {
        "total_mean_score": 4.0,
        "tasks": {
            "t1": {
                "mean_score": 2.0,
                "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
            "t2": {
                "mean_score": 2.0,
                "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
        },
    }
    candidate = {
        "total_mean_score": 3.4,  # drop is 0.6 (> 0.5)
        "tasks": {
            "t1": {
                "mean_score": 1.7,  # drops 0.3
                "min_score": 1.5,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
            "t2": {
                "mean_score": 1.7,  # drops 0.3
                "min_score": 1.5,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
        },
    }

    code, report = compare_runs(baseline, candidate)
    assert code == 1
    assert "Total mean regression" in report


def test_compare_condition_2_task_mean_drop_one_point_or_more() -> None:
    """compare exits 1 when any task's mean drops by 1.0 point or more."""
    baseline = {
        "total_mean_score": 4.0,
        "tasks": {
            "t1": {
                "mean_score": 2.0,
                "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
            "t2": {
                "mean_score": 2.0,
                "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
        },
    }
    candidate = {
        "total_mean_score": 3.6,  # total drop is 0.4 (< 0.5)
        "tasks": {
            "t1": {
                "mean_score": 1.0,  # drop is 1.0 (>= 1.0)
                "min_score": 1.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
            "t2": {
                "mean_score": 2.6,  # gained 0.6
                "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ans"}],
            },
        },
    }

    code, report = compare_runs(baseline, candidate)
    assert code == 1
    assert "Task 't1' mean regression" in report


def test_compare_condition_3_uncompleted_answer_regression() -> None:
    """compare exits 1 when candidate has uncompleted repetition where baseline had none."""
    baseline = {
        "total_mean_score": 2.0,
        "tasks": {
            "t1": {
                "mean_score": 2.0,
                "min_score": 2.0,
                "repetitions": [
                    {"flow_state": "completed", "answer": "ans1"},
                    {"flow_state": "completed", "answer": "ans2"},
                ],
            }
        },
    }
    candidate = {
        "total_mean_score": 1.9,
        "tasks": {
            "t1": {
                "mean_score": 1.9,
                "min_score": 0.0,
                "repetitions": [
                    {"flow_state": "completed", "answer": "ans1"},
                    {"flow_state": "failed_terminal", "answer": ""},  # uncompleted repetition!
                ],
            }
        },
    }

    code, report = compare_runs(baseline, candidate)
    assert code == 1
    assert "Uncompleted answer regression" in report


# --- Criterion 4: Port 8000, loopback, and cloud_ok refusals ------------------


def test_refuse_port_8000() -> None:
    """validate_base_url refuses port 8000."""
    with pytest.raises(ValueError, match="Port 8000 is refused"):
        validate_base_url("http://127.0.0.1:8000")

    with pytest.raises(ValueError, match="Port 8000 is refused"):
        validate_base_url("http://localhost:8000")


def test_refuse_non_loopback_hosts() -> None:
    """validate_base_url refuses non-loopback hosts."""
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("127.0.1.1")
    assert not is_loopback_host("192.168.1.50")
    assert not is_loopback_host("example.com")
    assert not is_loopback_host("8.8.8.8")

    with pytest.raises(ValueError, match="not a loopback interface"):
        validate_base_url("http://example.com:8001")

    with pytest.raises(ValueError, match="not a loopback interface"):
        validate_base_url("http://192.168.1.50:8001")


def test_reference_runner_refuses_cloud_not_ok(tmp_path: Path) -> None:
    """--reference refuses a task file without cloud_ok: true."""
    tasks_content = {
        "set_id": "private_tasks",
        "cloud_ok": False,
        "tasks": [{"id": "secret_task", "prompt": "calc", "checks": []}],
    }
    tasks_file = tmp_path / "private_tasks.json"
    tasks_file.write_text(json.dumps(tasks_content), encoding="utf-8")

    with pytest.raises(ValueError, match="Reference runner requires cloud_ok: true"):
        run_eval(
            tasks_path=tasks_file,
            label="test_ref",
            out_dir=tmp_path / "out",
            reference="haiku",
        )


def test_reference_runner_with_injected_claude_runner(tmp_path: Path) -> None:
    """Reference runner works with injected Claude runner when cloud_ok: true."""
    tasks_content = {
        "set_id": "cloud_tasks",
        "cloud_ok": True,
        "tasks": [
            {
                "id": "pipe_calc",
                "prompt": "Compute pressure",
                "reference": 20000.0,
                "checks": [{"kind": "number", "value": 20000.0, "rel_tol": 0.05}],
            }
        ],
    }
    tasks_file = tmp_path / "cloud_tasks.json"
    tasks_file.write_text(json.dumps(tasks_content), encoding="utf-8")

    def mock_claude(prompt: str, model: str) -> tuple[str, str, str | None]:
        assert model == "haiku"
        return "The pressure drop is 20000 Pa.", "completed", None

    set_claude_runner(mock_claude)
    try:
        out_dir = tmp_path / "ref_out"
        res = run_eval(
            tasks_path=tasks_file,
            label="haiku_baseline",
            reps=2,
            out_dir=out_dir,
            reference="haiku",
        )
        assert res["total_mean_score"] == 2.0
        assert res["tasks"]["pipe_calc"]["mean_score"] == 2.0
    finally:
        set_claude_runner(None)


# --- Criterion 5: Example task set & independent reproduction ----------------


def test_example_tasks_load_and_reproduce_reference_values() -> None:
    """Criterion 5: Example task set loads and every reference value is

    reproduced by an independent calculation in the test.
    """
    assert EXAMPLE_TASKS_PATH.is_file(), f"example_tasks.json not found at {EXAMPLE_TASKS_PATH}"
    data = json.loads(EXAMPLE_TASKS_PATH.read_text(encoding="utf-8"))
    assert data.get("cloud_ok") is True
    tasks = {t["id"]: t for t in data.get("tasks", [])}
    assert len(tasks) >= 6

    # 1. Pipe flow pressure drop: Delta P = f * (L/D) * (rho * v^2 / 2)
    f = 0.02
    length = 50.0
    diameter = 0.1
    rho = 1000.0
    v = 2.0
    expected_delta_p = f * (length / diameter) * (rho * (v**2) / 2.0)
    assert expected_delta_p == 20000.0
    assert tasks["pipe_flow_pressure_drop"]["reference"] == expected_delta_p
    assert tasks["pipe_flow_pressure_drop"]["checks"][0]["value"] == expected_delta_p

    # 2. Convective heat transfer: Q = h * A * (Ts - Tinf)
    h = 25.0
    area = 2.0
    t_s = 80.0
    t_inf = 20.0
    expected_q = h * area * (t_s - t_inf)
    assert expected_q == 3000.0
    assert tasks["convective_heat_transfer"]["reference"] == expected_q
    assert tasks["convective_heat_transfer"]["checks"][0]["value"] == expected_q

    # 3. Linear curve fit: (1,3), (2,5), (3,7), (4,9) -> y(5)
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [3.0, 5.0, 7.0, 9.0]
    # slope m = (9 - 3) / (4 - 1) = 2.0, intercept b = 3 - 2*1 = 1.0
    slope = (ys[-1] - ys[0]) / (xs[-1] - xs[0])
    intercept = ys[0] - slope * xs[0]
    expected_y_5 = slope * 5.0 + intercept
    assert slope == 2.0
    assert expected_y_5 == 11.0
    assert tasks["linear_curve_fit_extrapolation"]["reference"] == expected_y_5
    assert tasks["linear_curve_fit_extrapolation"]["checks"][0]["value"] == expected_y_5
    assert tasks["linear_curve_fit_extrapolation"]["checks"][1]["value"] == slope

    # 4. Unit conversion: 60 mph to m/s (1 mi = 1609.344 m, 1 hr = 3600 s)
    expected_mps = 60.0 * 1609.344 / 3600.0
    assert expected_mps == 26.8224
    assert tasks["unit_conversion_chain"]["reference"] == expected_mps
    assert tasks["unit_conversion_chain"]["checks"][0]["value"] == expected_mps

    # 5. Mass flow & kinetic energy rate:
    # m_dot = rho * V * A, E_dot = 0.5 * m_dot * V^2
    rho_air = 1.2
    vel = 10.0
    area_duct = 0.5
    expected_mdot = rho_air * vel * area_duct
    expected_edot = 0.5 * expected_mdot * (vel**2)
    assert expected_mdot == 6.0
    assert expected_edot == 300.0
    assert tasks["mass_flow_and_kinetic_rate"]["reference"] == expected_edot
    assert tasks["mass_flow_and_kinetic_rate"]["checks"][0]["value"] == expected_mdot
    assert tasks["mass_flow_and_kinetic_rate"]["checks"][1]["value"] == expected_edot

    # 6. Uncertain gas pressure: P = n*R*T / V for T in [300, 350]
    n_moles = 2.0
    r_const = 8.314
    vol = 0.05
    p_min = n_moles * r_const * 300.0 / vol
    p_max = n_moles * r_const * 350.0 / vol
    assert math.isclose(p_min, 99768.0, rel_tol=1e-5)
    assert math.isclose(p_max, 116396.0, rel_tol=1e-5)
    ref = tasks["uncertain_gas_pressure_range"]["reference"]
    assert math.isclose(ref["min"], p_min, rel_tol=1e-5)
    assert math.isclose(ref["max"], p_max, rel_tol=1e-5)
    assert math.isclose(tasks["uncertain_gas_pressure_range"]["checks"][0]["value"], p_min, rel_tol=1e-5)
    assert math.isclose(tasks["uncertain_gas_pressure_range"]["checks"][1]["value"], p_max, rel_tol=1e-5)


def test_extract_numbers_utility() -> None:
    """extract_numbers correctly parses integers, floats, scientific notation, and comma thousands."""
    nums = extract_numbers("Value is 20,000 Pa or 2.5e4 or -12.34 and 0")
    assert nums == [20000.0, 25000.0, -12.34, 0.0]


def test_tool_count_uses_broker_results_for_exact_thread(tmp_path: Path) -> None:
    db = tmp_path / "jarvisos.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE events (event_type TEXT, payload TEXT)")
        for event_type, thread_id, kind in (
            ("hermes.agent_event", "thread-a", "tool.call"),
            ("hermes.agent_event", "thread-b", "tool.call"),
            ("hermes.agent_event", "thread-a", "turn.completed"),
        ):
            connection.execute(
                "INSERT INTO events VALUES (?, ?)",
                (event_type, json.dumps({"session_ref": {"jarvis_thread_id": thread_id}, "kind": kind})),
            )
    assert count_thread_tool_calls(tmp_path, "thread-a") == 1


def test_http_request_uses_remaining_task_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    seen: list[float] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(_request: object, timeout: float) -> Response:
        seen.append(timeout)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    deadline = time.monotonic() + 0.2
    assert _http_request("http://127.0.0.1:1234", deadline=deadline) == {}
    assert 0 < seen[0] <= 0.2
    with pytest.raises(TimeoutError):
        _http_request("http://127.0.0.1:1234", deadline=time.monotonic() - 1)
    assert len(seen) == 1


def test_run_marks_stalled_submit_as_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from scripts.benchmarks.agent_eval import runner

    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({
        "set_id": "deadline", "cloud_ok": False,
        "tasks": [{"id": "one", "prompt": "Calculate 2 + 2", "reference": 4,
                   "max_points": 2, "checks": [{"kind": "number", "value": 4, "rel_tol": 0}]},
                  ],
    }), encoding="utf-8")
    deadlines: list[float] = []

    def fake_http(_url: str, method: str = "GET", payload: dict[str, Any] | None = None,
                  *, deadline: float) -> dict[str, Any]:
        deadlines.append(deadline)
        if len(deadlines) == 1:
            return {"id": "thread-one"}
        raise TimeoutError("submit stalled")

    monkeypatch.setattr(runner, "_http_request", fake_http)
    before = time.monotonic()
    result = run_eval(tasks_path=tasks, label="deadline", reps=1,
                      base_url="http://127.0.0.1:8123", out_dir=tmp_path / "out", timeout=0.2,
                      route_class="local:fake")
    rep = result["tasks"]["one"]["repetitions"][0]
    assert rep["flow_state"] == "timed_out"
    assert rep["terminal_reason"] == "timeout"
    assert rep["points"] == 0
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert before < deadlines[0] <= before + 0.3


def test_compare_rejects_different_task_sets() -> None:
    run = {"set_id": "a", "total_mean_score": 2.0, "tasks": {
        "one": {"mean_score": 2.0, "min_score": 2.0,
                "repetitions": [{"flow_state": "completed", "answer": "ok"}]},
    }}
    with pytest.raises(ValueError, match="task set ids"):
        compare_runs(run, {**run, "set_id": "b"})
    with pytest.raises(ValueError, match="task ids"):
        compare_runs(run, {**run, "tasks": {"two": run["tasks"]["one"]}})
    with pytest.raises(ValueError, match="does not match task means"):
        compare_runs(run, {**run, "total_mean_score": 0.0})
