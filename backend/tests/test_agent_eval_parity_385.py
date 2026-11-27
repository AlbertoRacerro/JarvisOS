"""Tests for Spec 385 — AGENT-EVAL-PARITY-1.

Three-arm parity reporting, Gate Zero alarm, per-category scoring, and atomic scoreboard writer.
All tests run offline without network access or GPU interaction.
"""

from __future__ import annotations

import io
import json
import math
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import vault_guard  # noqa: E402
from scripts.benchmarks.agent_eval.runner import (  # noqa: E402
    _compute_ratio_stats,
    format_parity_table,
    main,
    parity_report,
    run_eval,
    run_summary,
    set_claude_runner,
    update_scoreboard,
)


def make_run_data(
    tasks_scores: dict[str, tuple[float, str]],
    *,
    set_id: str = "test_set",
    split: str = "dev",
    reps: int = 2,
    label: str = "arm",
    tasks_sha256: str = "sha_abc123",
    complete: bool = True,
) -> dict[str, Any]:
    """Create synthetic completed run results for offline comparator testing."""
    tasks_dict: dict[str, Any] = {}
    for tid, (score, cat) in tasks_scores.items():
        reps_list = [
            {
                "rep": r,
                "points": float(score),
                "max_points": 2.0,
                "flow_state": "completed",
                "answer": f"Answer for {tid}",
                "tool_calls": 0,
                "wall_time_s": 0.1,
                "yield_wait_s": 0.0,
            }
            for r in range(reps)
        ]
        tasks_dict[tid] = {
            "task_id": tid,
            "category": cat,
            "mean_score": float(score),
            "min_score": float(score),
            "passes": reps if score >= 2.0 else 0,
            "n": reps,
            "pass_at_1": 1.0 if score >= 2.0 else 0.0,
            "pass_at_k": 1.0 if score >= 2.0 else 0.0,
            "mean_tool_calls": 0.0,
            "total_tool_calls": 0,
            "mean_wall_time_s": 0.1,
            "total_wall_time_s": 0.1 * reps,
            "repetitions": reps_list,
        }
    entries = list(tasks_dict.values())
    cat_names = sorted({t["category"] for t in entries})
    category_summary = {
        cat: run_summary({tid: t for tid, t in tasks_dict.items() if t["category"] == cat})
        for cat in cat_names
    }
    return {
        "label": label,
        "set_id": set_id,
        "split": split,
        "reps": reps,
        "task_ids": list(tasks_scores.keys()),
        "tasks_sha256": tasks_sha256,
        "complete": complete,
        "completed_repetitions": len(entries) * reps,
        "timestamp": "2026-11-27T00:00:00Z",
        "total_mean_score": round(sum(t["mean_score"] for t in entries), 4),
        "total_tool_calls": 0,
        "total_wall_time_s": round(sum(t["total_wall_time_s"] for t in entries), 3),
        "yield_pauses": 0,
        "yield_paused_s": 0.0,
        "summary": run_summary(tasks_dict),
        "category_summary": category_summary,
        "tasks": tasks_dict,
    }


def write_run_file(path: Path, run_data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run_data, indent=2), encoding="utf-8")
    return path


# --- Acceptance Criterion 1: Per-category run_eval and validation -------------


