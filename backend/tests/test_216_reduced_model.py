"""Spec 216 reduced-model fitting, publication guards, evaluation range and obsolescence (offline)."""

from __future__ import annotations

import pytest

from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract
from app.modules.bluecad.cfd_pipe.domain import derive_fluid_domain
from app.modules.bluecad.cfd_pipe.mesh_study import three_mesh_study
from app.modules.bluecad.cfd_pipe.reduced_model import (
    Discretization,
    PublicationRefused,
    ReducedModelError,
    StudyPoint,
    Tolerances,
    evaluate,
    fit_power_law,
    obsolescence,
    publish,
    reference_darcy,
    study_template_digest,
    verify_version,
)
from app.modules.bluecad.cfd_pipe.references import pressure_gradient_pa_m
from app.modules.bluecad.template import TubeTemplateCreate, template_geometry_spec

NU = 1.002e-3 / 998.2
TOL = Tolerances(laminar=0.02, turbulent_rans=0.10)


def tube(length_mm: float = 2600.0, outer_d_mm: float = 50.0) -> dict:
    return template_geometry_spec(TubeTemplateCreate.model_validate(
        {"template": "tube", "params": {"outer_d_mm": outer_d_mm, "wall_t_mm": 2.0, "length_mm": length_mm}}))


def turbulent(re: float, **overrides) -> PipeStudyContract:
    payload = {"domain": derive_fluid_domain(tube()), "inlet": {"mean_velocity_m_s": re * NU / 0.046},
               "regime": "turbulent_rans", "turbulence_model": "kOmegaSST", "wall_treatment": "low_re_resolved",
               "mesh_level": 1}
    return PipeStudyContract.model_validate({**payload, **overrides})


def laminar(re: float, **overrides) -> PipeStudyContract:
    payload = {"domain": derive_fluid_domain(tube(1000.0, 14.0)), "inlet": {"mean_velocity_m_s": re * NU / 0.01},
               "regime": "laminar", "wall_treatment": "resolved", "mesh_level": 1}
    return PipeStudyContract.model_validate({**payload, **overrides})


def point(contract: PipeStudyContract, error: float = 0.0, **overrides) -> StudyPoint:
    darcy = reference_darcy(contract.regime, contract.reynolds) * (1.0 + error)
    dpdl = pressure_gradient_pa_m(darcy, contract.fluid.density_kg_m3, contract.mean_velocity_m_s, contract.diameter_m)
    payload = {"run_id": f"run-{contract.regime}-{contract.reynolds:.0f}", "contract": contract,
               "case_manifest_digest": "sha256:" + "0" * 64, "status": "measured", "residual_converged": True,
               "pressure_gradient_pa_m": dpdl, "tool_versions": {"openfoam_simplefoam": "2412"}}
    return StudyPoint.model_validate({**payload, **overrides})


def turbulent_mesh() -> Discretization:
    # The measured C1 three-mesh study (cycle 84): L2/L1/L0 Pa/m with r21 1.491, r32 1.500.
    study = three_mesh_study((55.99108585951357, 55.8861340447833, 55.642746085032904), (1.4908676731354447, 1.5))
    return Discretization(regime="turbulent_rans", reynolds=20621.5, study=study, point_level_value=55.8861340447833,
                          run_ids=("fine", "medium", "coarse"),
                          case_manifest_digests=("fine-manifest", "medium-manifest", "coarse-manifest"))


def c1_model():
    points = [point(turbulent(re), err) for re, err in ((1e4, -0.02), (2e4, -0.01), (2.8e4, -0.005))]
    return publish("c1_tube.darcy", points, [turbulent_mesh()], TOL)


def test_power_law_fit_recovers_hagen_poiseuille_exactly() -> None:
    a, b = fit_power_law([(re, 64.0 / re) for re in (100.0, 500.0, 1000.0)])
    assert a == pytest.approx(64.0, rel=1e-12)
    assert b == pytest.approx(-1.0, rel=1e-12)
    with pytest.raises(ReducedModelError):
        fit_power_law([(500.0, 0.128), (500.0, 0.128)])


