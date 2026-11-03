"""Checked mesh preparation for a typed smooth-tube study.

The caller supplies a fresh runtime directory. This operation only prepares and
checks the mesh; an accepted mesh is evidence for a later solver run, not a study
result or publication authority.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.bluecad.cfd_pipe.casegen import mesh_plan, write_case
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract
from app.modules.bluecad.cfd_pipe.mesh_check import (
    MeshCheckParseError,
    MeshCheckRecord,
    evaluate_mesh,
    parse_checkmesh,
    plan_refusals,
)
from app.modules.bluecad.registry import ToolRunResult, resolve_tool, run_tool


class MeshExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "refused"]
    check: MeshCheckRecord
    reason: str | None = None
    case_manifest: dict | None = None


def _failure(result: ToolRunResult) -> str:
    return result.code or f"exit_{result.returncode}"


def prepare_checked_mesh(
    contract: PipeStudyContract, case_dir: Path, *, registry_path: Path | None = None,
    cancel: Event | None = None, on_log: Callable[[str, str], None] | None = None, processors: int = 1,
) -> MeshExecution:
    """Generate one case and run pinned blockMesh/checkMesh, refusing bad meshes."""
    case_dir = Path(case_dir).resolve()
    plan = mesh_plan(contract)
    preflight = plan_refusals(contract, plan)
    if preflight:
        return MeshExecution(status="refused", check=evaluate_mesh(contract, plan, None),
                             reason="; ".join(preflight))
    # Resolve both binaries before writing a case. The registry verifies hashes,
    # enablement, and installation; no PATH fallback is permitted.
    block = resolve_tool("openfoam_blockmesh", registry_path)
    checker = resolve_tool("openfoam_checkmesh", registry_path)
    if block["version_pin"] != checker["version_pin"]:
        return MeshExecution(status="refused", check=evaluate_mesh(contract, plan, None),
                             reason="mesh tools have different OpenFOAM version pins")
    manifest = write_case(contract, case_dir, {"openfoam": block["version_pin"]}, processors)
    for tool_id, args, log_name in (
        ("openfoam_blockmesh", ["-case", str(case_dir)], "blockMesh.log"),
        ("openfoam_checkmesh", ["-case", str(case_dir), "-allGeometry", "-allTopology"], "checkMesh.log"),
    ):
        live = (lambda line, tool_id=tool_id: on_log(tool_id, line)) if on_log is not None else None
        kwargs = {"on_stdout": live} if live is not None else {}
        result = run_tool(tool_id, args, case_dir, timeout=120, registry_path=registry_path, cancel=cancel, **kwargs)
        (case_dir / log_name).write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode != 0:
            return MeshExecution(status="refused", check=evaluate_mesh(contract, plan, None),
                                 reason=f"{tool_id}: {_failure(result)}", case_manifest=manifest)
        if tool_id == "openfoam_checkmesh":
            try:
                report = parse_checkmesh(result.stdout + result.stderr)
            except MeshCheckParseError as exc:
                return MeshExecution(status="refused", check=evaluate_mesh(contract, plan, None),
                                     reason=f"checkMesh output: {exc}", case_manifest=manifest)
            check = evaluate_mesh(contract, plan, report)
            return MeshExecution(status="accepted" if check.accepted else "refused", check=check,
                                 reason="; ".join(check.refusals) or None, case_manifest=manifest)
    raise AssertionError("checkMesh did not run")
