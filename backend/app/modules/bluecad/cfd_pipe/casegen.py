"""Deterministic OpenFOAM case generation from a :class:`PipeStudyContract`.

Domain: one quarter of the 3D bore (axis = x, symmetry planes y = 0 and z = 0) on a
structured O-grid: a square core block and two outer blocks whose outer faces are
the wall arcs. Laminar developed pipe flow is axisymmetric and the RANS mean flow of
a smooth straight pipe is too, so both symmetry planes are exact for this study.

A 2D-axisymmetric wedge is not generated yet. An earlier wedge probe showed a
pressure-gradient error that predates the pinned v2412 transpose workaround
(``cache { grad(U) yes; }``); with it, a 2026-10-25 diagnostic wedge matched
Hagen–Poiseuille within 0.06% and the 3D C1 study within 0.45%. A typed wedge
variant still needs a stated equivalence tolerance and a wedge-normal residual
rule; see the spec 216 implementation evidence.

The inlet imposes the exact volumetric flow rate (a quarter of the contract value),
so the faceted wall's slightly smaller area raises the mean velocity rather than
lowering the flow; that discretisation error vanishes under refinement.

``generate`` returns ``{relative_path: text}``; ``write_case`` writes it with a
manifest of input digests and the pinned tool version. Same contract and tool pin
give a byte-identical case.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.modules.bluecad.cfd_pipe.contract import (
    DOWNSTREAM_STATION_FRACTION,
    UPSTREAM_STATION_FRACTION,
    PipeStudyContract,
    contract_digest,
)
from app.modules.bluecad.cfd_pipe.foam_dict import render
from app.modules.bluecad.cfd_pipe.references import friction_estimate

CASEGEN_VERSION = "cfd_pipe_casegen_v2"
MAX_PROCESSORS = 64
MANIFEST_NAME = "jarvis_case_manifest.json"
DOMAIN_FRACTION = 0.25
# Core block: edge midpoints at CORE_EDGE * R on the symmetry planes, corner at CORE_CORNER * R
# on the diagonal (a slightly convex square keeps outer-block cells near-orthogonal).
CORE_EDGE = 0.5
CORE_CORNER = 0.6
REFINEMENT_RATIO = 1.5
MAX_AXIAL_CELLS = 5000
C_MU = 0.09
# Pinned-v2412 defect workaround (docs/implementation/216-cfd-pipe-qualification.md): in the
# conda-forge openfoam-2412 build, T() of a temporary tensor field writes (grad U)_zx into both
# XZ and ZX, so dev2(T(fvc::grad(U))) in the laminar/RAS stress adds a spurious body force when
# walls lie in two transverse directions (+22% on a square duct, +25% on the quarter pipe).
# Caching grad(U) makes fvc::grad return a non-reusable reference, so T() takes the allocating,
# correct path. Measured: square duct 2.8105 Pa/m (icoFoam 2.8105; analytic 2.8511).
CACHED_FIELDS = ("grad(U)",)
# First-cell-centre y+ targets by wall treatment.
LOW_RE_YPLUS = 0.5
WALL_FUNCTION_YPLUS = 60.0
_SYMMETRY_PATCHES = ("symmetry_y", "symmetry_z")
FIELD_FUNCTION = "pipe_fields"  # controlDict function object; output in postProcessing/pipe_fields/<time>/
FIELD_SURFACE = "symmetry_z"  # axial-section patch (z = 0 plane, contains the x axis) sampled for the viewport


class MeshPlan(BaseModel):
    """Quarter-pipe O-grid resolution and its pre-run wall estimate.

    ``wall_cell_height_m`` is the largest wall-adjacent cell height (on the symmetry
    planes, where the outer blocks span ``outer_extent_m``), so the y+ estimate is an
    upper bound over the wall.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: int
    core_cells: int
    outer_radial_cells: int
    axial_cells: int
    outer_grading: float
    outer_extent_m: float
    wall_cell_height_m: float
    axial_cell_length_m: float
    estimated_wall_yplus: float | None
    cell_count: int


def _graded_heights(extent: float, n: int, grading: float) -> tuple[float, float]:
    """(first, last) cell heights of a blockMesh simpleGrading edge (last/first = grading)."""
    if n == 1 or math.isclose(grading, 1.0):
        return extent / n, extent / n
    ratio = grading ** (1.0 / (n - 1))
    first = extent * (ratio - 1.0) / (ratio**n - 1.0)
    return first, first * ratio ** (n - 1)


