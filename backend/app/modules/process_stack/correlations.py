"""Pipe friction (fluids) and internal convection (ht) correlations (103 role: correlations).

Correlations are screening fidelity: empirical fits with published validity
ranges, enforced here as refusals instead of silent extrapolation.
"""

from __future__ import annotations

from typing import Final

from app.modules.engineering.evaluator_contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
)
from app.modules.process_stack._common import (
    EvaluationRefusal,
    descriptor_version,
    evaluate_with,
    import_availability,
    option,
    quantities,
    required_inputs,
)

PIPE_EVALUATOR_ID: Final = "fluids.pipe_pressure_drop"
CONVECTION_EVALUATOR_ID: Final = "ht.internal_convection"
TURBULENT_RE_MIN: Final = 4000.0  # below this and above fluids' laminar limit the regime is transitional
MAX_RELATIVE_ROUGHNESS: Final = 0.05  # Colebrook/Moody chart range
# Incropera & DeWitt ranges: (Re_min, Re_max, Pr_min, Pr_max)
CONVECTION_METHODS: Final = {
    "gnielinski": (3.0e3, 5.0e6, 0.5, 2000.0),
    "dittus_boelter_heating": (1.0e4, float("inf"), 0.6, 160.0),
    "dittus_boelter_cooling": (1.0e4, float("inf"), 0.6, 160.0),
}


class PipePressureDropEvaluator:
    """Darcy-Weisbach pressure drop with the Clamond solution of Colebrook (laminar: 64/Re)."""

    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(
            evaluator_id=PIPE_EVALUATOR_ID, backend_kind="specialist", backend_name="fluids",
            backend_version=descriptor_version("fluids"), fidelity="screening",
            capabilities=("pipe_friction", "darcy_weisbach"),
        )

    def availability(self) -> EvaluatorAvailability:
        return import_availability(PIPE_EVALUATOR_ID, "fluids", lambda: descriptor_version("fluids"))

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        return evaluate_with(self, request, lambda: _pipe(request))


class InternalConvectionEvaluator:
    """Fully developed turbulent internal-flow Nusselt number and film coefficient."""

    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(
            evaluator_id=CONVECTION_EVALUATOR_ID, backend_kind="specialist", backend_name="ht",
            backend_version=descriptor_version("ht"), fidelity="screening",
            capabilities=("internal_convection", *CONVECTION_METHODS),
        )

    def availability(self) -> EvaluatorAvailability:
        return import_availability(CONVECTION_EVALUATOR_ID, "ht", lambda: descriptor_version("ht"))

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        return evaluate_with(self, request, lambda: _convection(request))


def _positive(values: dict[str, float]) -> None:
    bad = sorted(name for name, value in values.items() if value <= 0.0)
    if bad:
        raise EvaluationRefusal("invalid_input", "input_non_positive", f"must be positive: {bad}")


def darcy_pipe_pressure_drop(
    *, density: float, dynamic_viscosity: float, velocity: float,
    diameter: float, length: float, roughness: float = 0.0,
) -> tuple[float, float, float]:
    """Return (Reynolds number, Darcy friction factor, pressure drop in Pa) for a straight pipe, SI units.

    Laminar: 64/Re. Turbulent: Clamond solution of Colebrook. Refuses (EvaluationRefusal) the
    transitional regime and relative roughness above the Moody chart range.
    """
    from fluids.core import K_from_f, Reynolds, dP_from_K
    from fluids.friction import LAMINAR_TRANSITION_PIPE, friction_factor

    _positive({"density": density, "dynamic_viscosity": dynamic_viscosity, "velocity": velocity,
               "diameter": diameter, "length": length})
    if roughness < 0.0:
        raise EvaluationRefusal("invalid_input", "input_negative", "roughness must be non-negative")
    reynolds = float(Reynolds(V=velocity, D=diameter, rho=density, mu=dynamic_viscosity))
    relative_roughness = roughness / diameter
    if LAMINAR_TRANSITION_PIPE <= reynolds < TURBULENT_RE_MIN:
        raise EvaluationRefusal("outside_validity_domain", "transitional_regime",
                                f"Re={reynolds:.6g} is between laminar and turbulent correlations")
    if relative_roughness > MAX_RELATIVE_ROUGHNESS:
        raise EvaluationRefusal("outside_validity_domain", "relative_roughness_range",
                                f"e/D={relative_roughness:.6g} > {MAX_RELATIVE_ROUGHNESS}")
    darcy = float(friction_factor(Re=reynolds, eD=relative_roughness, Method="Clamond"))
    pressure_drop = float(dP_from_K(K=K_from_f(fd=darcy, L=length, D=diameter), rho=density, V=velocity))
    return reynolds, darcy, pressure_drop


def _pipe(request: EvaluationRequest) -> tuple[tuple[NamedQuantity, ...], NumericalDiagnostics]:
    option(request, "method", ("Clamond",), "Clamond")
    x = required_inputs(request, {
        "density": "kg/m3", "dynamic_viscosity": "Pa*s", "velocity": "m/s",
        "diameter": "m", "length": "m", "roughness": "m",
    })
    reynolds, darcy, pressure_drop = darcy_pipe_pressure_drop(
        density=x["density"], dynamic_viscosity=x["dynamic_viscosity"], velocity=x["velocity"],
        diameter=x["diameter"], length=x["length"], roughness=x["roughness"],
    )
    return quantities({
        "reynolds_number": (reynolds, "1"), "relative_roughness": (x["roughness"] / x["diameter"], "1"),
        "darcy_friction_factor": (darcy, "1"), "pressure_drop": (pressure_drop, "Pa"),
    }), NumericalDiagnostics(converged=True)


def _convection(request: EvaluationRequest) -> tuple[tuple[NamedQuantity, ...], NumericalDiagnostics]:
    from fluids.friction import friction_factor
    from ht.conv_internal import turbulent_Dittus_Boelter, turbulent_Gnielinski

    method = option(request, "method", tuple(CONVECTION_METHODS), "gnielinski")
    x = required_inputs(request, {
        "reynolds_number": "1", "prandtl_number": "1", "thermal_conductivity": "W/(m*K)", "diameter": "m",
    })
    _positive(x)
    re_min, re_max, pr_min, pr_max = CONVECTION_METHODS[method]
    reynolds, prandtl = x["reynolds_number"], x["prandtl_number"]
    if not (re_min <= reynolds <= re_max and pr_min <= prandtl <= pr_max):
        raise EvaluationRefusal("outside_validity_domain", f"{method}_range",
                                f"Re={reynolds:.6g}, Pr={prandtl:.6g} outside {method} range")
    if method == "gnielinski":
        nusselt = float(turbulent_Gnielinski(Re=reynolds, Pr=prandtl, fd=friction_factor(Re=reynolds, eD=0.0)))
    else:
        nusselt = float(turbulent_Dittus_Boelter(Re=reynolds, Pr=prandtl, heating=method.endswith("heating")))
    return quantities({
        "nusselt_number": (nusselt, "1"),
        "heat_transfer_coefficient": (nusselt * x["thermal_conductivity"] / x["diameter"], "W/(m**2*K)"),
    }), NumericalDiagnostics(converged=True)
