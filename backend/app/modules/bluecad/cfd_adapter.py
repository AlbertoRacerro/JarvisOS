"""Bounded, unqualified OpenFOAM v2412 laminar channel evaluator."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from app.core.paths import build_paths
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.bluecad.registry import ToolRegistryError, load_registry, resolve_tool, run_tool
from app.modules.engineering.evaluator_contracts import (
    AvailabilityState,
    EvaluationFailure,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
    failure_category_for_code,
)
from app.modules.engineering.refs import EvaluationResultRef, PhysicsCaseRef, Quantity
from app.modules.files.models import ArtifactCreate
from app.modules.files.service import create_artifact_record
from app.modules.process_kernel.units import parse_unit_token, unit_registry

EVALUATOR_ID = "openfoam_channel_v1"
_RESIDUAL = re.compile(r"Solving for (?:Ux|Uy|p), Initial residual = ([^,\s]+), Final residual = ([^,\s]+)")
_VERSION = re.compile(r"OpenFOAM-(v\d+)")


def _si(quantity: Quantity, unit: str) -> float:
    return float((quantity.value * unit_registry().Unit(parse_unit_token(quantity.unit))).to(unit).magnitude)


def case_parameters(request: EvaluationRequest) -> dict[str, float]:
    fields = {item.name: item.value for item in request.inputs}
    if set(fields) != {"diameter", "length", "velocity", "viscosity"}:
        raise ValueError("input_invalid: diameter, length, velocity, viscosity required")
    values = {name: _si(fields[name], unit) for name, unit in {
        "diameter": "m", "length": "m", "velocity": "m/s", "viscosity": "m**2/s",
    }.items()}
    if any(not math.isfinite(value) or value <= 0 for value in values.values()):
        raise ValueError("input_invalid: all channel parameters must be positive")
    if values["velocity"] * values["diameter"] / values["viscosity"] >= 2000:
        raise ValueError("input_invalid: Reynolds number outside laminar case family")
    if values["length"] / values["diameter"] < 10:
        raise ValueError("input_invalid: channel must be at least ten heights long")
    return values


def case_digest(parameters: dict[str, float]) -> str:
    payload = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def generate_case(root: Path, parameters: dict[str, float]) -> None:
    """Write a 2D extruded channel with stable inlet/outlet/wall labels."""
    length, height = parameters["length"], parameters["diameter"]
    velocity, nu = parameters["velocity"], parameters["viscosity"]
    width = height / 10
    nx = max(40, min(400, round(length / height * 8)))
    ny = 24
    for folder in ("0", "constant", "system"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "system/blockMeshDict").write_text(f"""FoamFile {{ version 2.0; format ascii; class dictionary; object blockMeshDict; }}
convertToMeters 1;
vertices ((0 0 0) ({length} 0 0) ({length} {height} 0) (0 {height} 0)
          (0 0 {width}) ({length} 0 {width}) ({length} {height} {width}) (0 {height} {width}));
blocks (hex (0 1 2 3 4 5 6 7) ({nx} {ny} 1) simpleGrading (1 1 1));
edges ();
boundary (
 inlet {{ type patch; faces ((0 4 7 3)); }}
 outlet {{ type patch; faces ((1 2 6 5)); }}
 walls {{ type wall; faces ((0 1 5 4) (3 7 6 2)); }}
 frontAndBack {{ type empty; faces ((0 3 2 1) (4 5 6 7)); }}
);
mergePatchPairs ();
""")
    (root / "constant/transportProperties").write_text(f"FoamFile {{ version 2.0; format ascii; class dictionary; object transportProperties; }}\ntransportModel Newtonian;\nnu [0 2 -1 0 0 0 0] {nu};\n")
    header = "FoamFile { version 2.0; format ascii; class volVectorField; object U; }\n"
    (root / "0/U").write_text(header + f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform ({velocity} 0 0);\nboundaryField {{ inlet {{type fixedValue; value uniform ({velocity} 0 0);}} outlet {{type zeroGradient;}} walls {{type noSlip;}} frontAndBack {{type empty;}} }}\n")
    (root / "0/p").write_text("FoamFile { version 2.0; format ascii; class volScalarField; object p; }\ndimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\nboundaryField { inlet {type zeroGradient;} outlet {type fixedValue; value uniform 0;} walls {type zeroGradient;} frontAndBack {type empty;} }\n")
    (root / "system/fvSchemes").write_text("FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }\nddtSchemes {default Euler;} gradSchemes {default Gauss linear;} divSchemes {default none; div(phi,U) Gauss linearUpwind grad(U);} laplacianSchemes {default Gauss linear corrected;} interpolationSchemes {default linear;} snGradSchemes {default corrected;}\n")
    (root / "system/fvSolution").write_text("FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }\nsolvers { p {solver PCG; preconditioner DIC; tolerance 1e-8; relTol 0.01;} pFinal {$p; relTol 0;} U {solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.01;} UFinal {$U; relTol 0;} }\nPISO {nCorrectors 2; nNonOrthogonalCorrectors 0;}\n")
    z = width / 2
    (root / "system/controlDict").write_text(f"""FoamFile {{ version 2.0; format ascii; class dictionary; object controlDict; }}