def test_run_eval_validates_category_and_stores_summary(tmp_path: Path) -> None:
    """run_eval stores category, emits category_summary, and rejects non-empty string violations."""
    tasks_content = {
        "set_id": "cat_test_set",
        "cloud_ok": True,
        "tasks": [
            {
                "id": "t1",
                "prompt": "prompt 1",
                "category": "calculation",
                "checks": [{"kind": "number", "value": 42.0}],
            },
            {
                "id": "t2",
                "prompt": "prompt 2",
                "category": "literature",
                "checks": [{"kind": "number", "value": 42.0}],
            },
            {
                "id": "t3",
                "prompt": "prompt 3",
                # category absent -> defaults to "uncategorized"
                "checks": [{"kind": "number", "value": 42.0}],
            },
        ],
    }
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_content), encoding="utf-8")

    def mock_claude(prompt: str, model: str) -> tuple[str, str, str | None]:
        return "42.0", "completed", None

    set_claude_runner(mock_claude)
    try:
        out_dir = tmp_path / "out"
        res = run_eval(
            tasks_path=tasks_file,
            label="cat_run",
            reps=2,
            out_dir=out_dir,
            reference="haiku",
        )
        assert res["tasks"]["t1"]["category"] == "calculation"
        assert res["tasks"]["t2"]["category"] == "literature"
        assert res["tasks"]["t3"]["category"] == "uncategorized"

        assert "category_summary" in res
        cats = list(res["category_summary"].keys())
        assert cats == ["calculation", "literature", "uncategorized"]
        for cat in cats:
            assert res["category_summary"][cat]["tasks"] == 1
            assert res["category_summary"][cat]["mean_points"] == 2.0
    finally:
        set_claude_runner(None)


@pytest.mark.parametrize("invalid_category", ["", "   ", 123, None, ["calc"]])
def test_run_eval_rejects_invalid_categories(tmp_path: Path, invalid_category: Any) -> None:
    """category must be a non-empty string when present."""
    tasks_content = {
        "set_id": "invalid_cat_set",
        "cloud_ok": True,
        "tasks": [
            {
                "id": "t1",
                "prompt": "prompt",
                "category": invalid_category,
                "checks": [{"kind": "number", "value": 42.0}],
            }
        ],
    }
    tasks_file = tmp_path / "invalid_tasks.json"
    tasks_file.write_text(json.dumps(tasks_content), encoding="utf-8")

    with pytest.raises(ValueError, match="category must be a non-empty string"):
        run_eval(
            tasks_path=tasks_file,
            label="invalid_cat",
            reps=1,
            out_dir=tmp_path / "out",
            reference="haiku",
        )


# --- Acceptance Criterion 2: Legacy results backwards compatibility ----------


def test_parity_report_handles_legacy_results_without_category() -> None:
    """Legacy results.json lacking category or category_summary are treated as uncategorized."""
    a_data = make_run_data({"t1": (2.0, "calculation"), "t2": (1.5, "literature")}, label="arm_a")
    # Strip category from tasks to simulate legacy file
    del a_data["tasks"]["t1"]["category"]
    del a_data["tasks"]["t2"]["category"]
    del a_data["category_summary"]

    b_data = make_run_data({"t1": (2.0, "calculation"), "t2": (1.5, "literature")}, label="arm_b")
    del b_data["tasks"]["t1"]["category"]
    del b_data["tasks"]["t2"]["category"]
    del b_data["category_summary"]

    report = parity_report(a_data, b_data)
    assert "uncategorized" in report["categories"]
    assert report["categories"]["uncategorized"]["tasks"] == 2
    assert report["gate_zero_alarm"] is False


# --- Acceptance Criterion 3: Gate Zero Alarm and Worst-First Diff Ranking -----


def test_gate_zero_alarm_triggers_on_regression_and_ranks_worst_first() -> None:
    """Gate Zero alarm is True when B < A in any category or overall, and diffs are sorted worst first."""
    # A has scores: calc=2.0, lit=2.0
    a_scores = {"t1": (2.0, "calc"), "t2": (2.0, "lit"), "t3": (1.0, "lit")}
    # B regresses severely on t1 (-1.5), improves on t3 (+1.0)
    b_scores = {"t1": (0.5, "calc"), "t2": (2.0, "lit"), "t3": (2.0, "lit")}

    a_data = make_run_data(a_scores, label="arm_a")
    b_data = make_run_data(b_scores, label="arm_b")

    report = parity_report(a_data, b_data)
    assert report["gate_zero_alarm"] is True
    assert report["categories"]["calc"]["gate_zero_alarm"] is True
    assert report["categories"]["lit"]["gate_zero_alarm"] is False

    worst = report["worst_b_minus_a_tasks"]
    assert len(worst) == 3
    # Worst diff is t1 (0.5 - 2.0 = -1.5)
    assert worst[0]["task_id"] == "t1"
    assert worst[0]["diff"] == -1.5
    # Next is t2 (2.0 - 2.0 = 0.0)
    assert worst[1]["task_id"] == "t2"
    assert worst[1]["diff"] == 0.0
    # Best is t3 (2.0 - 1.0 = +1.0)
    assert worst[2]["task_id"] == "t3"
    assert worst[2]["diff"] == 1.0


