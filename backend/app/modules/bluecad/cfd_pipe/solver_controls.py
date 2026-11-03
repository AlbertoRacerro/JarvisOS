"""Analytic laminar solver controls for the pinned spec 216 OpenFOAM toolchain.

Two rectangular ``blockMesh`` cases reuse the generated pipe case's ``fvSchemes``
and ``fvSolution`` so they exercise exactly the solver settings a typed pipe study
gets. Each has an exact fully developed laminar answer:

* ``square_duct`` — four no-slip walls (walls in both transverse directions),
  Poiseuille number f·Re = 56.908 (Shah & London). It detects the pinned-v2412
  temporary-tensor transpose defect, which adds ~22% here when not worked around.
* ``half_channel`` — one no-slip wall and symmetry planes, dp/dx = 3 μ U / h².
  It has no ∂u/∂z, so it passes with or without the defect and guards the
  baseline discretisation.

The controls are qualification diagnostics of the toolchain, not pipe studies,
and they never publish a reduced model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.bluecad.cfd_pipe.casegen import mesh_plan, system_files
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract
from app.modules.bluecad.cfd_pipe.foam_dict import fmt_scalar, render
from app.modules.bluecad.cfd_pipe.probe_results import ProbeResultError, parse_probes

ControlName = Literal["square_duct", "half_channel"]
CONTROL_NAMES: tuple[ControlName, ...] = ("square_duct", "half_channel")
SQUARE_DUCT_POISEUILLE = 56.908
TOLERANCE = 0.02
LENGTH_M = 0.2
SIDE_M = 0.01
MEAN_VELOCITY_M_S = 0.01
ITERATIONS = 300
PRESSURE_STATIONS_M = (0.13, 0.17)


class ControlComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control: ControlName
    iteration: int
    measured_gradient_pa_m: float
    analytic_gradient_pa_m: float
    relative_error: float
    tolerance: float
    passed: bool


def _reference_settings(contract: PipeStudyContract) -> dict[str, str]:
    files = system_files(contract, mesh_plan(contract))
    return {rel: files[rel] for rel in ("system/fvSchemes", "system/fvSolution")}


def _geometry(control: ControlName) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[int, int, int]]:
    if control == "square_duct":
        half = SIDE_M / 2.0
        return (0.0, -half, -half), (LENGTH_M, half, half), (40, 16, 16)
    half = SIDE_M / 2.0
    return (0.0, 0.0, 0.0), (LENGTH_M, half, half), (80, 16, 16)


def _patches(control: ControlName) -> dict[str, tuple[str, tuple[tuple[int, int, int, int], ...]]]:
    inlet_outlet = {"inlet": ("patch", ((0, 4, 7, 3),)), "outlet": ("patch", ((1, 2, 6, 5),))}
    if control == "square_duct":
        return {**inlet_outlet, "wall": ("wall", ((3, 7, 6, 2), (4, 5, 6, 7), (0, 3, 2, 1), (0, 1, 5, 4)))}
    return {**inlet_outlet, "wall": ("wall", ((3, 7, 6, 2),)),
            "symmetry_y": ("symmetryPlane", ((0, 1, 5, 4),)),
            "symmetry_z": ("symmetry", ((0, 3, 2, 1), (4, 5, 6, 7)))}


def flow_rate_m3_s(control: ControlName) -> float:
    low, high, _ = _geometry(control)
    return MEAN_VELOCITY_M_S * (high[1] - low[1]) * (high[2] - low[2])


def analytic_gradient_pa_m(control: ControlName, contract: PipeStudyContract) -> float:
    mu = contract.fluid.dynamic_viscosity_pa_s
    if control == "square_duct":
        return SQUARE_DUCT_POISEUILLE * mu * MEAN_VELOCITY_M_S / (2.0 * SIDE_M**2)
    half_height = SIDE_M / 2.0
    return 3.0 * mu * MEAN_VELOCITY_M_S / half_height**2


def probe_locations(control: ControlName) -> tuple[tuple[float, float, float], ...]:
    low, _, _ = _geometry(control)
    y = low[1] + 0.0005 if control == "half_channel" else 0.0005
    return tuple((x, y, 0.0005) for x in PRESSURE_STATIONS_M)


def generate_control(control: ControlName, contract: PipeStudyContract) -> dict[str, str]:
    """Complete control case from typed values, reusing the pipe case's solver dictionaries."""
    if contract.regime != "laminar":
        raise ValueError("solver controls use the laminar pipe case's solver settings")
    low, high, cells = _geometry(control)
    vertices = tuple((x, y, z) for z in (low[2], high[2]) for (x, y) in
                     ((low[0], low[1]), (high[0], low[1]), (high[0], high[1]), (low[0], high[1])))
    patches = _patches(control)
    block_mesh = {
        "scale": 1,
        "vertices": list(vertices),
        "blocks": [f"hex (0 1 2 3 4 5 6 7) ({cells[0]} {cells[1]} {cells[2]}) simpleGrading (1 1 1)"],
        "edges": [],
        "boundary": [{name: {"type": kind, "faces": list(faces)}} for name, (kind, faces) in patches.items()],
    }
    control_dict = {
        "application": "simpleFoam", "startFrom": "startTime", "startTime": 0, "stopAt": "endTime",
        "endTime": ITERATIONS, "deltaT": 1, "writeControl": "timeStep", "writeInterval": ITERATIONS,
        "purgeWrite": 1, "writeFormat": "ascii", "writePrecision": 12, "writeCompression": "off",
        "timeFormat": "general", "timePrecision": 8, "runTimeModifiable": "true",
        "functions": {"control_probes": {
            "type": "probes", "libs": ('"libsampling.so"',), "interpolationScheme": "cellPoint", "writeControl": "timeStep",
            "writeInterval": ITERATIONS, "fields": ("p",), "probeLocations": probe_locations(control),
        }},
    }
    speed = fmt_scalar(MEAN_VELOCITY_M_S)
    symmetric = {name: {"type": kind} for name, (kind, _) in patches.items() if kind.startswith("symmetry")}
    velocity = {
        "dimensions": "[0 1 -1 0 0 0 0]", "internalField": f"uniform ({speed} 0 0)",
        "boundaryField": {
            "inlet": {"type": "flowRateInletVelocity", "volumetricFlowRate": f"constant {fmt_scalar(flow_rate_m3_s(control))}",
                      "value": f"uniform ({speed} 0 0)"},
            "outlet": {"type": "inletOutlet", "inletValue": "uniform (0 0 0)",
                       "value": f"uniform ({speed} 0 0)"},
            "wall": {"type": "noSlip"}, **symmetric,
        },
    }
    pressure = {
        "dimensions": "[0 2 -2 0 0 0 0]", "internalField": "uniform 0",
        "boundaryField": {"inlet": {"type": "zeroGradient"}, "outlet": {"type": "fixedValue", "value": "uniform 0"},
                          "wall": {"type": "zeroGradient"}, **symmetric},
    }
    transport = {"transportModel": "Newtonian", "nu": contract.fluid.kinematic_viscosity_m2_s}
    return {
        **_reference_settings(contract),
        "system/blockMeshDict": render("dictionary", "system", "blockMeshDict", block_mesh),
        "system/controlDict": render("dictionary", "system", "controlDict", control_dict),
        "0/U": render("volVectorField", "0", "U", velocity),
        "0/p": render("volScalarField", "0", "p", pressure),
        "constant/transportProperties": render("dictionary", "constant", "transportProperties", transport),
        "constant/turbulenceProperties": render("dictionary", "constant", "turbulenceProperties",
                                                {"simulationType": "laminar"}),
    }