def test_published_model_states_validity_uncertainty_and_provenance() -> None:
    model = c1_model()
    assert verify_version(model)
    (segment,) = model.segments
    assert segment.regime == "turbulent_rans" and segment.reference == "colebrook_smooth"
    assert segment.validity.reynolds_min == pytest.approx(1e4, rel=1e-9)
    assert segment.validity.reynolds_max == pytest.approx(2.8e4, rel=1e-9)
    assert segment.validity.diameter_m == pytest.approx(0.046)
    u = segment.uncertainty
    # Points sit on L1, 0.33% below the Richardson value: 1.25 x 0.33% exceeds the 0.185% fine GCI.
    assert u.discretization == pytest.approx(1.25 * (56.0738600425038 - 55.8861340447833) / 56.0738600425038)
    assert u.correlation == pytest.approx(0.02)
    assert 0 < u.fit < 0.01
    assert u.combined == pytest.approx(u.discretization + u.fit + u.correlation)
    assert model.geometry_digest == turbulent(2e4).domain.geometry_digest
    assert len(model.provenance["contract_digests"]) == 3
    assert model.provenance["tool_versions"] == ("openfoam_simplefoam=2412",)
    assert model.provenance["mesh_study_run_ids"] == (
        "turbulent_rans:fine", "turbulent_rans:medium", "turbulent_rans:coarse")


def test_version_identity_is_deterministic_and_tamper_evident() -> None:
    first, second = c1_model(), c1_model()
    assert first.version_id == second.version_id
    tampered = first.model_copy(update={"model_id": "other"})
    assert not verify_version(tampered)
    assert obsolescence(tampered, geometry_digest=first.geometry_digest, study_templates={})


def test_publication_refuses_failed_unconverged_and_out_of_tolerance_runs() -> None:
    good = [point(turbulent(1e4)), point(turbulent(2.8e4))]
    cases = [
        ([point(turbulent(1e4), status="failed"), good[1]], "not measured"),
        ([point(turbulent(1e4), residual_converged=False), good[1]], "residual target"),
        ([point(turbulent(1e4), -0.12), good[1]], "beyond the stated ±10%"),
        ([good[0], point(turbulent(2.8e4), tool_versions={"openfoam_simplefoam": "2413"})],
         "one recorded toolchain"),
        ([good[0], point(turbulent(2.8e4), tool_versions={})], "one recorded toolchain"),
        ([good[0]], "two or more Reynolds numbers"),
        ([good[0], point(turbulent(2.8e4, turbulence_model="kEpsilon", wall_treatment="wall_function"))],
         "differ in fluid, model or boundary"),
        ([good[0], point(turbulent(2.8e4, domain=derive_fluid_domain(tube(2700.0))))], "tube geometries"),
        ([], "no study runs"),
    ]
    for points, reason in cases:
        with pytest.raises(PublicationRefused) as exc:
            publish("m", points, [turbulent_mesh()], TOL)
        assert any(reason in r for r in exc.value.reasons), (reason, exc.value.reasons)
    with pytest.raises(PublicationRefused, match="no three-mesh study"):
        publish("m", good, [], TOL)


def test_publication_refuses_a_non_monotonic_mesh_study() -> None:
    oscillating = three_mesh_study((1.0, 1.1, 0.9), 1.5)
    evidence = Discretization(regime="turbulent_rans", reynolds=2e4, study=oscillating, point_level_value=1.1,
                              run_ids=("fine", "medium", "coarse"),
                              case_manifest_digests=("fine-manifest", "medium-manifest", "coarse-manifest"))
    with pytest.raises(PublicationRefused, match="oscillatory"):
        publish("m", [point(turbulent(1e4)), point(turbulent(2.8e4))], [evidence], TOL)


def test_publication_refuses_mixed_toolchains_across_regimes() -> None:
    domain = laminar(100.0).domain
    points = [point(turbulent(re, domain=domain, inlet={"mean_velocity_m_s": re * NU / 0.01}))
              for re in (1e4, 2.8e4)] + [
        point(laminar(re), tool_versions={"openfoam_simplefoam": "2413"})
        for re in (100.0, 200.0)
    ]
    with pytest.raises(PublicationRefused, match="one recorded toolchain"):
        publish("m", points, [turbulent_mesh()], TOL)


