from __future__ import annotations

import math

import pytest

from app.modules.bio_models import forms
from app.modules.photobioreactor.pbr_evaluator import _cardinal_temperature


def test_light_analytic_limits_and_monotonicity() -> None:
    assert forms.light_monod(4, 4) == 0.5
    optimum = math.sqrt(4 * 16)
    assert forms.light_haldane(optimum, 4, 16) == pytest.approx(0.5)
    assert forms.light_steele(0, 10) == 0
    assert forms.light_steele(10, 10) == 1
    assert forms.light_eilers_peeters_steady(10, 10, 0.7) == 1
    assert [forms.light_monod(i, 3) for i in (0, 1, 3, 9)] == sorted(forms.light_monod(i, 3) for i in (0, 1, 3, 9))
    assert forms.light_haldane(optimum, 4, 16) >= forms.light_haldane(4, 4, 16)
    assert forms.light_haldane(optimum, 4, 16) >= forms.light_haldane(2 * optimum, 4, 16)
    with pytest.raises(forms.FormRefusal, match="K_I"):
        forms.light_monod(2, 0)
    with pytest.raises(forms.FormRefusal, match="I"):
        forms.light_haldane(-1, 2, 3)


def test_slab_limits_and_noncommuting_nonlinear_average() -> None:
    assert forms.slab_mean_irradiance(100, 0, 4, 1) == 100
    assert forms.slab_mean_irradiance(100, 1e-14, 2, 1) == pytest.approx(100, abs=1e-11)
    average = forms.slab_response_average(forms.light_monod, 100, 2, 1, 1, K_I=20)
    at_mean = forms.light_monod(forms.slab_mean_irradiance(100, 2, 1, 1), 20)
    assert average != pytest.approx(at_mean, abs=1e-5)
    with pytest.raises(forms.FormRefusal, match="L"):
        forms.slab_mean_irradiance(10, 0, 0, 0)


def test_temperature_nutrient_and_loss_limits() -> None:
    assert forms.temperature_isothermal(280) == 1
    args = (285.0, 295.0, 305.0, 315.0)
    assert forms.temperature_ctmi(args[1], *args[1:]) == 0
    assert forms.temperature_ctmi(305, 285, 305, 315) == pytest.approx(1)
    assert forms.temperature_ctmi(315, 285, 305, 315) == 0
    for t in (286, 295, 305, 314):
        assert forms.temperature_ctmi(t, 285, 305, 315) == pytest.approx(_cardinal_temperature(t, 285, 305, 315))
    assert forms.temperature_arrhenius_ref(298, 298, 40000) == 1
    assert forms.nutrient_monod(2, 2) == 0.5
    assert forms.nutrient_droop(0.1, 0.1) == 0
    assert forms.nutrient_droop(0.2, 0.1) == 0.5
    assert forms.combine_liebig([0.4, 0.7]) == 0.4
    assert forms.combine_multiplicative([0.4, 0.7]) == pytest.approx(0.28)
    assert forms.loss_light_dark(10, 10, 0.1, 0.2) == 0.2
    assert forms.loss_first_order(0.03) == 0.03
    with pytest.raises(forms.FormRefusal, match="T_min"):
        forms.temperature_ctmi(300, 310, 300, 320)