def compare_control(control: ControlName, contract: PipeStudyContract, pressure_text: str) -> ControlComparison:
    sample = parse_probes(pressure_text, vectors=False)
    expected = probe_locations(control)
    if len(sample.locations) != len(expected) or any(
        abs(a - b) > 1e-9 for got, want in zip(sample.locations, expected, strict=True) for a, b in zip(got, want, strict=True)
    ):
        raise ProbeResultError("control probe locations do not match the generated control")
    if sample.iteration != ITERATIONS:
        raise ProbeResultError("control probe output is not from the final iteration")
    if not all(isinstance(value, float) for value in sample.values):
        raise ProbeResultError("control pressure probes must be scalar")
    upstream, downstream = (float(value) for value in sample.values if isinstance(value, float))
    measured = (upstream - downstream) / (PRESSURE_STATIONS_M[1] - PRESSURE_STATIONS_M[0]) * contract.fluid.density_kg_m3
    analytic = analytic_gradient_pa_m(control, contract)
    error = (measured - analytic) / analytic
    return ControlComparison(control=control, iteration=sample.iteration, measured_gradient_pa_m=measured,
                             analytic_gradient_pa_m=analytic, relative_error=error, tolerance=TOLERANCE,
                             passed=abs(error) <= TOLERANCE)