def _grading_for_wall_height(extent: float, n: int, wall_height: float) -> float:
    """Expansion ratio whose last (wall) cell has ``wall_height``; bisection in log space."""
    lo, hi = math.log(1e-5), math.log(1e5)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _graded_heights(extent, n, math.exp(mid))[1] < wall_height:
            lo = mid
        else:
            hi = mid
    return float(format(math.exp(0.5 * (lo + hi)), ".6g"))


def friction_velocity(contract: PipeStudyContract) -> float:
    return contract.mean_velocity_m_s * math.sqrt(friction_estimate(contract.reynolds) / 8.0)


def mesh_plan(contract: PipeStudyContract) -> MeshPlan:
    level = contract.mesh_level
    scale = REFINEMENT_RATIO**level
    radius = contract.diameter_m / 2.0
    extent = (1.0 - CORE_EDGE) * radius
    length = contract.domain.length_m
    viscous_length = contract.fluid.kinematic_viscosity_m2_s / friction_velocity(contract)
    if contract.regime == "laminar":
        core, outer, grading = round(8 * scale), round(8 * scale), 1.0
        # Developed laminar flow has no axial gradients; dx = 8 core cells keeps aspect ratios moderate.
        axial = round(length / (8.0 * CORE_EDGE * radius / core))
    elif contract.wall_treatment == "low_re_resolved":
        core, outer = round(10 * scale), round(30 * scale)
        grading = _grading_for_wall_height(extent, outer, 2.0 * LOW_RE_YPLUS * viscous_length)
        axial = round(length / (contract.diameter_m / 4.0) * scale)
    else:
        core = round(8 * scale)
        outer = max(2, round(extent / (2.0 * WALL_FUNCTION_YPLUS * viscous_length) * scale))
        grading = 1.0
        axial = round(length / (contract.diameter_m / 4.0) * scale)
    axial = max(10, min(MAX_AXIAL_CELLS, axial))
    wall_h = _graded_heights(extent, outer, grading)[1]
    yplus = None if contract.regime == "laminar" else 0.5 * wall_h / viscous_length
    return MeshPlan(
        level=level, core_cells=core, outer_radial_cells=outer, axial_cells=axial, outer_grading=grading,
        outer_extent_m=extent, wall_cell_height_m=wall_h, axial_cell_length_m=length / axial,
        estimated_wall_yplus=yplus, cell_count=(core * core + 2 * core * outer) * axial,
    )