def test_stoichiometry_closure_and_unique_coefficients() -> None:
    nh3 = forms.stoich_photoautotrophic(1.8, 0.5, 0.1, 0.01, 0.05, "NH3")
    hno3 = forms.stoich_photoautotrophic(1.8, 0.5, 0.1, 0.01, 0.05, "HNO3")
    assert set(nh3["coefficients_mol_per_C_mol"]) == {"CO2", "NH3", "H3PO4", "H2O", "O2"}
    assert set(hno3["coefficients_mol_per_C_mol"]) == {"CO2", "HNO3", "H3PO4", "H2O", "O2"}
    for result in (nh3, hno3):
        assert max(abs(value) for value in result["elemental_residuals_mol"].values()) < 1e-12
        assert all(math.isfinite(value) for value in result["yields"].values())
    assert nh3["coefficients_mol_per_C_mol"] != hno3["coefficients_mol_per_C_mol"]
    assert nh3["coefficients_mol_per_C_mol"]["H2O"] == pytest.approx(0.735)
    assert hno3["coefficients_mol_per_C_mol"]["H2O"] == pytest.approx(0.835)
    assert nh3["coefficients_mol_per_C_mol"]["O2"] == pytest.approx(-1.1375)
    assert hno3["coefficients_mol_per_C_mol"]["O2"] == pytest.approx(-1.3375)
    assert nh3["yields"]["O2_produced_kg_per_kg_total_dry"] == pytest.approx(1.4691879849621887)
    assert hno3["yields"]["O2_produced_kg_per_kg_total_dry"] == pytest.approx(1.727506751548947)
    assert nh3["coefficients_mol_per_C_mol"]["H2O"] > 0  # consumed; O2 is a negative product
    # Independent atom table/count check: positive coefficients are reactants.
    atom_table = {"CO2": {"C": 1, "O": 2}, "NH3": {"N": 1, "H": 3}, "HNO3": {"H": 1, "N": 1, "O": 3},
                  "H3PO4": {"H": 3, "P": 1, "O": 4}, "H2O": {"H": 2, "O": 1}, "O2": {"O": 2},
                  "biomass": {"C": 1, "H": 1.8, "O": 0.5, "N": 0.1, "P": 0.01}}
    for result in (nh3, hno3):
        signed = {**result["coefficients_mol_per_C_mol"], "biomass": -1.0}
        for element in ("C", "H", "O", "N", "P"):
            residual = sum(coefficient * atom_table[name].get(element, 0) for name, coefficient in signed.items())
            assert residual == pytest.approx(0, abs=1e-12)


def test_declared_form_domains_refuse_invalid_inputs() -> None:
    invalid_calls = [
        lambda: forms.light_steele(-1, 2),
        lambda: forms.light_eilers_peeters_steady(1, 2, -1),
        lambda: forms.temperature_arrhenius_ref(0, 298, 1000),
        lambda: forms.temperature_ctmi(-1, 280, 300, 320),
        lambda: forms.nutrient_monod(-1, 1),
        lambda: forms.nutrient_droop(1, 0),
        lambda: forms.combine_liebig([]),
        lambda: forms.combine_multiplicative([1.1]),
        lambda: forms.loss_first_order(-0.1),
        lambda: forms.loss_light_dark(1, 0, -0.1, 0.1),
        lambda: forms.stoich_photoautotrophic(1, 1, 1, 1, 1, "bad"),
    ]
    for call in invalid_calls:
        with pytest.raises(forms.FormRefusal):
            call()


def test_form_cards_use_allowlisted_typed_mathml() -> None:
    allowed = {"math", "mtext", "mrow", "mi", "mn", "mo", "msup", "msub", "mfrac", "msqrt"}
    def visit(node: dict) -> None:
        assert node["tag"] in allowed
        for child in node.get("children", []):
            visit(child)
    cards = {card["id"]: card for card in forms.FORM_CARDS}
    assert {card["version"] for card in cards.values()} == {forms.FORM_VERSION}
    for card in cards.values():
        visit(card["equation"])
        assert card["equation"]["tag"] == "math"
        assert card["equation_text"]
        tags = _math_tags(card["equation"])
        assert "mrow" in tags and "mi" in tags and "mo" in tags
    assert {"mfrac", "msub", "msup", "mn"} <= {
        tag for card in cards.values() for tag in _math_tags(card["equation"])
    }
    haldane = cards["light.haldane"]
    assert haldane["equation"]["children"][0]["children"][2]["tag"] == "mfrac"