def test_laminar_segment_reproduces_hagen_poiseuille() -> None:
    study = three_mesh_study((3.2122, 3.2154, 3.2208), 1.5)
    evidence = Discretization(regime="laminar", reynolds=100.0, study=study, point_level_value=3.2154,
                              run_ids=("fine", "medium", "coarse"),
                              case_manifest_digests=("fine-manifest", "medium-manifest", "coarse-manifest"))
    model = publish("lam", [point(laminar(re), 0.003) for re in (100, 300, 600)], [evidence], TOL)
    (segment,) = model.segments
    assert segment.b == pytest.approx(-1.0, abs=1e-9)
    assert segment.a == pytest.approx(64.0 * 1.003, rel=1e-9)
    assert segment.uncertainty.fit == pytest.approx(0.0, abs=1e-12)


def test_evaluation_inside_range_matches_the_fit_and_states_uncertainty() -> None:
    model = c1_model()
    result = evaluate(model, reynolds=2e4, diameter_m=0.046, density_kg_m3=998.2, dynamic_viscosity_pa_s=1.002e-3)
    assert result.status == "in_range" and not result.violations
    seg = model.segments[0]
    assert result.darcy == pytest.approx(seg.a * 2e4**seg.b)
    velocity = 2e4 * NU / 0.046
    assert result.pressure_gradient_pa_m == pytest.approx(result.darcy * 998.2 * velocity**2 / (2 * 0.046))
    assert result.wall_shear_pa == pytest.approx(result.pressure_gradient_pa_m * 0.046 / 4)
    assert result.relative_uncertainty == seg.uncertainty.combined


def test_evaluation_outside_validity_refuses_or_warns_visibly() -> None:
    model = c1_model()
    props = {"density_kg_m3": 998.2, "dynamic_viscosity_pa_s": 1.002e-3}
    refused = evaluate(model, reynolds=5e4, diameter_m=0.046, **props)
    assert refused.status == "refused" and refused.darcy is None
    assert "outside the validated range" in refused.violations[0]
    warned = evaluate(model, reynolds=5e4, diameter_m=0.046, out_of_range="warn", **props)
    assert warned.status == "out_of_range_warned" and warned.darcy is not None
    assert warned.relative_uncertainty is None
    other_bore = evaluate(model, reynolds=2e4, diameter_m=0.04, **props)
    assert other_bore.status == "refused" and "diameter" in other_bore.violations[0]
    transitional = evaluate(model, reynolds=3000, diameter_m=0.046, **props)
    assert transitional.status == "refused"


def test_obsolescence_after_geometry_or_study_input_change() -> None:
    model = c1_model()
    template = study_template_digest(turbulent(2e4))
    assert study_template_digest(turbulent(2.5e4, mesh_level=2)) == template
    current = {"turbulent_rans": template}
    assert obsolescence(model, geometry_digest=model.geometry_digest, study_templates=current) == ()
    longer = derive_fluid_domain(tube(2700.0)).geometry_digest
    assert "tube geometry changed" in obsolescence(model, geometry_digest=longer, study_templates=current)[0]
    warmer = study_template_digest(turbulent(2e4, fluid={"dynamic_viscosity_pa_s": 0.89e-3}))
    reasons = obsolescence(model, geometry_digest=model.geometry_digest, study_templates={"turbulent_rans": warmer})
    assert reasons == ("turbulent_rans study inputs changed since publication; rerun the study",)


def test_inlet_turbulence_settings_are_fixed_study_inputs() -> None:
    base = turbulent(2e4)
    changed = turbulent(2.8e4, inlet={
        "mean_velocity_m_s": 2.8e4 * NU / 0.046,
        "turbulence_intensity": 0.10,
        "mixing_length_over_d": 0.08,
    })
    assert study_template_digest(changed) != study_template_digest(base)
    with pytest.raises(PublicationRefused, match="differ in fluid, model or boundary"):
        publish("m", [point(base), point(changed)], [turbulent_mesh()], TOL)
    model = c1_model()
    assert obsolescence(model, geometry_digest=model.geometry_digest,
                        study_templates={"turbulent_rans": study_template_digest(changed)}) == (
        "turbulent_rans study inputs changed since publication; rerun the study",)
