#!/usr/bin/env python3
"""Readiness board status line and summary table (spec 303).

Prints the seven-step literature readiness board against the active data root and pack.
Output format: a table of cases and steps followed by:
    n/m green, first red: <case> <step>
Exits 0. When no cases are configured, prints a plain message and exits 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    import fastapi  # noqa: F401
except ImportError:
    import os

    venv_py = BACKEND_ROOT / ".venv" / "bin" / "python"
    if venv_py.is_file() and sys.prefix == sys.base_prefix:
        os.execv(str(venv_py), [str(venv_py)] + sys.argv)

from app.modules.engineering.readiness_board import (
    STEPS,
    compute_readiness_board,
)


def format_table(cases: list) -> str:
    if not cases:
        return ""
    headers = ["Case"] + [step.capitalize() for step in STEPS]
    col_widths = [len(h) for h in headers]
    for c in cases:
        col_widths[0] = max(col_widths[0], len(c.id))
        for idx, step_name in enumerate(STEPS, start=1):
            st = c.steps[step_name].status
            if step_name == "compare":
                cmp = c.steps["compare"].comparison
                if cmp is not None and cmp.deviation is not None:
                    st = f"{st} ({cmp.deviation * 100:.1f}%)"
            col_widths[idx] = max(col_widths[idx], len(st))

    header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    separator_line = "  ".join("-" * col_widths[i] for i in range(len(headers)))
    rows = [header_line, separator_line]

    for c in cases:
        row_vals = [c.id.ljust(col_widths[0])]
        for idx, step_name in enumerate(STEPS, start=1):
            st = c.steps[step_name].status
            if step_name == "compare":
                cmp = c.steps["compare"].comparison
                if cmp is not None and cmp.deviation is not None:
                    st = f"{st} ({cmp.deviation * 100:.1f}%)"
            row_vals.append(st.ljust(col_widths[idx]))
        rows.append("  ".join(row_vals))

    return "\n".join(rows)


def main() -> int:
    board = compute_readiness_board()
    if not board.cases:
        print("No readiness cases configured.")
        return 0

    table = format_table(board.cases)
    print(table)
    print()

    summary = board.summary
    if summary.first_red:
        print(f"{summary.green_cells}/{summary.total_cells} green, first red: {summary.first_red}")
    else:
        print(f"{summary.green_cells}/{summary.total_cells} green")

    if board.errors:
        for err in board.errors:
            prefix = f"[{err.case_id}] " if err.case_id else ""
            print(f"Warning: {prefix}{err.message}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
