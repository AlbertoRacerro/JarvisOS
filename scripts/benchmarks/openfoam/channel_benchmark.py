"""Run the bounded channel benchmark with locally installed, hash-pinned OpenFOAM."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.modules.bluecad.cfd_adapter import (
    generate_case,
    parse_probes,
    parse_residuals,
    parse_velocity_probes,
)
from app.modules.bluecad.registry import run_tool


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    config = yaml.safe_load((ROOT / "configs/bluecad_tools.yaml").read_text())
    hashes = {}
    for tool in config["tools"]:
        if tool["id"] in {"openfoam", "openfoam_blockmesh"}:
            binary = prefix / "bin" / ("icoFoam" if tool["id"] == "openfoam" else "blockMesh")
            tool["entrypoint"] = str(binary)
            tool["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
            tool["provenance_url"] = "https://www.openfoam.com/releases/openfoam-v2412/"
            tool["enabled"] = True
            hashes[tool["id"]] = tool["binary_sha256"]
    params = {"diameter": 0.01, "length": 0.2, "velocity": 0.01, "viscosity": 1e-6}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        registry = root / "registry.yaml"
        registry.write_text(yaml.safe_dump(config))
        case = root / "case"
        generate_case(case, params)
        logs = {}
        for tool_id in ("openfoam_blockmesh", "openfoam"):
            run = run_tool(tool_id, [], case, 300, registry)
            logs[tool_id] = run.stdout + run.stderr
            (case / f"{tool_id}.log").write_text(logs[tool_id])
            if run.returncode != 0:
                raise RuntimeError(f"{tool_id} failed: {logs[tool_id][-3000:]}")
        numerical = parse_residuals(logs["openfoam"])
        banner = re.search(r"Build\s+: (v\d+)", logs["openfoam"])
        if banner is None:
            raise RuntimeError("OpenFOAM version banner missing")
        probe_dir = max((case / "postProcessing/probes").iterdir(), key=lambda path: float(path.name))
        fixture = ROOT / "scripts/benchmarks/openfoam/solver_tail.txt"
        fixture.write_text("\n".join(logs["openfoam"].splitlines()[-35:]) + "\n")
        for field in ("p", "U"):
            source = probe_dir / field
            (ROOT / f"scripts/benchmarks/openfoam/probe_{field}.txt").write_text("\n".join(source.read_text().splitlines()[-5:]) + "\n")
        p0, p1 = parse_probes(probe_dir / "p")
        center = parse_velocity_probes(probe_dir / "U")
        measured_dp = (p0 - p1) * 1000  # Reference fluid density: 1000 kg/m3.
        expected_dp = 12 * 1000 * params["viscosity"] * params["velocity"] * (params["length"] / 2) / params["diameter"]**2
        expected_center = 1.5 * params["velocity"]
        archive = ROOT / "scripts/benchmarks/openfoam/channel_case_v2412.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for item in sorted(case.rglob("*")):
                if item.is_file():
                    bundle.write(item, item.relative_to(case))
        evidence = {
            "generated_by": "scripts/benchmarks/openfoam/channel_benchmark.py", "case_family": "laminar_2d_channel",
            "solver_version": banner.group(1), "binary_sha256": hashes, "parameters_si": params,
            "mesh": json.loads((case / "case.json").read_text())["mesh"],
            "case_digest": json.loads((case / "case.json").read_text())["case_digest"],
            "reynolds": params["velocity"] * params["diameter"] / params["viscosity"],
            "sample_positions_m": [0.05, 0.15], "reference_density_kg_m3": 1000,
            "numerical": numerical.model_dump(), "pressure_drop_pa": measured_dp,
            "expected_pressure_drop_pa": expected_dp,
            "pressure_drop_relative_error": abs(measured_dp - expected_dp) / expected_dp,
            "centreline_velocity_m_s": center, "expected_centreline_velocity_m_s": expected_center,
            "centreline_relative_error": abs(center - expected_center) / expected_center,
            "log_sha256": {key: hashlib.sha256(value.encode()).hexdigest() for key, value in logs.items()},
            "case_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
        output = ROOT / "scripts/benchmarks/openfoam/channel_v2412.json"
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"pressure_error": evidence["pressure_drop_relative_error"], "centreline_error": evidence["centreline_relative_error"], "residual": numerical.final_residual}))


if __name__ == "__main__":
    main()