application icoFoam; startFrom startTime; startTime 0; stopAt endTime; endTime 20; deltaT 0.002;
writeControl timeStep; writeInterval 10000; purgeWrite 0; writeFormat ascii;
functions {{ probes {{ type probes; libs (sampling); writeControl timeStep; writeInterval 10;
fields (p U); probeLocations (({length*0.25} {height/2} {z}) ({length*0.75} {height/2} {z})); }} }}
""")
    (root / "case.json").write_text(json.dumps({"family": "laminar_2d_channel", "parameters_si": parameters, "mesh": {"nx": nx, "ny": ny, "nz": 1}, "case_digest": case_digest(parameters)}, sort_keys=True, indent=2) + "\n")


def parse_residuals(log: str) -> NumericalDiagnostics:
    matches = [(float(a), float(b)) for a, b in _RESIDUAL.findall(log)]
    if not matches or any(not math.isfinite(a) or not math.isfinite(b) or a < 0 or b < 0 for a, b in matches):
        raise ValueError("missing or hostile residuals")
    time_steps = len(re.findall(r"^Time = ", log, re.MULTILINE))
    return NumericalDiagnostics(converged=matches[-1][1] < 1e-4, final_residual=matches[-1][1], iterations=time_steps or len(matches))


def parse_probes(path: Path) -> tuple[float, float]:
    rows = [line.split() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not rows or len(rows[-1]) != 3:
        raise ValueError("missing probe pair")
    values = [float(item) for item in rows[-1]]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("hostile probe value")
    return values[1], values[2]


def parse_velocity_probes(path: Path) -> float:
    rows = [line for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not rows:
        raise ValueError("missing velocity probes")
    matches = re.fullmatch(r"\s*[0-9.eE+-]+\s+\(([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\)\s+\(([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\)\s*", rows[-1])
    if matches is None:
        raise ValueError("invalid velocity probes")
    value = float(matches.group(2))
    if not math.isfinite(value):
        raise ValueError("hostile velocity probe")
    return value


def case_lineage(request: EvaluationRequest, case: Path, parameters: dict[str, float], kinematic_pressure_drop: float, centerline: float) -> dict[str, object]:
    mesh_files = sorted((case / "constant/polyMesh").iterdir())
    mesh_hashes = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in mesh_files if item.is_file()}
    if not mesh_hashes:
        raise ValueError("missing mesh files")
    return {
        "request_ref": request.request_ref.model_dump(mode="json"),
        "subject_ref": request.subject_ref.model_dump(mode="json"),
        "case_digest": case_digest(parameters), "mesh_sha256": mesh_hashes,
        "results": {"kinematic_pressure_drop_m2_s2": kinematic_pressure_drop, "centreline_velocity_m_s": centerline},
    }


class OpenFoamEvaluator:
    def __init__(self, registry_path: Path | None = None, cancellation: Event | None = None) -> None:
        self.registry_path = registry_path
        self.cancellation = cancellation or Event()

    def availability(self) -> EvaluatorAvailability:
        now = datetime.now(UTC)
        try:
            solver = resolve_tool("openfoam", self.registry_path)
            resolve_tool("openfoam_blockmesh", self.registry_path)
            banner = run_tool("openfoam", ["-help"], Path.cwd(), 10, self.registry_path)
            match = _VERSION.search(banner.stdout + banner.stderr)
            if banner.returncode != 0 or match is None or match.group(1) != solver["version_pin"]:
                return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state="unhealthy", checked_at=now, reason_code="SOLVER_BANNER_INVALID")
            return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state="available", checked_at=now, backend_version=match.group(1))
        except ToolRegistryError as exc:
            state: AvailabilityState = "disabled" if exc.code == "TOOL_DISABLED" else "not_installed" if exc.code in {"TOOL_BINARY_MISSING", "TOOL_UNKNOWN"} else "unhealthy"
            return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state=state, checked_at=now, reason_code=exc.code)
        except OSError:
            return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state="unhealthy", checked_at=now, reason_code="SOLVER_LAUNCH_ERROR")

    def descriptor(self) -> EvaluatorDescriptor:
        available = self.availability()
        version = available.backend_version
        if version is None:
            registry = load_registry(self.registry_path)
            version = next(tool["version_pin"] for tool in registry["tools"] if tool["id"] == "openfoam")
        return EvaluatorDescriptor(evaluator_id=EVALUATOR_ID, backend_kind="cfd_solver", backend_name="OpenFOAM", backend_version=version, tool_registry_id="openfoam", fidelity="field_resolved", capabilities=("laminar_channel_flow",))

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        now = datetime.now(UTC)
        version = self.descriptor().backend_version

        def result(status: EvaluationStatus, code: str | None = None, *, numerical: NumericalDiagnostics | None = None, outputs: tuple[NamedQuantity, ...] = (), artifacts: tuple[SourceRef, ...] = ()) -> EvaluationResult:
            failure = None
            if code is not None:
                category = "cancelled" if status == "cancelled" else "not_available" if status == "refused" and code != "input_invalid" else failure_category_for_code(code)
                failure = EvaluationFailure(category=category, backend_code=code)
            completed = datetime.now(UTC)
            diagnostics = numerical or NumericalDiagnostics()
            content = {
                "request_ref": request.request_ref.model_dump(mode="json"), "evaluator_id": EVALUATOR_ID,
                "backend_version": version, "status": status,
                "failure": failure.model_dump(mode="json") if failure else None,
                "outputs": [item.model_dump(mode="json") for item in outputs],
                "output_artifacts": [item.model_dump(mode="json") for item in artifacts],
                "numerical": diagnostics.model_dump(mode="json"), "completed_at": completed.isoformat(),
            }
            digest = "sha256:" + hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            result_ref = EvaluationResultRef(authority_owner="bluecad", object_id=request.request_ref.object_id, workspace_id=request.request_ref.workspace_id, content_digest=digest)
            return EvaluationResult(result_ref=result_ref, request_ref=request.request_ref, evaluator_id=EVALUATOR_ID, backend_version=version, status=status, failure=failure, fidelity="field_resolved", outputs=outputs, output_artifacts=artifacts, numerical=diagnostics, completed_at=completed)
        if request.evaluator_id != EVALUATOR_ID:
            return result("refused", "input_invalid")
        if self.cancellation.is_set():
            return result("cancelled", "CANCELLED")
        if request.is_expired(now):
            return result("deadline_exceeded", "TIMEOUT")
        try:
            parameters = case_parameters(request)
        except (ValueError, KeyError):
            return result("refused", "input_invalid")
        if not isinstance(request.subject_ref, PhysicsCaseRef) or request.subject_ref.content_digest != case_digest(parameters):
            return result("refused", "input_invalid")
        available = self.availability()
        if available.state != "available":
            return result("refused", available.reason_code or "TOOL_UNKNOWN")
        remaining = (request.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return result("deadline_exceeded", "TIMEOUT")
        paths = build_paths()
        work = paths.artifacts_dir / request.request_ref.workspace_id / "cfd_work"
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            case = Path(temporary)
            generate_case(case, parameters)
            for tool_id, logname in (("openfoam_blockmesh", "blockMesh.log"), ("openfoam", "icoFoam.log")):
                remaining = (request.deadline_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    return result("deadline_exceeded", "TIMEOUT")
                try:
                    run = run_tool(tool_id, [], case, remaining, self.registry_path, cancel=self.cancellation)
                except ToolRegistryError as exc:
                    return result("refused", exc.code)
                except OSError:
                    return result("failed", "SOLVE_ERROR")
                (case / logname).write_text(run.stdout + run.stderr)
                if run.code == "CANCELLED":
                    return result("cancelled", "CANCELLED")
                if run.timed_out:
                    return result("deadline_exceeded", "TIMEOUT")
                if run.returncode != 0:
                    return result("failed", "SOLVE_ERROR")
            try:
                diagnostics = parse_residuals((case / "icoFoam.log").read_text())
                probe_dir = sorted((case / "postProcessing/probes").iterdir(), key=lambda path: float(path.name))[-1]
                p0, p1 = parse_probes(probe_dir / "p")
                centerline = parse_velocity_probes(probe_dir / "U")
                if p0 <= p1 or centerline <= 0:
                    raise ValueError("unphysical probe values")
            except (OSError, ValueError, IndexError):
                return result("failed", "PARSE_ERROR")
            if diagnostics.converged is not True:
                return result("failed", "SOLVE_DIVERGED", numerical=diagnostics)
            try:
                lineage = case_lineage(request, case, parameters, p0 - p1, centerline)
            except (OSError, ValueError):
                return result("failed", "PARSE_ERROR")
            (case / "lineage.json").write_text(json.dumps(lineage, sort_keys=True, indent=2) + "\n")
            archive = case / "case.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for item in sorted(case.rglob("*")):
                    if item.is_file() and item != archive:
                        bundle.write(item, item.relative_to(case))
            digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            destination = paths.artifacts_dir / request.request_ref.workspace_id / "cfd" / (digest[7:] + ".zip")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, destination)
            record = create_artifact_record(request.request_ref.workspace_id, ArtifactCreate(filename=destination.name, stored_path=str(destination), artifact_type="cfd_case", mime_type="application/zip", sha256=digest[7:], source_ref=request.subject_ref.model_dump_json(), notes=f"case_digest={case_digest(parameters)}; mesh=blockMesh"))
            artifact_ref = SourceRef(authority_owner="artifacts", object_type="artifact", object_id=record.id, workspace_id=request.request_ref.workspace_id, content_digest=digest)
            # icoFoam solves kinematic pressure (p/rho); density is not a case input, so no Pa value is claimed.
            outputs = (NamedQuantity(name="kinematic_pressure_drop", value=Quantity(value=p0 - p1, unit="m**2/s**2")), NamedQuantity(name="centreline_velocity", value=Quantity(value=centerline, unit="m/s")))
            if datetime.now(UTC) >= request.deadline_at:
                return result("deadline_exceeded", "TIMEOUT")
            return result("succeeded", numerical=diagnostics, outputs=outputs, artifacts=(artifact_ref,))
