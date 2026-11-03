"""Server run records for reduced-model publication tests; no OpenFOAM, only files a finished run leaves."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.core.paths import build_paths
from app.modules.bluecad.cfd_pipe.casegen import probe_locations
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract, contract_digest
from app.modules.bluecad.cfd_pipe.reduced_model import study_template_digest
from tests.test_216_reduced_model import point

DIGEST = "sha256:" + "0" * 64


def probe_files(contract: PipeStudyContract, gradient_pa_m: float, iteration: int = 1500) -> tuple[str, str]:
    """Final-time OpenFOAM probe text (kinematic pressure, velocity) with the given developed gradient."""
    places = probe_locations(contract)
    head = "".join(f"# Probe {i} ({x!r} {y!r} {z!r})\n" for i, (x, y, z) in enumerate(places))
    drop = gradient_pa_m / contract.fluid.density_kg_m3 * (places[1][0] - places[0][0])
    pressure = head + f"{iteration} {1.0 + drop!r} 1.0 1.0 1.0\n"
    velocity = head + f"{iteration} " + " ".join(f"({contract.mean_velocity_m_s!r} 0 0)" for _ in places) + "\n"
    return pressure, velocity


def run_dir(workspace_id: str, component_id: str, run_id: str) -> Path:
    return build_paths().workspaces_dir / workspace_id / "design_cfd_runs" / component_id / run_id


def write_run(workspace_id: str, component_id: str, contract: PipeStudyContract, *, error: float = 0.0,
              state: str = "finished", status: str = "measured", converged: bool = True, cells: int | None = 1000,
              digest: str | None = DIGEST, contract_digest_recorded: bool = True,
              tool_versions: dict[str, str] | None = None) -> str:
    """Write the run.json and contract.json a finished server run leaves; ``error`` is relative to the reference."""
    run_id = uuid4().hex
    path = run_dir(workspace_id, component_id, run_id)
    path.mkdir(parents=True)
    measurement: dict = {"status": status, "reason": None if status == "measured" else "probes unreadable"}
    if status == "measured":
        measurement |= {"regime": contract.regime, "reynolds": contract.reynolds, "mesh_level": contract.mesh_level,
                        "pressure_gradient_pa_m": point(contract, error).pressure_gradient_pa_m, "cells": cells,
                        "case_manifest_digest": digest,
                        "tool_versions": tool_versions if tool_versions is not None else {"openfoam_simplefoam": "2412"},
                        "comparison": {}}
    record = {"run_id": run_id, "component_id": component_id, "regime": contract.regime, "state": state,
              "template_digest": study_template_digest(contract), "geometry_digest": contract.domain.geometry_digest,
              "queued_at": "2026-01-01T00:00:00Z", "retry_of": None, "qualifies_publication": False,
              "residual_converged": converged, "measurement": measurement}
    if contract_digest_recorded:
        record["contract_digest"] = contract_digest(contract)
    else:
        record["geometry_digest"] = ""
    (path / "run.json").write_text(json.dumps(record), encoding="utf-8")
    (path / "contract.json").write_text(json.dumps(contract.model_dump(mode="json")), encoding="utf-8")
    return run_id


def turbulent_study(workspace_id: str, component_id: str, re: float = 2e4) -> list[str]:
    """Three same-Re runs at mesh levels 0, 1, 2 (coarse to fine) whose Darcy values converge monotonically."""
    from tests.test_216_reduced_model import turbulent

    runs = []
    for level, error, cells in ((0, 0.010, 20000), (1, 0.004, 68000), (2, 0.0, 230000)):
        runs.append(write_run(workspace_id, component_id, turbulent(re, mesh_level=level), error=error, cells=cells))
    return runs
