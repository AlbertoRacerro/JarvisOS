#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys
import time


def _block(name: str, components: list[str], records: dict[int, list[float]]) -> list[str]:
    lines = [f" -4  {name:<8} {len(components):4d}    1"]
    for index, component in enumerate(components, start=1):
        lines.append(f" -5  {component:<8}    1    2 {index:4d}    0")
    for node_id, values in records.items():
        lines.append(" -1" + f"{node_id:10d}" + "".join(f"{value:12.5E}" for value in values))
    lines.append(" -3")
    return lines


job = sys.argv[1]
cwd = pathlib.Path.cwd()
mode = cwd.name
if "timeout" in mode:
    time.sleep(5)
if "solve_error" in mode:
    print("*ERROR fake solver failure")
    sys.exit(3)
if "diverged" in mode:
    print("non convergence detected")
    sys.exit(2)
if "parse_error" not in mode:
    frd_lines = []
    frd_lines.extend(_block("DISP", ["D1", "D2", "D3"], {1: [0.0, 0.0, 0.0], 2: [3.0, 4.0, 0.0]}))
    frd_lines.extend(
        _block(
            "STRESS",
            ["SXX", "SYY", "SZZ", "SXY", "SYZ", "SZX"],
            {1: [12.5, 0.0, 0.0, 0.0, 0.0, 0.0], 2: [275.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        )
    )
    (cwd / f"{job}.frd").write_text("\n".join(frd_lines) + "\n", encoding="utf-8")
else:
    (cwd / f"{job}.frd").write_text(" -4  DISP        3    1\n -3\n", encoding="utf-8")
(cwd / f"{job}.dat").write_text("REACTION 1 -10 0 0\n", encoding="utf-8")
print("fake ccx completed")