def test_gate_zero_passes_when_b_greater_than_or_equal_to_a() -> None:
    """Gate Zero alarm is False when B >= A overall and in every category."""
    a_scores = {"t1": (1.0, "calc"), "t2": (1.5, "lit")}
    b_scores = {"t1": (1.5, "calc"), "t2": (1.5, "lit")}

    a_data = make_run_data(a_scores, label="arm_a")
    b_data = make_run_data(b_scores, label="arm_b")

    report = parity_report(a_data, b_data)
    assert report["gate_zero_alarm"] is False
    assert report["categories"]["calc"]["gate_zero_alarm"] is False
    assert report["categories"]["lit"]["gate_zero_alarm"] is False


# --- Acceptance Criterion 4: Delta-Method SE, 95% CI, Parity Met, Zero Safety -


def test_delta_method_ratio_and_confidence_interval() -> None:
    """Ratio, delta-method SE, and 95% CI match analytical formula."""
    num_mean, num_se = 2.0, 0.1
    den_mean, den_se = 2.0, 0.1
    stats = _compute_ratio_stats(num_mean, num_se, den_mean, den_se)

    expected_ratio = 1.0
    expected_se = 1.0 * math.sqrt((0.1 / 2.0) ** 2 + (0.1 / 2.0) ** 2)
    expected_low = expected_ratio - 1.96 * expected_se
    expected_high = expected_ratio + 1.96 * expected_se

    assert stats["ratio"] == pytest.approx(expected_ratio, abs=1e-4)
    assert stats["se"] == pytest.approx(expected_se, abs=1e-4)
    assert stats["ci_low"] == pytest.approx(expected_low, abs=1e-4)
    assert stats["ci_high"] == pytest.approx(expected_high, abs=1e-4)
    assert stats["ci_includes_one"] is True


def test_parity_met_requires_all_categories_ci_to_include_one() -> None:
    """parity_met is True iff arm_c is provided and every category CI spans 1.0."""
    # A and B are 2.0 across calc and lit
    a_scores = {"t1": (2.0, "calc"), "t2": (2.0, "calc"), "t3": (2.0, "lit"), "t4": (2.0, "lit")}
    b_scores = {"t1": (2.0, "calc"), "t2": (2.0, "calc"), "t3": (2.0, "lit"), "t4": (2.0, "lit")}

    # Case 1: Arm C matches on both categories
    c_scores_pass = {"t1": (2.0, "calc"), "t2": (2.0, "calc"), "t3": (2.0, "lit"), "t4": (2.0, "lit")}
    report_pass = parity_report(
        make_run_data(a_scores), make_run_data(b_scores), make_run_data(c_scores_pass)
    )
    assert report_pass["parity_met"] is True
    assert report_pass["categories"]["calc"]["c_over_max_ab"]["ci_includes_one"] is True
    assert report_pass["categories"]["lit"]["c_over_max_ab"]["ci_includes_one"] is True

    # Case 2: Arm C severely underperforms in calc (0.2 vs 2.0)
    c_scores_fail = {"t1": (0.2, "calc"), "t2": (0.2, "calc"), "t3": (2.0, "lit"), "t4": (2.0, "lit")}
    report_fail = parity_report(
        make_run_data(a_scores), make_run_data(b_scores), make_run_data(c_scores_fail)
    )
    assert report_fail["parity_met"] is False
    assert report_fail["categories"]["calc"]["c_over_max_ab"]["ci_includes_one"] is False
    assert report_fail["categories"]["lit"]["c_over_max_ab"]["ci_includes_one"] is True

    # Case 3: Arm C omitted -> parity_met is False
    report_no_c = parity_report(make_run_data(a_scores), make_run_data(b_scores))
    assert report_no_c["parity_met"] is False
    assert report_no_c["arm_c"] is None


