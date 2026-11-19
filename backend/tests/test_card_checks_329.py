"""Spec 329: correlation and property cards record VALIDITY_RANGE and FINITE_OUTPUTS on succeeded runs."""

from __future__ import annotations

import math

import pytest
from test_process_stack_104 import WATER_PIPE, _failure, _request, _run

from app.modules.engineering.evaluator_contracts import EngineeringEvaluator, EvaluationResult, result_verdict
from app.modules.process_stack.correlations import (
    TURBULENT_RE_MIN,
    InternalConvectionEvaluator,
    PipePressureDropEvaluator,
)
from app.modules.process_stack.properties import CoolPropPropertyEvaluator

PIPE = PipePressureDropEvaluator()
CONVECTION = InternalConvectionEvaluator()
COOLPROP = CoolPropPropertyEvaluator()
CONVECTION_INPUTS = {"reynolds_number": (56011.23596, "1"), "prandtl_number": (6.2, "1"),
                     "thermal_conductivity": (0.6, "W/(m*K)"), "diameter": (0.05, "m")}
WATER_STATE = {"temperature": (300.0, "K"), "pressure": (101325.0, "Pa")}
CASES = (
    (PIPE, WATER_PIPE, "reynolds_number"),
    (CONVECTION, CONVECTION_INPUTS, "reynolds_number"),
    (COOLPROP, WATER_STATE, "temperature"),
)
IDS = [case[0].descriptor().evaluator_id for case in CASES]


def _checks(result: EvaluationResult) -> dict[str, object]:
    return {check.id: check for check in result.checks}


@pytest.mark.parametrize(("evaluator", "inputs", "governing"), CASES, ids=IDS)
def test_default_run_is_usable_with_both_checks(
    evaluator: EngineeringEvaluator, inputs: dict[str, tuple[float, str]], governing: str
) -> None:
    descriptor = evaluator.descriptor()
    assert descriptor.required_checks == ("VALIDITY_RANGE", "FINITE_OUTPUTS")
    result = _run(evaluator, _request(evaluator, inputs))
    checks = _checks(result)
    assert set(checks) == {"VALIDITY_RANGE", "FINITE_OUTPUTS"}
    assert all(check.status == "pass" for check in checks.values())  # type: ignore[attr-defined]
    validity = checks["VALIDITY_RANGE"]
    assert validity.family == "input" and validity.source == f"constant:{descriptor.evaluator_id}"  # type: ignore[attr-defined]
    expected = {item.name: item.value.value for item in result.outputs}.get(governing, inputs.get(governing, (0,))[0])
    assert validity.actual == pytest.approx(expected)  # type: ignore[attr-defined]
    assert checks["FINITE_OUTPUTS"].family == "numerical" and checks["FINITE_OUTPUTS"].source == "physical_limit"  # type: ignore[attr-defined]
    assert result_verdict(descriptor, result) == "usable"


def test_pipe_limit_is_the_nearest_enforced_bound() -> None:
    turbulent = _checks(_run(PIPE, _request(PIPE, WATER_PIPE)))["VALIDITY_RANGE"]
    assert turbulent.limit == TURBULENT_RE_MIN  # type: ignore[attr-defined]
    laminar = _checks(_run(PIPE, _request(PIPE, {**WATER_PIPE, "velocity": (0.01, "m/s")})))["VALIDITY_RANGE"]
    assert laminar.actual < laminar.limit  # type: ignore[attr-defined]


def test_convection_and_coolprop_limits_come_from_the_enforced_range() -> None:
    near_low = _checks(_run(CONVECTION, _request(CONVECTION, {**CONVECTION_INPUTS, "reynolds_number": (3500.0, "1")})))
    assert near_low["VALIDITY_RANGE"].limit == 3.0e3  # type: ignore[attr-defined]
    heating = _run(CONVECTION, _request(CONVECTION, CONVECTION_INPUTS, options={"method": "dittus_boelter_heating"}))
    assert _checks(heating)["VALIDITY_RANGE"].limit == 1.0e4  # type: ignore[attr-defined]
    from CoolProp.CoolProp import PropsSI

    t_min, t_max = PropsSI("Tmin", "Water"), PropsSI("Tmax", "Water")
    low = _checks(_run(COOLPROP, _request(COOLPROP, {**WATER_STATE, "temperature": (t_min + 1.0, "K")})))
    assert low["VALIDITY_RANGE"].limit == pytest.approx(t_min)  # type: ignore[attr-defined]
    high = _checks(_run(COOLPROP, _request(COOLPROP, {**WATER_STATE, "temperature": (t_max - 100.0, "K"), "pressure": (1e7, "Pa")})))
    assert high["VALIDITY_RANGE"].limit == pytest.approx(t_max)  # type: ignore[attr-defined]


def test_out_of_range_inputs_are_still_refused_without_checks() -> None:
    cases = (
        (PIPE, _request(PIPE, {**WATER_PIPE, "velocity": (0.05, "m/s")}), "transitional_regime"),
        (PIPE, _request(PIPE, {**WATER_PIPE, "roughness": (5.0, "mm")}), "relative_roughness_range"),
        (CONVECTION, _request(CONVECTION, {**CONVECTION_INPUTS, "prandtl_number": (5000.0, "1")}), "gnielinski_range"),
        (COOLPROP, _request(COOLPROP, {**WATER_STATE, "temperature": (10.0, "K")}), "coolprop_eos_range"),
    )
    for evaluator, request, code in cases:
        result = _run(evaluator, request)
        assert _failure(result)[2] == code
        assert result.checks == ()
        assert result_verdict(evaluator.descriptor(), result) == "not_usable"


def test_non_finite_library_output_is_not_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    import CoolProp.CoolProp
    import fluids.friction
    import ht.conv_internal

    nan = lambda *args, **kwargs: math.nan  # noqa: E731
    monkeypatch.setattr(fluids.friction, "friction_factor", nan)
    monkeypatch.setattr(ht.conv_internal, "turbulent_Gnielinski", nan)
    monkeypatch.setattr(CoolProp.CoolProp, "PropsSI", lambda key, *args: 1.0 if len(args) == 1 else math.nan)
    monkeypatch.setattr(CoolProp.CoolProp, "PhaseSI", lambda *args: "liquid")
    for evaluator, inputs, _governing in CASES:
        result = evaluator.evaluate(_request(evaluator, inputs))
        assert result.status != "succeeded"
        assert not any(check.status == "pass" for check in result.checks if check.id == "FINITE_OUTPUTS")
        assert result_verdict(evaluator.descriptor(), result) == "not_usable"
