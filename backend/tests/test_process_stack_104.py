"""104 PROCESS-STACK-STRANGLER-1: selected upstream evaluators behind the 106 boundary.

Reference numbers are the independently recorded 103 qualification results in
scripts/benchmarks/process_upstream/results/, not values derived from this code.
"""

from __future__ import annotations

import builtins
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.modules.engineering.evaluator_contracts import (
    EngineeringEvaluator,
    EvaluationRequest,
    EvaluationResult,
    validate_evaluation_result,
)
from app.modules.process_stack.correlations import InternalConvectionEvaluator, PipePressureDropEvaluator
from app.modules.process_stack.dynamics import OdeInputError, integrate_ode
from app.modules.process_stack.properties import CoolPropPropertyEvaluator

WS = "main"
STATE = {"authority_owner": "process_stack", "object_type": "material_state", "object_id": "water-25C",
         "workspace_id": WS, "revision": "1"}
MODEL = {**STATE, "object_type": "dynamic_model", "object_id": "pbr"}
WATER_PIPE = {"density": (997.0, "kg/m3"), "dynamic_viscosity": (0.00089, "Pa*s"), "velocity": (1.0, "m/s"),
              "diameter": (50.0, "mm"), "length": (10.0, "m"), "roughness": (0.0, "m")}


def _request(evaluator: EngineeringEvaluator, inputs: dict[str, tuple[float, str]], *,
             subject: dict[str, str] = STATE, options: dict[str, Any] | None = None,
             deadline: timedelta = timedelta(minutes=5)) -> EvaluationRequest:
    now = datetime.now(UTC)
    return EvaluationRequest.model_validate({
        "request_ref": {"authority_owner": "process_stack", "object_type": "evaluation_request",
                        "object_id": "req-1", "workspace_id": WS, "revision": "1"},
        "evaluator_id": evaluator.descriptor().evaluator_id,
        "subject_ref": subject,
        "inputs": [{"name": name, "value": {"value": value, "unit": unit}} for name, (value, unit) in inputs.items()],
        "backend_options": options or {},
        "requested_at": now - timedelta(seconds=1),
        "deadline_at": now + deadline,
    })


def _run(evaluator: EngineeringEvaluator, request: EvaluationRequest) -> EvaluationResult:
    result = evaluator.evaluate(request)
    validate_evaluation_result(request, result)
    return result


def _outputs(result: EvaluationResult) -> dict[str, tuple[float, str]]:
    assert result.status == "succeeded", result.failure
    return {item.name: (item.value.value, item.value.unit) for item in result.outputs}


def _failure(result: EvaluationResult) -> tuple[str, str, str]:
    assert result.failure is not None and not result.outputs
    return result.status, result.failure.category, result.failure.backend_code


def test_coolprop_water_matches_103_evidence_and_converts_units() -> None:
    evaluator = CoolPropPropertyEvaluator()
    assert evaluator.availability().state == "available"
    result = _run(evaluator, _request(evaluator, {"temperature": (25.0, "degC"), "pressure": (1.01325, "bar")}))
    out = _outputs(result)
    assert out["density"] == (pytest.approx(997.047636760347, rel=1e-12), "kg/m3")  # 103 CoolProp.json
    assert out["dynamic_viscosity"][0] == pytest.approx(0.000890022489, rel=1e-8)
    assert out["thermal_conductivity"][1] == "W/(m*K)" and out["specific_heat_cp"][1] == "J/(kg*K)"
    assert result.fidelity == "steady_state_detailed" and result.numerical.converged is True
    assert result.backend_version == evaluator.descriptor().backend_version != "not_installed"


