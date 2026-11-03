"""Spec 183: recycle/tear solver methods, convergence truth, diagnostics and operator settings."""

from __future__ import annotations

import copy
import math

import pytest

from app.modules.process_stack import draft, mixed, mixed_runtime, tear_solver
from app.modules.process_stack.draft_models import SetSolver
from tests import tear_qualification_support as qual


def _runner(method: str, max_iterations: int = 25):
    settings = tear_solver.Settings(method=method, max_iterations=max_iterations)
    return lambda initial, evaluate: tear_solver.solve(initial, evaluate, settings=settings)


@pytest.mark.parametrize("method", tear_solver.METHODS)
@pytest.mark.parametrize("budget", [25, 200])
def test_matrix_has_no_false_convergence_or_false_nonconvergence(method: str, budget: int) -> None:
    rows = qual.run_matrix(_runner(method, budget))
    assert [row["case"] for row in rows if row["false_convergence"]] == []
    assert [row["case"] for row in rows if row["false_nonconvergence"]] == []
    # A converged result is always within ten tolerances of a true fixed point; noisy maps only within their floor.
    for row in rows:
        if row["status"] == "completed" and row["family"] != "noisy_vector":
            assert row["true_error_tol_units"] <= 2.0, row


def test_legacy_false_convergences_are_no_longer_reported_converged() -> None:
    rows = {row["case"]: row for row in qual.run_matrix(_runner("direct_substitution"))}
    for case in ("linear q=0.999 seed=1pct", "linear q=0.9999 seed=1pct", "linear q=0.9999 seed=10pct",
                 "plateau seed=10pct"):
        assert rows[case]["status"] == "unconverged"
        assert rows[case]["reason"] == "iteration_budget_insufficient"


def test_broyden_converges_every_linear_contraction_from_every_seed_in_a_few_evaluations() -> None:
    rows = qual.run_matrix(_runner("broyden"))
    linear = [row for row in rows if row["family"] in ("linear_scalar", "linear_vector")]
    assert linear and all(row["status"] == "completed" for row in linear)
    assert max(row["iterations"] for row in linear) <= 4
    ill = [row for row in rows if row["family"] == "ill_conditioned_vector"]
    assert all(row["status"] == "completed" for row in ill)


def test_regime_switch_reports_the_branch_its_seed_selects() -> None:
    rows = {row["case"]: row for row in qual.run_matrix(_runner("broyden"))}
    assert rows["regime-switch seed=productive_seed"]["intended_branch"]
    assert rows["regime-switch seed=near_threshold"]["intended_branch"]
    # Below the threshold the washout branch is a true fixed point: converged, but on that branch.
    assert rows["regime-switch seed=near_zero"]["status"] == "completed"
    assert rows["regime-switch seed=near_zero"]["branch"] == [0.02]


def test_near_neutral_recycle_diagnostics_say_the_budget_cannot_reach_tolerance() -> None:
    case = qual.Case("q", ("mass_flow_kg_s",), lambda x: [1.0 + 0.9994 * (x[0] - 1.0)], [1.0], [1.1],
                     family="linear_scalar", q=0.9994)
    result = tear_solver.solve(qual.state({"mass_flow_kg_s": 1.1}), case.evaluate, settings=tear_solver.Settings())
    diagnostics = result["diagnostics"]
    assert result["status"] == "unconverged"
    assert result["reason"] == "iteration_budget_insufficient"
    assert diagnostics["q_hat"] == pytest.approx(0.9994, abs=1e-6)
    assert diagnostics["classification"] == "near_neutral"
    assert diagnostics["estimate_valid"] is True
    # The estimate is in tolerances at the iterate (1.1 here), the true error in tolerances at x* = 1.
    assert diagnostics["estimated_error_normalized"] * 1.1 == pytest.approx(
        qual.true_error(case, result["last_input"])["normalized"], rel=1e-2)
    assert 15_000 < diagnostics["direct_substitution_iterations_required"] < 25_000
    assert "Broyden" in diagnostics["recommendation"]
    assert diagnostics["max_iterations"] == 25