def test_form_card_equation_names_its_published_left_hand_side() -> None:
    def lhs(card: dict) -> list[str]:
        row = card["equation"]["children"][0]["children"]
        return [node.get("text", "") for node in row[:2]]
    cards = {card["id"]: card for card in forms.FORM_CARDS}
    assert lhs(cards["optics.cylinder_beam_diffuse_response_average"]) == ["⟨f⟩", "="]
    assert lhs(cards["optics.slab_mean_irradiance"]) == ["Ī", "="]
    assert lhs(cards["nutrient.monod"]) == ["f_j", "="]
    assert lhs(cards["combine.liebig"]) == ["f_S", "="]
    assert lhs(cards["loss.first_order"]) == ["r", "="]
    # The published right-hand side is not repeated after the rendered "lhs =".
    loss = cards["loss.first_order"]["equation"]["children"][0]["children"]
    assert [node.get("text") for node in loss[2]["children"]] == ["k_d"]
    stoich = cards["stoich.photoautotrophic"]["equation"]["children"][0]["children"]
    assert all(node.get("text") != "equation" for node in stoich)


def _math_tags(node: dict) -> set[str]:
    return {node["tag"], *(tag for child in node.get("children", []) for tag in _math_tags(child))}


@pytest.mark.parametrize("response,mode,parameters", [
    (forms.light_monod, "monod", {"K_I": 210.0}),
    (forms.light_haldane, "haldane", {"K_I": 210.0, "K_i": 900.0}),
])
def test_slab_response_matches_107_growth_for_light(response, mode: str, parameters: dict[str, float]) -> None:
    from app.modules.photobioreactor.pbr_evaluator import _rhs

    x = {"photoperiod": 12., "peak_par": 700., "specific_light_extinction": 110., "tube_inner_diameter": 0.045,
         "light_saturation_constant": 210., "light_inhibition_constant": 900., "max_specific_growth_rate": 0.08,
         "temperature_mean": 298.15, "temperature_amplitude": 0.4, "kinetics_reference_temperature": 298.15,
         "temperature_min": 285., "temperature_opt": 298.15,
         "temperature_max": 315., "nitrogen_half_saturation": 0.02, "biomass_loss_rate": 0.003,
         "biomass_nitrogen_fraction": 0.08, "oxygen_kla": 0.1, "oxygen_saturation": 0.01, "oxygen_yield": 1.2}
    for thermal_mode in ("cardinal", "isothermal"):
        modes = {"light_response": mode, "temperature_response": thermal_mode, "nitrogen_response": "monod"}
        for hour, biomass, nitrogen in ((12., 0.7, 0.1), (9., 0.4, 0.03), (7., 0.9, 0.15), (18., 0.5, 0.0)):
            if thermal_mode == "isothermal":
                x["temperature_amplitude"] = 0.
            growth_with_factors = _rhs(x, modes)(hour, (biomass, nitrogen, 0.01, 0.0))[0] / biomass + x["biomass_loss_rate"]
            sunrise = 12. - x["photoperiod"] / 2.
            surface = (x["peak_par"] * math.sin(math.pi * (hour - sunrise) / x["photoperiod"])
                       if sunrise < hour < sunrise + x["photoperiod"] else 0.)
            average = forms.slab_response_average(response, surface, x["specific_light_extinction"], biomass,
                                                  x["tube_inner_diameter"], **parameters)
            temp = x["temperature_mean"] + x["temperature_amplitude"] * math.sin(2. * math.pi * (hour - 9.) / 24.)
            thermal = forms.temperature_ctmi(temp, x["temperature_min"], x["temperature_opt"], x["temperature_max"]) if thermal_mode == "cardinal" else 1.
            expected = (x["max_specific_growth_rate"] * thermal * average
                        * forms.nutrient_monod(nitrogen, x["nitrogen_half_saturation"]))
            assert growth_with_factors == pytest.approx(expected, abs=1e-12)


def test_kinetics_explanation_is_derived_from_form_metadata() -> None:
    card = forms.form_card("nutrient.monod")
    message = forms.kinetics_explanation()
    assert card["applies_to"] in message
    assert "select the reactor and open Kinetics" in message
    assert "not a reaction rate law" not in message
    assert "a Monod or Haldane rate law is a typed reactor reaction" in message
    assert "Photobioreactor (T1) uses a pinned biological model card" in message
    assert "An explanation alone proposes no change" in message