@pytest.mark.parametrize(("inputs", "options", "subject", "expected"), [
    ({"temperature": (5000.0, "K"), "pressure": (101325.0, "Pa")}, None, STATE,
     ("refused", "outside_validity_domain", "coolprop_eos_range")),
    ({"temperature": (298.15, "K"), "pressure": (101325.0, "Pa")}, {"fluid": "Brine"}, STATE,
     ("refused", "unsupported_request", "option_unsupported")),
    ({"temperature": (298.15, "K"), "pressure": (101325.0, "Pa")}, {"fluid": "Water", "backend": "REFPROP"}, STATE,
     ("refused", "unsupported_request", "option_unsupported")),
    ({"temperature": (298.15, "K")}, None, STATE, ("refused", "invalid_input", "input_names_invalid")),
    ({"temperature": (298.15, "m"), "pressure": (101325.0, "Pa")}, None, STATE,
     ("refused", "invalid_input", "unit_dimension_mismatch")),
    ({"temperature": (298.15, "K"), "pressure": (101325.0, "Pa")}, None, MODEL,
     ("refused", "unsupported_request", "subject_unsupported")),
])
def test_coolprop_refuses_instead_of_extrapolating(
    inputs: dict[str, tuple[float, str]], options: dict[str, Any] | None, subject: dict[str, str],
    expected: tuple[str, str, str],
) -> None:
    evaluator = CoolPropPropertyEvaluator()
    assert _failure(_run(evaluator, _request(evaluator, inputs, options=options, subject=subject))) == expected