def test_small_step_on_the_first_evaluation_does_not_converge_without_an_estimate() -> None:
    case = qual.Case("q", ("mass_flow_kg_s",), lambda x: [1.0 + 0.9999 * (x[0] - 1.0)], [1.0], [1.01],
                     family="linear_scalar")
    result = tear_solver.solve(qual.state({"mass_flow_kg_s": 1.01}), case.evaluate,
                               settings=tear_solver.Settings(method="broyden"))
    first = result["history"][0]
    assert first["max_normalized_residual"] <= 1 and first["estimate_valid"] is False
    assert result["status"] == "completed"
    assert qual.true_error(case, result["iterate"])["normalized"] <= 1


def test_exact_fixed_point_converges_in_one_evaluation() -> None:
    result = tear_solver.solve(qual.state({}), lambda guess, _k: copy.deepcopy(guess), settings=tear_solver.Settings())
    assert result["status"] == "completed" and len(result["history"]) == 1


def test_non_finite_evaluation_stops_explicitly() -> None:
    def evaluate(guess, iteration):
        output = copy.deepcopy(guess)
        output[qual.TAG]["mass_flow_kg_s"] = math.nan if iteration == 2 else 0.5 * guess[qual.TAG]["mass_flow_kg_s"]
        return output
    for method in tear_solver.METHODS:
        result = tear_solver.solve(qual.state({"mass_flow_kg_s": 2.0}), evaluate,
                                   settings=tear_solver.Settings(method=method))
        assert result["reason"] == "non_finite_evaluation"
        assert "non_finite_evaluation" in result["history"][-1]["events"]
        assert math.isfinite(result["iterate"][qual.TAG]["mass_flow_kg_s"])


def test_accelerated_steps_stay_physical() -> None:
    # A seed far above the fixed point with a steep map: the raw secant step would make the flow negative.
    def evaluate(guess, _k):
        output = copy.deepcopy(guess)
        x = guess[qual.TAG]["mass_flow_kg_s"]
        output[qual.TAG]["mass_flow_kg_s"] = 0.01 + 0.5 * x * x / (1 + x)
        output[qual.TAG]["mass_fractions"] = {"Water": 0.9, "Salt": 0.1}
        return output
    start = qual.state({"mass_flow_kg_s": 50.0})
    start[qual.TAG]["mass_fractions"] = {"Water": 0.95, "Salt": 0.05}
    result = tear_solver.solve(start, evaluate, settings=tear_solver.Settings(method="broyden", max_iterations=60))
    assert result["status"] == "completed"
    fractions = result["iterate"][qual.TAG]["mass_fractions"]
    assert result["iterate"][qual.TAG]["mass_flow_kg_s"] >= 0
    assert sum(fractions.values()) == pytest.approx(1.0) and min(fractions.values()) >= 0


@pytest.mark.parametrize("method", tear_solver.METHODS)
def test_solver_is_deterministic(method: str) -> None:
    first = qual.run_matrix(_runner(method))
    second = qual.run_matrix(_runner(method))
    strip = [{k: v for k, v in row.items() if k != "wall_s"} for row in first]
    assert strip == [{k: v for k, v in row.items() if k != "wall_s"} for row in second]


def test_mixed_iterate_delegates_and_caller_budget_only_lowers_the_operator_limit() -> None:
    def evaluate(guess, _k):
        output = copy.deepcopy(guess)
        output[qual.TAG]["mass_flow_kg_s"] += 1e-3
        return output
    settings = tear_solver.Settings(max_iterations=40, stop_when_budget_insufficient=False)
    assert len(mixed.iterate(qual.state({}), evaluate, settings=settings)["history"]) == 40
    assert len(mixed.iterate(qual.state({}), evaluate, settings=settings, max_iterations=12)["history"]) == 12
    assert len(mixed.iterate(qual.state({}), evaluate, settings=settings, max_iterations=400)["history"]) == 40