def _vec(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (float(x), float(y), float(z))


def block_mesh_dict(contract: PipeStudyContract, plan: MeshPlan) -> dict[str, Any]:
    radius = contract.diameter_m / 2.0
    length = contract.domain.length_m
    a = CORE_EDGE * radius
    b = CORE_CORNER * radius / math.sqrt(2.0)
    w = radius / math.sqrt(2.0)
    # Section points (y, z): axis, core edge on y, core corner, core edge on z, wall on y, wall diagonal, wall on z.
    section = [(0.0, 0.0), (a, 0.0), (b, b), (0.0, a), (radius, 0.0), (w, w), (0.0, radius)]
    vertices = [_vec(0.0, y, z) for y, z in section] + [_vec(length, y, z) for y, z in section]
    n, m, nx, g = plan.core_cells, plan.outer_radial_cells, plan.axial_cells, plan.outer_grading

    def arc(start: int, end: int, angle_deg: float, x: float) -> str:
        angle = math.radians(angle_deg)
        return f"arc {start} {end} ({x:.12g} {radius * math.cos(angle):.12g} {radius * math.sin(angle):.12g})"

    def faces(*quads: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
        return list(quads)

    return {
        "convertToMeters": 1.0,
        "vertices": vertices,
        # Local x1, x2 counter-clockwise in the (y, z) section, x3 along the pipe axis.
        "blocks": [
            f"hex (0 1 2 3 7 8 9 10) ({n} {n} {nx}) simpleGrading (1 1 1)",
            f"hex (1 4 5 2 8 11 12 9) ({m} {n} {nx}) simpleGrading ({g:.6g} 1 1)",
            f"hex (3 2 5 6 10 9 12 13) ({n} {m} {nx}) simpleGrading (1 {g:.6g} 1)",
        ],
        "edges": [arc(4, 5, 22.5, 0.0), arc(5, 6, 67.5, 0.0), arc(11, 12, 22.5, length), arc(12, 13, 67.5, length)],
        "boundary": [
            {"inlet": {"type": "patch", "faces": faces((0, 3, 2, 1), (1, 2, 5, 4), (3, 6, 5, 2))}},
            {"outlet": {"type": "patch", "faces": faces((7, 8, 9, 10), (8, 11, 12, 9), (10, 9, 12, 13))}},
            {"wall": {"type": "wall", "faces": faces((4, 5, 12, 11), (5, 6, 13, 12))}},
            {"symmetry_y": {"type": "symmetryPlane", "faces": faces((0, 1, 8, 7), (1, 4, 11, 8))}},
            {"symmetry_z": {"type": "symmetryPlane", "faces": faces((0, 7, 10, 3), (3, 10, 13, 6))}},
        ],
        "mergePatchPairs": [],
    }


def inlet_turbulence(contract: PipeStudyContract) -> dict[str, float]:
    u = contract.mean_velocity_m_s
    k = 1.5 * (contract.inlet.turbulence_intensity * u) ** 2
    ell = contract.inlet.mixing_length_over_d * contract.diameter_m
    return {"k": k, "epsilon": C_MU**0.75 * k**1.5 / ell, "omega": math.sqrt(k) / (C_MU**0.25 * ell)}


def _field(name: str, dims: str, internal: str, patches: dict[str, dict[str, Any]], foam_class: str = "volScalarField") -> str:
    boundary = {**patches, **{patch: {"type": "symmetryPlane"} for patch in _SYMMETRY_PATCHES}}
    return render(foam_class, "0", name, {"dimensions": dims, "internalField": internal, "boundaryField": boundary})


def initial_fields(contract: PipeStudyContract) -> dict[str, str]:
    u_in = f"uniform ({contract.mean_velocity_m_s:.12g} 0 0)"
    # simpleFoam solves kinematic pressure p/rho.
    p_out = f"uniform {contract.outlet_pressure_pa / contract.fluid.density_kg_m3:.12g}"
    flow = f"constant {DOMAIN_FRACTION * contract.flow_rate_m3_s:.12g}"
    files = {
        "0/U": _field("U", "[0 1 -1 0 0 0 0]", u_in, {
            "inlet": {"type": "flowRateInletVelocity", "volumetricFlowRate": flow, "value": u_in},
            "outlet": {"type": "inletOutlet", "inletValue": "uniform (0 0 0)", "value": u_in},
            "wall": {"type": "noSlip"},
        }, foam_class="volVectorField"),
        "0/p": _field("p", "[0 2 -2 0 0 0 0]", p_out, {
            "inlet": {"type": "zeroGradient"},
            "outlet": {"type": "fixedValue", "value": p_out},
            "wall": {"type": "zeroGradient"},
        }),
    }
    if contract.regime == "laminar":
        return files
    t = inlet_turbulence(contract)
    low_re = contract.wall_treatment == "low_re_resolved"
    k_in = f"uniform {t['k']:.12g}"
    files["0/k"] = _field("k", "[0 2 -2 0 0 0 0]", k_in, {
        "inlet": {"type": "fixedValue", "value": k_in},
        "outlet": {"type": "inletOutlet", "inletValue": k_in, "value": k_in},
        "wall": {"type": "fixedValue", "value": "uniform 1e-15"} if low_re else {"type": "kqRWallFunction", "value": k_in},
    })
    if contract.turbulence_model == "kOmegaSST":
        w_in = f"uniform {t['omega']:.12g}"
        files["0/omega"] = _field("omega", "[0 0 -1 0 0 0 0]", w_in, {
            "inlet": {"type": "fixedValue", "value": w_in},
            "outlet": {"type": "inletOutlet", "inletValue": w_in, "value": w_in},
            "wall": {"type": "omegaWallFunction", "value": w_in},
        })
    else:
        e_in = f"uniform {t['epsilon']:.12g}"
        files["0/epsilon"] = _field("epsilon", "[0 2 -3 0 0 0 0]", e_in, {
            "inlet": {"type": "fixedValue", "value": e_in},
            "outlet": {"type": "inletOutlet", "inletValue": e_in, "value": e_in},
            "wall": {"type": "epsilonWallFunction", "value": e_in},
        })
    nut_wall = {"type": "nutLowReWallFunction" if low_re else "nutkWallFunction", "value": "uniform 0"}
    files["0/nut"] = _field("nut", "[0 2 -1 0 0 0 0]", "uniform 0", {
        "inlet": {"type": "calculated", "value": "uniform 0"},
        "outlet": {"type": "calculated", "value": "uniform 0"},
        "wall": nut_wall,
    })
    return files


def turbulence_fields(contract: PipeStudyContract) -> tuple[str, ...]:
    if contract.regime == "laminar":
        return ()
    return ("k", "omega") if contract.turbulence_model == "kOmegaSST" else ("k", "epsilon")


def control_dict(contract: PipeStudyContract, *, stop_now: bool = False) -> str:
    """controlDict; ``stop_now`` renders the typed safe-stop variant (write and end).

    There is no solver-side residualControl: the runner judges convergence from
    parsed velocity, pressure, and turbulence residuals and stops through this
    rewrite, which ``runTimeModifiable`` makes the solver read between iterations.
    """
    control = {
        "application": "simpleFoam",
        "startFrom": "startTime", "startTime": 0, "stopAt": "writeNow" if stop_now else "endTime",
        "endTime": contract.solver.max_iterations, "deltaT": 1,
        "writeControl": "timeStep", "writeInterval": contract.solver.max_iterations,
        "purgeWrite": 1, "writeFormat": "ascii", "writePrecision": 12,
        "writeCompression": "off", "timeFormat": "general", "timePrecision": 8,
        "runTimeModifiable": "true",
        "functions": {
            "pipe_probes": {
                "type": "probes", "libs": ('"libsampling.so"',),
                # Interpolate to the probe point; the default cell-centre value biases the
                # radial profile by up to half a cell's velocity change.
                "interpolationScheme": "cellPoint",
                "writeControl": "timeStep", "writeInterval": contract.solver.max_iterations,
                "fields": ("p", "U"),
                "probeLocations": probe_locations(contract),
            },
            FIELD_FUNCTION: {
                # Axial section for the viewport: the symmetry_z patch lies on the domain boundary in the
                # z = 0 plane and contains the pipe axis (x), so it needs no offset or cutting-plane
                # tolerance and is exact for the symmetric quarter domain. Points are cellPoint-interpolated.
                "type": "surfaces", "libs": ('"libsampling.so"',),
                "surfaceFormat": "vtk",
                "formatOptions": {"vtk": {"legacy": "true", "format": "ascii"}},
                "interpolationScheme": "cellPoint", "interpolate": "true",
                "writeControl": "timeStep", "writeInterval": contract.solver.max_iterations,
                "fields": ("p", "U"),
                "surfaces": [{FIELD_SURFACE: {"type": "patch", "patches": (FIELD_SURFACE,)}}],
            },
        },
    }
    return render("dictionary", "system", "controlDict", control)


def probe_locations(contract: PipeStudyContract) -> tuple[tuple[float, float, float], ...]:
    """Two developed-section pressure stations, then radial velocity samples.

    Samples stay inside cells, off the symmetry planes and wall. The first two
    locations have the same radius, so their pressure difference measures the
    developed gradient without an inlet or outlet boundary value.
    """
    length = contract.domain.length_m
    radius = contract.diameter_m / 2.0
    return (
        (UPSTREAM_STATION_FRACTION * length, 0.1 * radius, 0.1 * radius),
        (DOWNSTREAM_STATION_FRACTION * length, 0.1 * radius, 0.1 * radius),
        (DOWNSTREAM_STATION_FRACTION * length, 0.4 * radius, 0.1 * radius),
        (DOWNSTREAM_STATION_FRACTION * length, 0.7 * radius, 0.1 * radius),
    )


def system_files(contract: PipeStudyContract, plan: MeshPlan) -> dict[str, str]:
    tol = contract.solver.residual_target
    turb = turbulence_fields(contract)
    div = {
        "default": "none",
        "div(phi,U)": "bounded Gauss linearUpwind grad(U)",
        "div((nuEff*dev2(T(grad(U)))))": "Gauss linear",
    }
    for name in turb:
        div[f"div(phi,{name})"] = "bounded Gauss limitedLinear 1"
    schemes = {
        "ddtSchemes": {"default": "steadyState"},
        "gradSchemes": {"default": "Gauss linear"},
        "divSchemes": div,
        "laplacianSchemes": {"default": "Gauss linear corrected"},
        "interpolationSchemes": {"default": "linear"},
        "snGradSchemes": {"default": "corrected"},
        "wallDist": {"method": "meshWave"},
    }
    solution = {
        "solvers": {
            "p": {"solver": "GAMG", "smoother": "DICGaussSeidel", "nCellsInCoarsestLevel": 50,
                  "directSolveCoarsest": "true", "tolerance": tol * 1e-2, "relTol": 0.1},
            '"(' + "|".join(("U", *turb)) + ')"': {"solver": "smoothSolver", "smoother": "symGaussSeidel",
                                                  "tolerance": tol * 1e-2, "relTol": 0.1},
        },
        "SIMPLE": {"nNonOrthogonalCorrectors": 0, "consistent": "yes"},
        "relaxationFactors": {"equations": {"U": 0.9, '".*"': 0.7}},
        "cache": {name: "yes" for name in CACHED_FIELDS},
    }
    return {
        "system/blockMeshDict": render("dictionary", "system", "blockMeshDict", block_mesh_dict(contract, plan)),
        "system/controlDict": control_dict(contract),
        "system/fvSchemes": render("dictionary", "system", "fvSchemes", schemes),
        "system/fvSolution": render("dictionary", "system", "fvSolution", solution),
    }


def constant_files(contract: PipeStudyContract) -> dict[str, str]:
    transport = {"transportModel": "Newtonian", "nu": contract.fluid.kinematic_viscosity_m2_s}
    if contract.regime == "laminar":
        turbulence: dict[str, Any] = {"simulationType": "laminar"}
    else:
        assert contract.turbulence_model is not None
        turbulence = {"simulationType": "RAS",
                      "RAS": {"RASModel": contract.turbulence_model, "turbulence": "on", "printCoeffs": "on"}}
    return {
        "constant/transportProperties": render("dictionary", "constant", "transportProperties", transport),
        "constant/turbulenceProperties": render("dictionary", "constant", "turbulenceProperties", turbulence),
    }


def generate(contract: PipeStudyContract) -> dict[str, str]:
    plan = mesh_plan(contract)
    files = {**system_files(contract, plan), **constant_files(contract), **initial_fields(contract)}
    return dict(sorted(files.items()))


def case_manifest(contract: PipeStudyContract, files: dict[str, str], tool_versions: dict[str, str]) -> dict[str, Any]:
    plan = mesh_plan(contract)
    return {
        "casegen_version": CASEGEN_VERSION,
        "contract_digest": contract_digest(contract),
        "geometry_digest": contract.domain.geometry_digest,
        "contract": contract.model_dump(mode="json"),
        "derived": {
            "reynolds": contract.reynolds,
            "mean_velocity_m_s": contract.mean_velocity_m_s,
            "flow_rate_m3_s": contract.flow_rate_m3_s,
            "kinematic_viscosity_m2_s": contract.fluid.kinematic_viscosity_m2_s,
            "required_length_m": contract.required_length_m,
            "domain_fraction": DOMAIN_FRACTION,
        },
        "mesh_plan": plan.model_dump(mode="json"),
        "tool_versions": dict(sorted(tool_versions.items())),
        "files": {rel: hashlib.sha256(text.encode("utf-8")).hexdigest() for rel, text in files.items()},
    }


def manifest_digest(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decompose_par_dict(processors: int) -> str:
    """Domain decomposition for an MPI run; scotch needs no geometric input."""
    if not 2 <= processors <= MAX_PROCESSORS:
        raise ValueError(f"parallel runs need 2 to {MAX_PROCESSORS} processors, got {processors}")
    return render("dictionary", "system", "decomposeParDict", {"numberOfSubdomains": processors, "method": "scotch"})


def write_case(contract: PipeStudyContract, case_dir: Path, tool_versions: dict[str, str],
               processors: int = 1) -> dict[str, Any]:
    """Write the case and its manifest; refuses to overwrite a non-empty directory.

    ``processors`` > 1 adds ``system/decomposeParDict`` and records the count in
    the manifest; a serial case and its manifest are unchanged.
    """
    case_dir = Path(case_dir)
    if case_dir.exists() and any(case_dir.iterdir()):
        raise FileExistsError(f"case directory {case_dir} is not empty")
    files = generate(contract)
    if processors != 1:
        files["system/decomposeParDict"] = decompose_par_dict(processors)
    for rel, text in files.items():
        path = case_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    manifest = case_manifest(contract, files, tool_versions)
    if processors != 1:
        manifest["processors"] = processors
    (case_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