def test_deadline_mismatch_and_missing_backend_are_typed_results(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = CoolPropPropertyEvaluator()
    inputs = {"temperature": (298.15, "K"), "pressure": (101325.0, "Pa")}
    expired = _request(evaluator, inputs, deadline=timedelta(seconds=-0.5))
    assert _failure(_run(evaluator, expired)) == ("deadline_exceeded", "timeout", "deadline_passed")
    wrong = _request(PipePressureDropEvaluator(), inputs)
    assert _failure(evaluator.evaluate(wrong)) == ("refused", "unsupported_request", "evaluator_mismatch")

    real_import = builtins.__import__

    def no_coolprop(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("CoolProp"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_coolprop)
    availability = evaluator.availability()
    assert (availability.state, availability.reason_code) == ("not_installed", "CoolProp.CoolProp_not_installed")
    assert _failure(_run(evaluator, _request(evaluator, inputs))) == (
        "refused", "not_available", "CoolProp.CoolProp_not_installed",
    )


def test_pipe_pressure_drop_matches_103_evidence_and_differs_from_blasius_fit() -> None:
    evaluator = PipePressureDropEvaluator()
    out = _outputs(_run(evaluator, _request(evaluator, WATER_PIPE)))
    assert out["reynolds_number"][0] == pytest.approx(56011.23595505618, rel=1e-12)
    assert out["darcy_friction_factor"][0] == pytest.approx(0.02037186401881335, rel=1e-12)  # 103 fluids.json
    assert out["pressure_drop"] == (pytest.approx(2031.0748426756911, rel=1e-12), "Pa")
    # Smooth-pipe Blasius fit f = 0.3164 Re^-0.25 (the older screening correlation) for the same 10 m x 50 mm pipe.
    incumbent = 0.3164 * out["reynolds_number"][0] ** -0.25 * (10.0 / 0.05) * 997.0 * 1.0**2 / 2.0
    # The Colebrook evaluator and the Blasius fit differ by ~0.948% here.
    assert (out["pressure_drop"][0] - incumbent) / incumbent == pytest.approx(-0.009480379035224617, rel=1e-9)


def test_pipe_regimes_laminar_transitional_and_roughness() -> None:
    evaluator = PipePressureDropEvaluator()
    laminar = {**WATER_PIPE, "velocity": (0.01, "m/s")}  # Re ~ 560
    out = _outputs(_run(evaluator, _request(evaluator, laminar)))
    assert out["darcy_friction_factor"][0] == pytest.approx(64.0 / out["reynolds_number"][0], rel=1e-12)
    transitional = {**WATER_PIPE, "velocity": (0.05, "m/s")}  # Re ~ 2800
    assert _failure(_run(evaluator, _request(evaluator, transitional)))[2] == "transitional_regime"
    rough = {**WATER_PIPE, "roughness": (5.0, "mm")}
    assert _failure(_run(evaluator, _request(evaluator, rough)))[2] == "relative_roughness_range"
    zero_velocity = {**WATER_PIPE, "velocity": (0.0, "m/s")}
    assert _failure(_run(evaluator, _request(evaluator, zero_velocity)))[1:] == ("invalid_input", "input_non_positive")


def test_internal_convection_methods_and_validity_ranges() -> None:
    evaluator = InternalConvectionEvaluator()
    base = {"reynolds_number": (56011.23596, "1"), "prandtl_number": (6.2, "1"),
            "thermal_conductivity": (0.6, "W/(m*K)"), "diameter": (0.05, "m")}
    dittus = _outputs(_run(evaluator, _request(evaluator, base, options={"method": "dittus_boelter_heating"})))
    assert dittus["nusselt_number"][0] == pytest.approx(300.12670249791796, rel=1e-12)  # 103 Windows+WSL2 smoke
    assert dittus["heat_transfer_coefficient"] == (pytest.approx(300.12670249791796 * 0.6 / 0.05, rel=1e-12),
                                                   "W/(m**2*K)")
    gnielinski = _outputs(_run(evaluator, _request(evaluator, base)))
    assert 0.8 < gnielinski["nusselt_number"][0] / dittus["nusselt_number"][0] < 1.25
    cooling = _outputs(_run(evaluator, _request(evaluator, base, options={"method": "dittus_boelter_cooling"})))
    assert cooling["nusselt_number"][0] < dittus["nusselt_number"][0]
    low_re = {**base, "reynolds_number": (5000.0, "1")}
    assert _failure(_run(evaluator, _request(evaluator, low_re, options={"method": "dittus_boelter_heating"})))[1:] == (
        "outside_validity_domain", "dittus_boelter_heating_range")
    assert _outputs(_run(evaluator, _request(evaluator, low_re)))["nusselt_number"][0] > 0  # Gnielinski covers it
    oil = {**base, "prandtl_number": (5000.0, "1")}
    assert _failure(_run(evaluator, _request(evaluator, oil)))[2] == "gnielinski_range"


def _day_night(t: float, y: tuple[float, ...]) -> list[float]:
    light = max(0.0, math.sin(2 * math.pi * (t % 24) / 24))
    return [(0.08 * light - 0.025) * y[0]]


def test_cvode_owner_reproduces_103_day_night_case_and_analytic_decay() -> None:
    hours = [i * 0.5 for i in range(97)]
    solution = integrate_ode(_day_night, [1.0], hours, rtol=1e-9, atol=1e-11)
    assert solution.success and solution.diagnostics.converged is True
    assert solution.times == tuple(hours)
    assert solution.states[-1][0] == pytest.approx(1.022560700726125, rel=1e-7)  # 103 scikit-sundae.json
    decay = integrate_ode(lambda t, y: [-y[0], -2.0 * y[1]], [1.0, 1.0], [0.0, 1.0, 2.0], rtol=1e-10, atol=1e-12)
    assert decay.states[-1] == (pytest.approx(math.exp(-2.0), rel=1e-7), pytest.approx(math.exp(-4.0), rel=1e-7))
    assert decay.diagnostics.iterations and decay.diagnostics.iterations > 0


def test_cvode_owner_rejects_bad_problems_and_reports_solver_failure() -> None:
    with pytest.raises(OdeInputError):
        integrate_ode(lambda t, y: [0.0], [1.0], [0.0, 0.0])
    with pytest.raises(OdeInputError):
        integrate_ode(lambda t, y: [0.0], [math.nan], [0.0, 1.0])
    with pytest.raises(OdeInputError):
        integrate_ode(lambda t, y: [0.0, 0.0], [1.0], [0.0, 1.0])
    failed = integrate_ode(lambda t, y: [math.nan], [1.0], [0.0, 1.0])
    assert not failed.success and failed.states == () and failed.diagnostics.converged is False
    assert failed.message
