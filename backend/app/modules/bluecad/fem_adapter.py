"""BLUECAD CalculiX FEM adapter with deterministic solid-face pressure mapping."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.modules.bluecad import fem_adapter_base as _base
from app.modules.bluecad.fem_pressure_integration import (
    _deck_text,
    _parse_mesh,
    _prepare_pressure_mappings,
    _write_solid_solver_mesh,
)
from app.modules.bluecad.fem_reactions import _parse_reactions
from app.modules.bluecad.pressure_mapping import PressureMappingError

ToolRegistryError = _base.ToolRegistryError
resolve_tool = _base.resolve_tool
run_tool = _base.run_tool
_artifact_map = _base._artifact_map
_error = _base._error
_ERROR_PATTERN = _base._ERROR_PATTERN
_DIVERGED_PATTERN = _base._DIVERGED_PATTERN

# The retained 009 parser calls this module-global hook. Bind it to the hardened
# native/synthetic reaction parser before any solve executes.
_base._parse_reactions = _parse_reactions


def solve_static_analysis(
    analysis_spec: dict[str, Any],
    mesh_result: dict[str, Any],
    out_dir: str | Path,
    *,
    registry_path: str | Path | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Assemble a static CalculiX deck, run registered ccx, and summarize results."""

    if analysis_spec.get("analysis_type") != "static":
        return _error(
            "SOLVE_ERROR",
            {"message": "only static analysis is supported"},
            {},
        )
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, Any] = {}
    log_path = out_path / "analysis.log"
    try:
        tool = resolve_tool("calculix", registry_path)
        mesh_path = Path(mesh_result["artifacts"]["mesh_inp"]["path"]).resolve()
        mesh = _parse_mesh(mesh_path.read_text(encoding="utf-8"))
        pressure_mappings = _prepare_pressure_mappings(
            analysis_spec,
            mesh,
            out_path,
            artifacts,
        )
        solver_mesh_path = (
            _write_solid_solver_mesh(mesh, out_path, artifacts)
            if pressure_mappings
            else mesh_path
        )
        mesh_include = Path(
            os.path.relpath(solver_mesh_path, out_path.resolve())
        )
        inp_path = out_path / "analysis.inp"
        inp_path.write_text(
            _deck_text(
                analysis_spec,
                mesh_include,
                mesh,
                pressure_mappings=pressure_mappings,
            ),
            encoding="utf-8",
        )
        artifacts.update(_artifact_map({"inp": inp_path}))
        run = run_tool(
            "calculix",
            [inp_path.stem],
            out_path,
            timeout_s or float(analysis_spec.get("timeout_s", 60)),
            registry_path,
        )
        log_path.write_text(
            (run.stdout or "") + (run.stderr or ""),
            encoding="utf-8",
        )
        artifacts.update(_artifact_map({"log": log_path}))
        if run.timed_out:
            return _error(
                "TIMEOUT",
                {"returncode": run.returncode},
                artifacts,
                run.returncode,
                tool,
            )
        log_text = log_path.read_text(encoding="utf-8")
        if run.returncode != 0 or _ERROR_PATTERN.search(log_text):
            code = (
                "SOLVE_DIVERGED"
                if _DIVERGED_PATTERN.search(log_text)
                else "SOLVE_ERROR"
            )
            return _error(
                code,
                {"returncode": run.returncode},
                artifacts,
                run.returncode,
                tool,
            )
        frd_path = out_path / "analysis.frd"
        dat_path = out_path / "analysis.dat"
        artifacts.update(_artifact_map({"frd": frd_path, "dat": dat_path}))
        parsed = _parse_outputs(frd_path, dat_path, mesh)
    except ToolRegistryError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        artifacts.update(_artifact_map({"log": log_path}))
        return _error(
            "SOLVE_ERROR",
            {"registry_code": exc.code, **(exc.detail or {})},
            artifacts,
        )
    except PressureMappingError as exc:
        artifacts.update(_artifact_map({"log": log_path}))
        return _error(
            "PARSE_ERROR",
            {"mapping_code": exc.code, **exc.detail},
            artifacts,
        )
    except (OSError, KeyError, ValueError) as exc:
        artifacts.update(_artifact_map({"log": log_path}))
        return _error("PARSE_ERROR", {"message": str(exc)}, artifacts)
    return {
        "schema_version": "bluecad_result_summary_v0_1",
        "verdict": "pass",
        "errors": [],
        "solver": {
            "tool_id": "calculix",
            "version": tool["version_pin"],
            "returncode": run.returncode,
        },
        "analysis_type": "static",
        **parsed,
        "artifacts": artifacts,
    }


append_tier3_checks = _base.append_tier3_checks
_parse_outputs_base = _base._parse_outputs
_parse_native_frd = _base._parse_native_frd
_parse_frd_primary_record = _base._parse_frd_primary_record
_parse_frd_continuation = _base._parse_frd_continuation
_parse_frd_value_tail = _base._parse_frd_value_tail
_parse_legacy_frd = _base._parse_legacy_frd
_von_mises = _base._von_mises
_parse_header = _base._parse_header
_integer_values = _base._integer_values
_criteria_list = _base._criteria_list
_compare = _base._compare
_require_node_set = _base._require_node_set
_require_element_set = _base._require_element_set


def _parse_outputs(
    frd_path: Path,
    dat_path: Path,
    mesh: dict[str, Any],
) -> dict[str, Any]:
    parsed = _parse_outputs_base(frd_path, dat_path, mesh)
    reactions = parsed.get("reactions", [])
    parsed["reaction_resultant"] = (
        [sum(item["force"][axis] for item in reactions) for axis in range(3)]
        if reactions
        else None
    )
    return parsed