@pytest.mark.parametrize(("raw", "message"), [
    ({"method": "anderson"}, "solver method"),
    ({"max_iterations": 0}, "max_iterations"),
    ({"max_iterations": 5001}, "max_iterations"),
    ({"max_iterations": True}, "max_iterations"),
    ({"damping": 0.0}, "damping"),
    ({"tolerances": {"mass_flow_rel": 0.5}}, "tolerance mass_flow_rel"),
    ({"tolerances": {"speed": 1e-3}}, "unknown tolerance"),
    ({"wegstein_bounds": [0.0, -5.0]}, "wegstein_bounds"),
    ({"wall_s": 5}, "wall_s"),
    ({"seed_mode": "zero"}, "seed_mode"),
    ({"seeds": {"R1": {"volume": 1.0}}}, "seed field"),
    ({"seeds": {"R1": {"mass_flow_kg_s": -1.0}}}, "positive"),
    ({"seeds": {"R1": {"mass_flow_kg_s": math.inf}}}, "finite"),
    ({"surprise": 1}, "unknown solver settings"),
])
def test_settings_are_validated(raw: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        tear_solver.parse_settings(raw)


def test_default_settings_are_the_legacy_controller_limits() -> None:
    settings = tear_solver.parse_settings(None)
    assert (settings.method, settings.max_iterations, settings.damping, settings.wall_s, settings.seed_mode) == (
        "direct_substitution", 25, 1.0, 90.0, "legacy")
    for name, value in (("mass_flow_kg_s", 2.0), ("temperature_K", 300.0), ("pressure_Pa", 1e5),
                        ("mass_fraction.Water", 0.5), ("biomass", 0.3), ("biomass", 0.0)):
        assert settings.tolerance(name, value) == mixed.tolerance(name, value)


def _recycle_document() -> dict:
    culture = {"biomass": {"value": 0.5, "unit": "g/L", "si": 0.5}, "nitrogen": {"value": 20.0, "unit": "mg/L", "si": 0.02}}
    objects = {
        "feed": {"id": "feed", "kind": "stream", "type": "MaterialStream", "tag": "Feed", "source": None,
                 "target": {"unit": "mix", "port": 0},
                 "spec": {"temperature": {"si": 298.15}, "pressure": {"si": 101325.0}, "mass_flow": {"si": 2.0},
                          "composition": {"Water": 1.0}, "culture": culture}},
        "mix": {"id": "mix", "kind": "unit", "type": "Mixer", "tag": "M1", "params": {}, "mode": None},
        "rec": {"id": "rec", "kind": "unit", "type": "Recycle", "tag": "R1", "params": {}, "mode": None},
        "pbr": {"id": "pbr", "kind": "unit", "type": "PhotobioreactorT1", "tag": "PBR1", "params": {}, "mode": None},
        "s1": {"id": "s1", "kind": "stream", "type": "MaterialStream", "tag": "S1",
               "source": {"unit": "rec", "port": 0}, "target": {"unit": "mix", "port": 1}, "spec": {}},
    }
    return {"schema_version": 1, "name": "t", "compounds": ["Water"], "property_package": "Steam Tables (IAPWS-IF97)",
            "objects": objects, "reactions": {}}


def test_seed_modes_and_explicit_seeds() -> None:
    document = _recycle_document()
    part = {"consumed": ["rec"]}
    legacy = mixed_runtime._seed(document, part)["S1"]
    assert legacy["mass_flow_kg_s"] == pytest.approx(2e-6)
    assert legacy["culture"]["biomass"] == 0.0
    feed = mixed_runtime._seed(document, part, tear_solver.parse_settings({"seed_mode": "feed"}))["S1"]
    assert feed["mass_flow_kg_s"] == pytest.approx(2.0)
    assert feed["culture"]["biomass"] == pytest.approx(0.5e-3)
    assert feed["culture"]["phosphorus"] is None  # not declared on the feed: not iterated, as before
    explicit = mixed_runtime._seed(document, part, tear_solver.parse_settings(
        {"seed_mode": "feed", "seeds": {"R1": {"mass_flow_kg_s": 7.5, "temperature_K": 305.0}}}))["S1"]
    assert explicit["mass_flow_kg_s"] == 7.5 and explicit["temperature_K"] == 305.0
    assert explicit["culture"]["biomass"] == feed["culture"]["biomass"]


def test_set_solver_op_validates_stores_and_clears() -> None:
    document = _recycle_document()
    draft.apply_op(document, SetSolver(op="set_solver", solver={"method": "broyden", "max_iterations": 80}))
    assert document["solver"] == {"method": "broyden", "max_iterations": 80}
    with pytest.raises(draft.DraftError) as error:
        draft.apply_op(document, SetSolver(op="set_solver", solver={"method": "magic"}))
    assert error.value.code == "solver_settings_invalid"
    assert document["solver"]["method"] == "broyden"
    draft.apply_op(document, SetSolver(op="set_solver", solver=None))
    assert "solver" not in document


def test_seed_for_a_missing_recycle_is_a_blocker_and_settings_change_the_fingerprint() -> None:
    document = _recycle_document()
    document["solver"] = {"seeds": {"R9": {"mass_flow_kg_s": 1.0}}}
    codes = {item["code"] for item in draft.validate_document(document) if item["severity"] == "blocker"}
    assert "SOLVER_SEED_UNKNOWN_RECYCLE" in codes
    plain = _recycle_document()
    tuned = _recycle_document() | {"solver": {"method": "broyden"}}
    part = {"consumed": [], "segments": [], "levels": {}, "jarvis_units": []}
    assert mixed.fingerprint(plain, part) != mixed.fingerprint(tuned, part)


def test_projection_returns_the_stored_solver_settings_so_the_setup_form_round_trips() -> None:
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state = draft.create_draft(workspace_id, "183 projection")
    assert "solver" not in draft.projection(workspace_id, state["draft_id"])
    settings = {"method": "wegstein", "max_iterations": 60, "seeds": {"Rec": {"mass_flow_kg_s": 0.09}}}
    state = draft.patch(workspace_id, state["draft_id"], state["revision"],
                        [SetSolver(op="set_solver", solver=settings)])
    assert draft.projection(workspace_id, state["draft_id"])["solver"] == settings
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [SetSolver(op="set_solver", solver=None)])
    assert "solver" not in draft.projection(workspace_id, state["draft_id"])