def test_zero_denominator_and_zero_numerator_safety() -> None:
    """Zero denominator produces None ratio and False CI inclusion without raising exceptions."""
    # Denominator <= 0
    den_zero = _compute_ratio_stats(2.0, 0.1, 0.0, 0.0)
    assert den_zero["ratio"] is None
    assert den_zero["se"] is None
    assert den_zero["ci_low"] is None
    assert den_zero["ci_high"] is None
    assert den_zero["ci_includes_one"] is False

    # Negative denominator
    den_neg = _compute_ratio_stats(2.0, 0.1, -1.0, 0.1)
    assert den_neg["ratio"] is None
    assert den_neg["ci_includes_one"] is False

    # Zero numerator with positive denominator
    num_zero = _compute_ratio_stats(0.0, 0.0, 2.0, 0.1)
    assert num_zero["ratio"] == 0.0
    assert num_zero["se"] is None
    assert num_zero["ci_includes_one"] is False


# --- Acceptance Criterion 5: Atomic Plain-Italian Scoreboard Writer -----------


def test_update_scoreboard_creates_table_and_appends_rows(tmp_path: Path) -> None:
    """update_scoreboard creates Markdown table with headers and appends subsequent rows atomically."""
    board_file = tmp_path / "SCOREBOARD.md"
    a_scores = {"t1": (2.0, "calc"), "t2": (2.0, "lit")}
    b_scores = {"t1": (2.0, "calc"), "t2": (2.0, "lit")}
    c_scores = {"t1": (1.9, "calc"), "t2": (1.8, "lit")}

    report1 = parity_report(
        make_run_data(a_scores), make_run_data(b_scores), make_run_data(c_scores)
    )

    # First update: creates header + first row
    update_scoreboard(board_file, report1, date="2026-11-25", change_label="baseline")
    assert board_file.is_file()
    text1 = board_file.read_text(encoding="utf-8")
    lines1 = [line.strip() for line in text1.splitlines() if line.strip()]
    assert len(lines1) == 3
    assert lines1[0].startswith("| Data | Modifica | Split | Gate Zero (B/A) |")
    assert lines1[0].endswith("| Totale |")
    assert "calc" in lines1[0] and "lit" in lines1[0]
    assert "2026-11-25 | baseline | dev | PASS" in lines1[2]

    # Second update without arm_c: appends row with n/d for ratios
    report2 = parity_report(make_run_data(a_scores), make_run_data(b_scores))
    update_scoreboard(board_file, report2, date="2026-11-26", change_label="no-c-arm")
    text2 = board_file.read_text(encoding="utf-8")
    lines2 = [line.strip() for line in text2.splitlines() if line.strip()]
    assert len(lines2) == 4
    # The first row is preserved
    assert lines2[2] == lines1[2]
    # The new row has n/d
    assert "2026-11-26 | no-c-arm | dev | PASS" in lines2[3]
    assert "n/d" in lines2[3]


# --- Acceptance Criterion 6: Vault Guard Refusal ------------------------------


