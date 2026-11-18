"""Read-only view of a workspace's bench runs for the Runs screen (spec 299).

Reuses `show_run` and `compare_runs` with the workspace bench root as `root`; nothing here writes, and no
payload carries a filesystem path.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.modules.engineering.bench import BenchInputError, _agent_view, compare_runs, show_run
from app.modules.engineering.operator_service import SAFE_ID, workspace_dir

MAX_LISTED_RUNS = 100
VERDICTS = frozenset({"usable", "usable_with_warnings", "unchecked", "not_usable"})


class BenchViewError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def bench_root(workspace_id: str) -> Path:
    """The workspace bench root, resolved but never created."""
    try:
        studies = workspace_dir(workspace_id)
    except LookupError as exc:
        raise BenchViewError(404, "workspace_not_found") from exc
    return (studies.parent / "bench").resolve()


def _verdict(value: Any) -> str:
    return value if isinstance(value, str) and value in VERDICTS else "unchecked"


def _run_dir(root: Path, run_id: str) -> Path:
    if not SAFE_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise BenchViewError(422, "run_id_invalid")
    path = (root / run_id).resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise BenchViewError(404, "run_not_found")
    return path


def _when(data: dict[str, Any], result_file: Path) -> datetime:
    stored = data.get("completed_at")
    if isinstance(stored, str):
        try:
            parsed = datetime.fromisoformat(stored)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(result_file.stat().st_mtime, UTC)


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("boolean is not a coordinate")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _read_ports(run_dir: Path) -> list[dict[str, Any]]:
    manifest_path = run_dir / "artifacts" / "manifest.json"
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return []
        resolved_run = run_dir.resolve()
        resolved_manifest = manifest_path.resolve()
        if not resolved_manifest.is_relative_to(resolved_run):
            return []
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    resolved_ports = data.get("resolved_ports")
    if not isinstance(resolved_ports, dict):
        return []

    body = resolved_ports.get("body")
    if isinstance(body, dict):
        port_groups = [body]
    else:
        port_groups = [
            ports
            for _, ports in sorted(resolved_ports.items(), key=lambda item: str(item[0]))
            if isinstance(ports, dict)
        ]

    ports: list[dict[str, Any]] = []
    for port_group in port_groups:
        for port_name, port in port_group.items():
            if not isinstance(port, dict):
                continue
            origin = port.get("origin")
            direction = port.get("direction")
            if not isinstance(origin, list) or not isinstance(direction, list):
                continue
            try:
                entry: dict[str, Any] = {
                    "name": str(port_name),
                    "interface": str(port.get("interface", "")),
                    "origin": [_finite(value) for value in origin],
                    "direction": [_finite(value) for value in direction],
                }
                outer_d = port.get("outer_d")
                if outer_d is not None:
                    entry["outer_d"] = _finite(outer_d)
                    entry["wall_t"] = _finite(port.get("wall_t", 0.0))
                pad_d = port.get("pad_d")
                if pad_d is not None:
                    entry["pad_d"] = _finite(pad_d)
            except (TypeError, ValueError, OverflowError):
                continue
            ports.append(entry)
    ports.sort(key=lambda port: port["name"])
    return ports


def list_runs(workspace_id: str) -> dict[str, Any]:
    root = bench_root(workspace_id)
    rows: list[tuple[datetime, str, dict[str, Any]]] = []
    skipped = 0
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for folder in folders:
        result_file = folder / "result.json"
        if not SAFE_ID.fullmatch(folder.name) or not folder.resolve().is_relative_to(root) or not result_file.is_file():
            skipped += 1
            continue
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(data, dict):
            skipped += 1
            continue
        when = _when(data, result_file)
        evaluator = data.get("evaluator_id")
        status = data.get("status")
        rows.append((when, folder.name, {
            "run_id": folder.name,
            "evaluator_id": evaluator if isinstance(evaluator, str) else "unknown",
            "status": status if isinstance(status, str) else "unknown",
            "verdict": _verdict(data.get("verdict")),
            "completed_at": when.isoformat(),
        }))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return {"runs": [row[2] for row in rows[:MAX_LISTED_RUNS]], "skipped": skipped}


def show(workspace_id: str, run_id: str) -> dict[str, Any]:
    root = bench_root(workspace_id)
    run_dir = _run_dir(root, run_id)
    try:
        view = _agent_view(show_run(run_dir, root=root))
    except BenchInputError as exc:
        raise BenchViewError(404, "run_not_found") from exc
    except Exception as exc:
        raise BenchViewError(422, "run_unreadable") from exc
    outputs = view.get("outputs")
    checks = [*view.get("failed_checks", []), *view.get("warning_checks", []), *view.get("other_checks", [])]
    return {
        "run_id": run_id,
        "evaluator_id": view.get("evaluator_id"),
        "status": view.get("status"),
        "verdict": _verdict(view.get("verdict")),
        "failure": view.get("failure"),
        "checks": checks,
        "outputs": outputs if isinstance(outputs, list) else [],
        "outputs_withheld": isinstance(outputs, str),
        "artifacts": view.get("artifacts", []),
        "truncated": bool(view.get("truncated")),
        "ports": _read_ports(run_dir),
    }


def compare(workspace_id: str, run_a: str, run_b: str) -> dict[str, Any]:
    root = bench_root(workspace_id)
    dir_a = _run_dir(root, run_a)
    dir_b = _run_dir(root, run_b)
    try:
        doc = _agent_view(compare_runs(dir_a, dir_b, root=root))
    except BenchInputError as exc:
        raise BenchViewError(404, "run_not_found") from exc
    except Exception as exc:
        raise BenchViewError(422, "compare_failed") from exc
    doc.pop("result_path_a", None)
    doc.pop("result_path_b", None)
    doc["verdict_a"] = _verdict(doc.get("verdict_a"))
    doc["verdict_b"] = _verdict(doc.get("verdict_b"))
    return {"run_id_a": run_a, "run_id_b": run_b, **doc}