def _real_pbr_recycle(back_fraction: float):
    from app.modules.bio_models import service as bio_models
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        Disconnect,
        DraftQuantity,
        SetUnitModel,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace, pbr_ops, pin_of

    workspace_id = new_workspace()
    parameter_set = bio_models.create_set(workspace_id, "183 recycle parameters")
    for symbol, value, unit in (
        ("K_I", 150.0, "umol/(m**2*s)"), ("K_j_0", 0.001, "kg/m3"), ("k_d", 0.003, "1/hour"),
        ("a", 1.8, "1"), ("b", 0.5, "1"), ("c", 0.1, "1"), ("d", 0.01, "1"),
        ("w_ash", 0.05, "1"), ("k_X", 150.0, "m**2/kg"), ("T_min", 278.15, "K"),
        ("T_opt", 298.15, "K"), ("T_max", 318.15, "K"),
    ):
        parameter_set = bio_models.edit_set_value(
            workspace_id, parameter_set["id"], symbol, {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"])
    card = bio_models.create_card(
        workspace_id, "183 recycle card", parameter_set["id"],
        {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.ctmi",
         "nutrients": ["nutrient.monod"], "combination": "combine.liebig", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"},
        {"value": 0.08, "unit": "1/hour"})
    state = draft.create_draft(workspace_id, "183 recycle")
    culture = {"biomass": (0.2, "kg/m3"), "nitrogen": (0.05, "kg/m3"), "oxygen": (0.0, "kg/m3"),
               "salinity": (35.0, "g/kg")}
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        *pbr_ops(culture=culture, flow_kg_s=0.01),
        SetUnitModel(op="set_unit_model", unit="pbr", model=pin_of(card)),
        AddUnit(op="add_unit", id="mixer", type="Mixer", tag="Mixer", x=50, y=0),
        AddUnit(op="add_unit", id="split", type="Splitter", tag="Split", x=300, y=0),
        AddUnit(op="add_unit", id="rec", type="Recycle", tag="Rec", x=200, y=100),
        AddStream(op="add_stream", id="mixed", tag="Mixed", x=80, y=0),
        AddStream(op="add_stream", id="purge", tag="Purge", x=350, y=0),
        AddStream(op="add_stream", id="back", tag="Back", x=300, y=100),
        AddStream(op="add_stream", id="torn", tag="Torn", x=100, y=100),
        Disconnect(op="disconnect", stream="feed", end="target"),
        Connect(op="connect", stream="feed", end="target", unit="mixer", port=0),
        Connect(op="connect", stream="mixed", end="source", unit="mixer", port=0),
        Connect(op="connect", stream="mixed", end="target", unit="pbr", port=0),
        Connect(op="connect", stream="product", end="target", unit="split", port=0),
        Connect(op="connect", stream="purge", end="source", unit="split", port=0),
        Connect(op="connect", stream="back", end="source", unit="split", port=1),
        Connect(op="connect", stream="back", end="target", unit="rec", port=0),
        Connect(op="connect", stream="torn", end="source", unit="rec", port=0),
        Connect(op="connect", stream="torn", end="target", unit="mixer", port=1),
        SetUnitParams(op="set_unit_params", unit="split", mode="split_ratios", values={
            "split_ratio_1": DraftQuantity(value=1 - back_fraction, unit="dimensionless"),
            "split_ratio_2": DraftQuantity(value=back_fraction, unit="dimensionless")}),
    ])
    return workspace_id, state


@pytest.mark.skipif(not __import__("os").environ.get("JARVISOS_DWSIM_MCP_PATH"),
                    reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_pbr_culture_recycle_with_each_method() -> None:
    import json
    import os

    workspace_id, state = _real_pbr_recycle(0.9)
    blockers = [item for item in draft.validate_document(draft.load_revision(
        draft.draft_dir(workspace_id, state["draft_id"]), state["revision"])["document"], workspace_id)
        if item["severity"] == "blocker"]
    assert blockers == []
    outcomes = {}
    for method, extra in (("direct_substitution", {}), ("direct_substitution", {"max_iterations": 200}),
                          ("broyden", {}), ("wegstein", {"max_iterations": 60})):
        settings = {"method": method, "seed_mode": "feed", "wall_s": 600, **extra}
        state = draft.patch(workspace_id, state["draft_id"], state["revision"],
                            [SetSolver(op="set_solver", solver=settings)])
        run = draft.execute(workspace_id, state["draft_id"], state["revision"], "run")["run"]
        solve = run["mixed_solve"]
        assert solve["method"] == method and solve["solver"]["method"] == method
        assert solve["solver"]["max_iterations"] == settings.get("max_iterations", 25)
        assert solve["diagnostics"]["method"] == method
        outcomes[f"{method}/{settings.get('max_iterations', 25)}"] = {
            "status": run["status"], "reason": solve["reason"], "iterations": len(solve["history"]),
            "diagnostics": solve["diagnostics"], "elapsed_s": solve["elapsed_s"],
            "purge": (run["streams"].get("Purge") or {}).get("mass_flow_kg_s"),
            "back": (run["streams"].get("Back") or {}).get("mass_flow_kg_s"),
            "balances_passed": all(row.get("passed", True) for row in solve.get("balances", {}).values())}
    evidence = os.environ.get("JARVISOS_183_EVIDENCE")
    if evidence:
        with open(evidence, "w") as handle:
            json.dump(outcomes, handle, indent=1, default=str)
    broyden = outcomes["broyden/25"]
    assert broyden["status"] == "completed", broyden
    assert broyden["balances_passed"]
    assert broyden["purge"] == pytest.approx(0.01, rel=1e-3)
    assert broyden["back"] == pytest.approx(0.09, rel=1e-3)
    for key, outcome in outcomes.items():
        # Every method either converges truthfully or names why it stopped; none converges on the wrong flows.
        if outcome["status"] == "completed":
            assert outcome["purge"] == pytest.approx(0.01, rel=1e-3), key
        else:
            assert outcome["reason"] in {"iteration_budget_insufficient", "max_iterations"}, (key, outcome)
