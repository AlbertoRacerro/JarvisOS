from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "STATUS.md"


def test_grade_status_registry_lists_backend_slices() -> None:
    text = STATUS_PATH.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| 062 |"))
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    assert cells[1] == "blocked"
    assert cells[2] == "—"
    assert "Backend PRs #166 and #167 remain merged evidence" in row
    assert "rejected permanent per-response grading in normal Jarvis chat" in row
    assert "separately re-derived as secondary Evaluation/Audit UI" in row
    assert "does not block the operator-workstation queue" in row
