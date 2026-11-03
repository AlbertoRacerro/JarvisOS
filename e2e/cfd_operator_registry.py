"""Write the operator-owned BlueCad tool registry that enables the spec 216 CFD toolchain.

The shipped ``configs/bluecad_tools.yaml`` keeps every subprocess tool disabled.
This script copies it, enables the OpenFOAM and Gmsh entries against one conda
environment outside the repository, and records for each binary its absolute
path, SHA-256 and the conda-forge package URL (with package SHA-256) as
provenance. The installed package versions must equal the shipped pins.

    PYTHONPATH=backend python e2e/cfd_operator_registry.py \
        --env ~/jarvis-control/work/tools/mamba/envs/cfd \
        --out ~/jarvis-control/work/tools/registry/bluecad_tools.cfd.yaml
    JARVISOS_BLUECAD_TOOL_REGISTRY=<out> python -m app.modules.bluecad.registry check
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from app.modules.bluecad.registry import DEFAULT_REGISTRY_PATH, load_registry

# tool id -> (conda package, binary name)
TOOLS = {
    "openfoam_blockmesh": ("openfoam", "blockMesh"),
    "openfoam_checkmesh": ("openfoam", "checkMesh"),
    "openfoam_simplefoam": ("openfoam", "simpleFoam"),
    "openfoam_decomposepar": ("openfoam", "decomposePar"),
    "openfoam_reconstructpar": ("openfoam", "reconstructPar"),
    "openfoam_mpiexec": ("mpich", "mpiexec"),
    "gmsh": ("gmsh", "gmsh"),
}


def _package(env: Path, name: str) -> dict[str, str]:
    matches = sorted((env / "conda-meta").glob(f"{name}-[0-9]*.json"))
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {name} package in {env}, found {len(matches)}")
    meta = json.loads(matches[0].read_text())
    return {"version": meta["version"], "url": meta["url"], "sha256": meta["sha256"]}


def _pin_matches(pin: str, version: str) -> bool:
    return pin == version or pin == f"v{version}"


def build(env: Path) -> dict:
    shipped = yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    for tool in shipped["tools"]:
        if tool["id"] not in TOOLS:
            continue
        package, binary = TOOLS[tool["id"]]
        meta = _package(env, package)
        if not _pin_matches(tool["version_pin"], meta["version"]):
            raise SystemExit(f"{tool['id']}: installed {package} {meta['version']} != pin {tool['version_pin']}")
        path = (env / "bin" / binary).resolve()
        tool.update({
            "enabled": True,
            "entrypoint": str(path),
            "binary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "provenance_url": f"{meta['url']}#sha256={meta['sha256']}",
        })
    return shipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    registry = build(args.env.expanduser().resolve())
    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    load_registry(out)  # fail closed on anything the loader would reject
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