def test_vault_guard_refusal_on_parity_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every path flag on parity (--arm-a, --arm-b, --arm-c, --json, --scoreboard) refuses vault paths with exit 3."""
    vault_dir = tmp_path / "secret_vault"
    vault_dir.mkdir()
    monkeypatch.setenv(vault_guard.VAULT_PATH_ENV, str(vault_dir))

    valid_file = write_run_file(tmp_path / "valid.json", make_run_data({"t1": (2.0, "calc")}))
    vault_file = str(vault_dir / "target.json")

    flags = [
        ["--arm-a", vault_file, "--arm-b", str(valid_file)],
        ["--arm-a", str(valid_file), "--arm-b", vault_file],
        ["--arm-a", str(valid_file), "--arm-b", str(valid_file), "--arm-c", vault_file],
        ["--arm-a", str(valid_file), "--arm-b", str(valid_file), "--json", vault_file],
        ["--arm-a", str(valid_file), "--arm-b", str(valid_file), "--scoreboard", str(vault_dir / "board.md")],
    ]

    for flag_args in flags:
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            code = main(["parity", *flag_args])
        assert code == vault_guard.REFUSED_EXIT
        assert vault_guard.REFUSED_MESSAGE in stderr_buf.getvalue()

    # Programmatic update_scoreboard and parity_report also refuse vault paths
    with pytest.raises(PermissionError, match=vault_guard.REFUSED_MESSAGE):
        update_scoreboard(vault_dir / "board.md", make_run_data({"t1": (2.0, "calc")}))

    with pytest.raises(PermissionError, match=vault_guard.REFUSED_MESSAGE):
        parity_report(vault_file, valid_file)


# --- Acceptance Criterion 7: Comparability and Drift --------------------------


def test_parity_comparability_and_drift_checks(tmp_path: Path) -> None:
    """Drift in set_id, tasks_sha256, or task_ids raises ValueError unless --allow-task-drift."""
    a_data = make_run_data({"t1": (2.0, "calc"), "t2": (2.0, "lit")}, set_id="setA", tasks_sha256="shaA")
    b_data = make_run_data({"t1": (2.0, "calc"), "t3": (2.0, "lit")}, set_id="setB", tasks_sha256="shaB")

    with pytest.raises(ValueError, match="Runs are not comparable"):
        parity_report(a_data, b_data)

    # With allow_task_drift=True, common task t1 is compared
    rep = parity_report(a_data, b_data, allow_task_drift=True)
    assert rep["task_ids"] == ["t1"]
    assert rep["tasks_count"] == 1


def test_incomplete_run_refusal() -> None:
    """Incomplete run raises ValueError."""
    a_data = make_run_data({"t1": (2.0, "calc")}, complete=False)
    b_data = make_run_data({"t1": (2.0, "calc")})
    with pytest.raises(ValueError, match="incomplete"):
        parity_report(a_data, b_data)


# --- Acceptance Criterion 8: CLI Parity Execution End-to-End -----------------


def test_parity_cli_end_to_end(tmp_path: Path) -> None:
    """agent_eval parity CLI writes json, formats table, and updates scoreboard."""
    a_scores = {"t1": (2.0, "calc"), "t2": (2.0, "lit")}
    b_scores = {"t1": (2.0, "calc"), "t2": (2.0, "lit")}
    c_scores = {"t1": (2.0, "calc"), "t2": (2.0, "lit")}

    a_path = write_run_file(tmp_path / "a.json", make_run_data(a_scores, label="arm_a"))
    b_path = write_run_file(tmp_path / "b.json", make_run_data(b_scores, label="arm_b"))
    c_path = write_run_file(tmp_path / "c.json", make_run_data(c_scores, label="arm_c"))
    json_out = tmp_path / "parity_result.json"
    sb_out = tmp_path / "scoreboard.md"

    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf):
        code = main([
            "parity",
            "--arm-a", str(a_path),
            "--arm-b", str(b_path),
            "--arm-c", str(c_path),
            "--json", str(json_out),
            "--scoreboard", str(sb_out),
            "--change-label", "v1.0",
            "--date", "2026-11-27",
        ])

    assert code == 0
    table_output = stdout_buf.getvalue()
    assert "AGENT EVAL PARITY REPORT" in table_output
    assert "Gate Zero: PASS" in table_output
    assert "PARITY MET" in table_output

    assert json_out.is_file()
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["parity_met"] is True
    assert payload["gate_zero_alarm"] is False

    # Direct format_parity_table check
    rendered = format_parity_table(payload)
    assert "AGENT EVAL PARITY REPORT" in rendered
    assert "PARITY MET" in rendered

    assert sb_out.is_file()
    sb_text = sb_out.read_text(encoding="utf-8")
    assert "v1.0" in sb_text
    assert "PASS" in sb_text
