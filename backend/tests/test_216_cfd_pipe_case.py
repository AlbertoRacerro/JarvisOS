"""Spec 216 offline tests: fluid domain, study contract, case generation, mesh check, residuals."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.bluecad.cfd_pipe import casegen, references, solver_controls
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract, contract_digest
from app.modules.bluecad.cfd_pipe.domain import FluidDomainError, derive_fluid_domain
from app.modules.bluecad.cfd_pipe.foam_dict import FoamDictError, fmt_scalar, render
from app.modules.bluecad.cfd_pipe.mesh_check import (
    CheckMeshReport,
    MeshCheckParseError,
    evaluate_mesh,
    parse_checkmesh,
)
from app.modules.bluecad.cfd_pipe.mesh_study import (
    MeshStudyError,
    refinement_ratio,
    refinement_ratios,
    three_mesh_study,
)
from app.modules.bluecad.cfd_pipe.probe_results import (
    ProbeResultError,
    laminar_comparison,
    parse_probes,
    turbulent_comparison,
)
from app.modules.bluecad.cfd_pipe.residuals import (
    LogParseError,
    ResidualStream,
    converged,
    parse_log,
    required_fields,
)
from app.modules.bluecad.template import TubeTemplateCreate, template_geometry_spec

FIXTURES = Path(__file__).parent / "fixtures" / "cfd_pipe"
REPO = Path(__file__).resolve().parents[2]


def tube_spec(outer_d_mm: float = 50.0, wall_t_mm: float = 2.0, length_mm: float = 2600.0) -> dict:
    return template_geometry_spec(TubeTemplateCreate.model_validate(
        {"template": "tube", "params": {"outer_d_mm": outer_d_mm, "wall_t_mm": wall_t_mm, "length_mm": length_mm}}
    ))


def laminar(**overrides) -> PipeStudyContract:
    payload = {
        "domain": derive_fluid_domain(tube_spec(14.0, 2.0, 1000.0)),
        "inlet": {"mean_velocity_m_s": 0.05}, "regime": "laminar", "wall_treatment": "resolved",
    }
    return PipeStudyContract.model_validate({**payload, **overrides})


def c1_turbulent(**overrides) -> PipeStudyContract:
    payload = {
        "domain": derive_fluid_domain(tube_spec()), "inlet": {"mean_velocity_m_s": 0.45},
        "regime": "turbulent_rans", "turbulence_model": "kOmegaSST", "wall_treatment": "low_re_resolved", "mesh_level": 0,
    }
    return PipeStudyContract.model_validate({**payload, **overrides})


# --- fluid domain -------------------------------------------------------------------------


def test_fluid_domain_is_the_bore_and_digest_tracks_geometry() -> None:
    domain = derive_fluid_domain(tube_spec())
    assert domain.inner_diameter_m == pytest.approx(0.046)
    assert domain.length_m == pytest.approx(2.6)
    assert domain.patches == ("inlet", "outlet", "wall")
    assert derive_fluid_domain(tube_spec()).geometry_digest == domain.geometry_digest
    assert derive_fluid_domain(tube_spec(length_mm=2100.0)).geometry_digest != domain.geometry_digest
    assert derive_fluid_domain(tube_spec(wall_t_mm=3.0)).geometry_digest != domain.geometry_digest


def test_fluid_domain_refuses_non_tube_geometry() -> None:
    spec = tube_spec()
    two_parts = {**spec, "parts": [spec["parts"][0], {**spec["parts"][0], "part_id": "second"}]}
    two_parts.pop("spec_id")
    with pytest.raises(FluidDomainError) as exc:
        derive_fluid_domain(two_parts)
    assert exc.value.code == "geometry_unsupported"
    bend = {"spec_version": "bluecad_geometry_spec_v0_1", "name": "bend", "connections": [], "parts": [
        {"part_id": "b", "kind": "bend", "params": {"outer_d": 50.0, "wall_t": 2.0, "bend_radius": 100.0, "angle": 90.0}}]}
    with pytest.raises(FluidDomainError) as exc:
        derive_fluid_domain(bend)
    assert exc.value.code == "geometry_unsupported"
    with pytest.raises(FluidDomainError) as exc:
        derive_fluid_domain({"spec_version": "nope"})
    assert exc.value.code == "geometry_invalid"


# --- contract -----------------------------------------------------------------------------


def test_contract_derives_c1_reynolds_and_flow_rate_equivalence() -> None:
    by_velocity = c1_turbulent()
    assert by_velocity.reynolds == pytest.approx(0.45 * 0.046 / (1.002e-3 / 998.2))
    assert 2.0e4 < by_velocity.reynolds < 2.1e4
    by_flow = c1_turbulent(inlet={"flow_rate_m3_s": by_velocity.flow_rate_m3_s})
    assert by_flow.mean_velocity_m_s == pytest.approx(0.45)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"inlet": {}}, "exactly one"),
        ({"inlet": {"mean_velocity_m_s": 0.45, "flow_rate_m3_s": 1e-3}}, "exactly one"),
        ({"regime": "laminar", "turbulence_model": None, "wall_treatment": "resolved"}, "laminar regime requested at Re"),
        ({"turbulence_model": None}, "requires a turbulence model"),
        ({"wall_treatment": "resolved"}, "low_re_resolved"),
        ({"turbulence_model": "kEpsilon"}, "only with wall functions"),
        ({"inlet": {"mean_velocity_m_s": 0.05}}, "turbulent_rans requested at Re"),
        ({"domain": derive_fluid_domain(tube_spec(length_mm=1000.0))}, "developed flow"),
        ({"outlet_pressure_pa": float("inf")}, "finite"),
        ({"mesh_level": 5}, "less than or equal"),
        ({"solver": {"wall_time_limit_s": 59}}, "greater than or equal to 60"),
        ({"solver": {"wall_time_limit_s": 172801}}, "less than or equal to 172800"),
        ({"unknown": 1}, "Extra inputs"),
    ],
)
def test_contract_refuses_inconsistent_studies(overrides: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        c1_turbulent(**overrides)


def test_laminar_contract_refuses_model_and_wall_treatment() -> None:
    with pytest.raises(ValidationError, match="no turbulence model"):
        laminar(turbulence_model="kOmegaSST")
    with pytest.raises(ValidationError, match="'resolved'"):
        laminar(wall_treatment="wall_function")
    with pytest.raises(ValidationError, match="Re=2491 > 2000"):
        laminar(inlet={"mean_velocity_m_s": 0.25})


def test_contract_digest_is_stable_and_input_sensitive() -> None:
    assert contract_digest(c1_turbulent()) == contract_digest(c1_turbulent())
    assert contract_digest(c1_turbulent(mesh_level=1)) != contract_digest(c1_turbulent())
    assert contract_digest(c1_turbulent(fluid={"density_kg_m3": 1000.0})) != contract_digest(c1_turbulent())
    assert contract_digest(c1_turbulent(solver={"wall_time_limit_s": 7200})) != contract_digest(c1_turbulent())


def test_coarse_example_mesh_levels_keep_three_mesh_refinement_gate() -> None:
    from app.modules.bluecad.cfd_pipe.mesh_study import refinement_ratios

    cells = [casegen.mesh_plan(c1_turbulent(mesh_level=level)).cell_count for level in (-2, -1, 0)]
    assert max(cells) == 158_200
    assert min(refinement_ratios((cells[2], cells[1], cells[0]))) >= 1.3


def test_parallel_case_adds_only_the_decomposition_and_records_processors(tmp_path: Path) -> None:
    contract = c1_turbulent()
    pins = {"openfoam_simplefoam": "v2412"}
    serial = casegen.write_case(contract, tmp_path / "serial", pins)
    parallel = casegen.write_case(contract, tmp_path / "parallel", pins, processors=6)
    assert "processors" not in serial and parallel["processors"] == 6
    assert set(parallel["files"]) - set(serial["files"]) == {"system/decomposeParDict"}
    assert {k: v for k, v in parallel["files"].items() if k in serial["files"]} == serial["files"]
    text = (tmp_path / "parallel" / "system" / "decomposeParDict").read_text()
    assert "numberOfSubdomains 6;" in text and "method scotch;" in text
    with pytest.raises(ValueError, match="2 to 64"):
        casegen.decompose_par_dict(1)


# --- references ---------------------------------------------------------------------------


def test_references_match_textbook_values() -> None:
    assert references.darcy_laminar(1000) == pytest.approx(0.064)
    assert references.darcy_blasius(1e5) == pytest.approx(0.01777, rel=1e-3)
    # Moody smooth-pipe value at Re = 1e5 is 0.0180.
    assert references.darcy_colebrook_smooth(1e5) == pytest.approx(0.0180, rel=5e-3)
    # Darcy-Weisbach with f = 64/Re is Hagen-Poiseuille 32 mu U / D^2.
    mu = 998.2 * 0.05 * 0.01 / 500
    gradient = references.pressure_gradient_pa_m(references.darcy_laminar(500), 998.2, 0.05, 0.01)
    assert gradient == pytest.approx(32 * mu * 0.05 / 0.01**2)
    assert references.poiseuille_velocity(0.0, 1.0, 1.0) == 2.0
    with pytest.raises(ValueError):
        references.darcy_laminar(0)


# --- foam dictionary serializer -----------------------------------------------------------


@pytest.mark.parametrize("value", ["a;b", "#include \"x\"", "$U", "x // y", "a{b}", 'odd"', "a\nb"])
def test_foam_dict_rejects_unsafe_tokens(value: str) -> None:
    with pytest.raises(FoamDictError):
        fmt_scalar(value)
    with pytest.raises(FoamDictError):
        render("dictionary", "system", "x", {value: 1})


def test_foam_dict_rejects_ambiguous_and_non_finite_values() -> None:
    for value in (True, float("nan"), float("inf"), None, object()):
        with pytest.raises(FoamDictError):
            fmt_scalar(value)


# --- case generation ----------------------------------------------------------------------


def test_case_generation_is_deterministic_and_complete(tmp_path: Path) -> None:
    contract = c1_turbulent()
    files = casegen.generate(contract)
    assert files == casegen.generate(c1_turbulent())
    assert sorted(files) == [
        "0/U", "0/k", "0/nut", "0/omega", "0/p", "constant/transportProperties",
        "constant/turbulenceProperties", "system/blockMeshDict", "system/controlDict",
        "system/fvSchemes", "system/fvSolution",
    ]
    first = casegen.write_case(contract, tmp_path / "a", {"openfoam_simplefoam": "v2412"})
    second = casegen.write_case(contract, tmp_path / "b", {"openfoam_simplefoam": "v2412"})
    assert first == second
    assert casegen.manifest_digest(first) == casegen.manifest_digest(second)
    on_disk = json.loads((tmp_path / "a" / casegen.MANIFEST_NAME).read_text())
    assert on_disk == first
    assert first["geometry_digest"] == contract.domain.geometry_digest
    assert first["contract_digest"] == contract_digest(contract)
    assert first["tool_versions"] == {"openfoam_simplefoam": "v2412"}
    for rel, digest in first["files"].items():
        assert hashlib.sha256((tmp_path / "a" / rel).read_bytes()).hexdigest() == digest
    other = casegen.write_case(contract, tmp_path / "c", {"openfoam_simplefoam": "v2506"})
    assert casegen.manifest_digest(other) != casegen.manifest_digest(first)
    with pytest.raises(FileExistsError):
        casegen.write_case(contract, tmp_path / "a", {})


def test_laminar_case_has_no_turbulence_fields_and_regime_dictionaries() -> None:
    files = casegen.generate(laminar())
    assert set(files) >= {"0/U", "0/p"} and not {"0/k", "0/omega", "0/epsilon", "0/nut"} & set(files)
    assert "simulationType laminar;" in files["constant/turbulenceProperties"]
    kepsilon = casegen.generate(c1_turbulent(turbulence_model="kEpsilon", wall_treatment="wall_function"))
    assert "0/epsilon" in kepsilon and "0/omega" not in kepsilon
    assert "kqRWallFunction" in kepsilon["0/k"] and "nutkWallFunction" in kepsilon["0/nut"]
    sst = casegen.generate(c1_turbulent())
    assert "nutLowReWallFunction" in sst["0/nut"] and "RASModel kOmegaSST;" in sst["constant/turbulenceProperties"]


def test_case_values_come_from_contract() -> None:
    contract = c1_turbulent(outlet_pressure_pa=998.2)
    files = casegen.generate(contract)
    assert "uniform (0.45 0 0)" in files["0/U"]
    assert "uniform 1;" in files["0/p"]  # kinematic: 998.2 Pa / 998.2 kg/m3
    assert f"nu {contract.fluid.kinematic_viscosity_m2_s:.12g};" in files["constant/transportProperties"]
    for text in files.values():
        assert "#" not in text.split("\n", 1)[1] and "$" not in text
    # No solver-side residualControl; the runner judges convergence and stops via controlDict.
    assert "residualControl" not in files["system/fvSolution"]
    assert "runTimeModifiable true;" in files["system/controlDict"]
    assert "type probes;" in files["system/controlDict"]
    assert "probeLocations" in files["system/controlDict"]
    stop = casegen.control_dict(contract, stop_now=True)
    assert "stopAt writeNow;" in stop and "stopAt endTime;" in files["system/controlDict"]


def test_quarter_pipe_geometry_and_exact_flow_rate() -> None:
    contract = c1_turbulent()
    files = casegen.generate(contract)
    mesh = casegen.block_mesh_dict(contract, casegen.mesh_plan(contract))
    assert len(mesh["vertices"]) == 14 and len(mesh["blocks"]) == 3 and len(mesh["edges"]) == 4
    assert {next(iter(item)) for item in mesh["boundary"]} == {"inlet", "outlet", "wall", "symmetry_y", "symmetry_z"}
    wall_points = [mesh["vertices"][index] for index in (4, 5, 6, 11, 12, 13)]
    assert all(math.hypot(y, z) == pytest.approx(0.023, rel=1e-12) for _, y, z in wall_points)
    for field in ("0/U", "0/p", "0/k", "0/omega", "0/nut"):
        assert files[field].count("type symmetryPlane;") == 2
    # A quarter of the exact contract flow rate, independent of the faceted wall area.
    assert f"volumetricFlowRate constant {contract.flow_rate_m3_s / 4:.12g};" in files["0/U"]
    assert "flowRateInletVelocity" in files["0/U"]


def test_real_v2412_probe_samples_report_pressure_and_profile_errors() -> None:
    contract = laminar(mesh_level=0, inlet={"mean_velocity_m_s": 0.01},
                       domain=derive_fluid_domain(tube_spec(14.0, 2.0, 200.0)))
    pressure = (FIXTURES / "laminar_probe_p.txt").read_text()
    velocity = (FIXTURES / "laminar_probe_U.txt").read_text()
    comparison = laminar_comparison(contract, pressure, velocity)
    assert comparison["iteration"] == 300
    assert comparison["pressure_gradient_pa_m"] == pytest.approx(4.01680543857)
    assert comparison["hagen_poiseuille_pa_m"] == pytest.approx(3.2064)
    # This real coarse-mesh result misses the analytic gradient; never mark it validated.
    assert comparison["pressure_gradient_relative_error"] > 0.25
    assert comparison["profile_max_error_over_mean_velocity"] < 0.02


def test_generated_cases_cache_grad_u_to_avoid_pinned_transpose_defect() -> None:
    for contract in (laminar(), c1_turbulent(), c1_turbulent(turbulence_model="kEpsilon", wall_treatment="wall_function")):
        solution = casegen.generate(contract)["system/fvSolution"]
        assert "cache\n{\n    grad(U) yes;\n}" in solution
        assert "interpolationScheme cellPoint;" in casegen.generate(contract)["system/controlDict"]
    assert casegen.CASEGEN_VERSION == "cfd_pipe_casegen_v2"


def test_real_v2412_probe_samples_with_cached_gradient_match_hagen_poiseuille() -> None:
    contract = laminar(mesh_level=0, inlet={"mean_velocity_m_s": 0.01},
                       domain=derive_fluid_domain(tube_spec(14.0, 2.0, 200.0)))
    comparison = laminar_comparison(contract, (FIXTURES / "laminar_cached_grad_probe_p.txt").read_text(),
                                    (FIXTURES / "laminar_cached_grad_probe_U.txt").read_text())
    assert comparison["pressure_gradient_pa_m"] == pytest.approx(3.22077746097)
    assert abs(comparison["pressure_gradient_relative_error"]) < 0.01
    assert comparison["profile_max_error_over_mean_velocity"] < 0.01


def test_probe_reader_refuses_missing_or_mismatched_samples() -> None:
    pressure = (FIXTURES / "laminar_probe_p.txt").read_text()
    velocity = (FIXTURES / "laminar_probe_U.txt").read_text()
    contract = laminar(mesh_level=0, inlet={"mean_velocity_m_s": 0.01},
                       domain=derive_fluid_domain(tube_spec(14.0, 2.0, 200.0)))
    with pytest.raises(ProbeResultError, match="missing or non-finite"):
        parse_probes(pressure.replace("0.000295641728524", "nan").replace("0.000286136884695", "nan"), vectors=False)
    with pytest.raises(ProbeResultError, match="different iterations"):
        laminar_comparison(contract, pressure, velocity.replace("300 ", "299 "))
    with pytest.raises(ProbeResultError, match="differs from the typed case"):
        laminar_comparison(contract, pressure.replace("0.13 ", "0.12 "), velocity.replace("0.13 ", "0.12 "))



def _probe_text(contract: PipeStudyContract, pressures: tuple[float, float], iteration: int = 1500) -> tuple[str, str]:
    locations = casegen.probe_locations(contract)
    header = "".join(f"# Probe {i} ({x!r} {y!r} {z!r})\n" for i, (x, y, z) in enumerate(locations))
    pressure = header + f"{iteration} {pressures[0]!r} {pressures[1]!r} {pressures[1]!r} {pressures[1]!r}\n"
    velocity = header + f"{iteration}" + " (0.5 0 0)" * len(locations) + "\n"
    return pressure, velocity


def test_turbulent_comparison_reports_signed_errors_against_both_correlations() -> None:
    contract = c1_turbulent(domain=derive_fluid_domain(tube_spec(length_mm=3000.0)))
    stations = casegen.probe_locations(contract)
    assert stations[0][0] >= contract.required_length_m
    target = 0.97 * references.pressure_gradient_pa_m(
        references.darcy_colebrook_smooth(contract.reynolds), contract.fluid.density_kg_m3,
        contract.mean_velocity_m_s, contract.diameter_m)
    drop = target * (stations[1][0] - stations[0][0]) / contract.fluid.density_kg_m3
    comparison = turbulent_comparison(contract, *_probe_text(contract, (0.04 + drop, 0.04)))
    assert comparison["iteration"] == 1500
    assert comparison["pressure_gradient_pa_m"] == pytest.approx(target)
    assert comparison["colebrook_smooth_pa_m"] == pytest.approx(56.444, rel=1e-4)
    assert comparison["blasius_pa_m"] == pytest.approx(57.938, rel=1e-4)
    assert comparison["signed_error_vs_colebrook"] == pytest.approx(-0.03)
    assert comparison["signed_error_vs_blasius"] < comparison["signed_error_vs_colebrook"]
    assert comparison["darcy_friction_factor"] == pytest.approx(0.97 * references.darcy_colebrook_smooth(contract.reynolds))
    with pytest.raises(ProbeResultError, match="turbulent_rans contract"):
        turbulent_comparison(laminar(), *_probe_text(laminar(), (1.0, 0.5)))
    with pytest.raises(ProbeResultError, match="laminar contract"):
        laminar_comparison(contract, *_probe_text(contract, (1.0, 0.5)))


def test_real_v2412_turbulent_probes_before_development_length_are_refused() -> None:
    # The first C1 SST run used a 2 m tube: its 1.3 m station lies inside the 1.59 m allowance.
    # The contract now refuses that tube before a run; the probe guard stays as a second line.
    short = derive_fluid_domain(tube_spec(length_mm=2000.0))
    with pytest.raises(ValidationError, match="upstream pressure station"):
        c1_turbulent(domain=short)
    contract = c1_turbulent(solver={"max_iterations": 1500, "residual_target": 1e-5}).model_copy(
        update={"domain": short})
    pressure = (FIXTURES / "turbulent_sst_2m_probe_p.txt").read_text()
    velocity = (FIXTURES / "turbulent_sst_2m_probe_U.txt").read_text()
    assert parse_probes(pressure, vectors=False).iteration == 1500
    with pytest.raises(ProbeResultError, match="before the 1.59 m development length"):
        turbulent_comparison(contract, pressure, velocity)



def test_real_v2412_turbulent_developed_section_probes_against_correlations() -> None:
    contract = c1_turbulent(solver={"max_iterations": 1500, "residual_target": 1e-5},
                            domain=derive_fluid_domain(tube_spec(length_mm=3000.0)))
    comparison = turbulent_comparison(contract, (FIXTURES / "turbulent_sst_3m_probe_p.txt").read_text(),
                                      (FIXTURES / "turbulent_sst_3m_probe_U.txt").read_text())
    assert comparison["pressure_gradient_pa_m"] == pytest.approx(55.5303764, rel=1e-7)
    assert comparison["signed_error_vs_colebrook"] == pytest.approx(-0.0161848, abs=1e-6)
    assert comparison["signed_error_vs_blasius"] == pytest.approx(-0.0415514, abs=1e-6)

# --- mesh study ---------------------------------------------------------------------------


def test_three_mesh_study_recovers_second_order_and_gci() -> None:
    values = tuple(1.0 + 0.01 * h**2 for h in (1.0, 1.5, 2.25))
    study = three_mesh_study(values, 1.5)
    assert study.convergence == "monotonic"
    assert study.observed_order == pytest.approx(2.0)
    assert study.extrapolated == pytest.approx(1.0)
    assert study.gci_fine == pytest.approx(1.25 * (0.0125 / 1.01) / (1.5**2 - 1.0))


def test_three_mesh_study_never_reports_gci_for_oscillatory_or_divergent_triplets() -> None:
    # Typed laminar levels 2/1/0 on the pinned build (level 2 not residual-converged).
    oscillatory = three_mesh_study((3.218273, 3.215386, 3.220777), 1.5)
    assert oscillatory.convergence == "oscillatory"
    assert oscillatory.gci_fine is None and oscillatory.observed_order is None
    divergent = three_mesh_study((1.0, 1.1, 1.15), 1.5)
    assert divergent.convergence == "divergent" and divergent.gci_fine is None
    assert three_mesh_study((2.0, 2.0, 2.0), 1.5).convergence == "converged"


def test_three_mesh_study_solves_order_for_non_constant_ratio() -> None:
    r21, r32 = 1.4909, 1.5
    values = tuple(1.0 + 0.01 * h**2 for h in (1.0, r21, r21 * r32))
    study = three_mesh_study(values, (r21, r32))
    assert study.convergence == "monotonic"
    assert study.observed_order == pytest.approx(2.0)
    assert study.extrapolated == pytest.approx(1.0)
    assert study.gci_fine == pytest.approx(1.25 * abs(values[1] - values[0]) / values[0] / (r21**2 - 1.0))
    # The constant-ratio formula would misstate the order on the same triplet.
    assert three_mesh_study(values, r21).observed_order != pytest.approx(2.0)


def test_converged_laminar_triplet_is_monotonic_with_gci() -> None:
    # Typed laminar levels 2/1/0, 600 iterations, all residual-converged (cycle 83 runs).
    study = three_mesh_study((3.2121806796689745, 3.2153669223401247, 3.2207775720011185), 1.5)
    assert study.convergence == "monotonic"
    assert study.observed_order == pytest.approx(1.306, abs=1e-3)
    assert study.extrapolated == pytest.approx(3.207617, abs=1e-6)
    assert study.gci_fine == pytest.approx(0.001776, abs=1e-6)


def test_turbulent_c1_triplet_meets_the_pre_stated_mesh_tolerance() -> None:
    # Typed C1 SST levels 2/1/0 on the 2.6 m tube (cycle 84); T2 requires monotonic and GCI <= 3%.
    cells = (1_769_284, 533_925, 158_200)
    study = three_mesh_study((55.99108585951357, 55.8861340447833, 55.642746085032904), refinement_ratios(cells))
    assert study.convergence == "monotonic"
    assert study.observed_order == pytest.approx(2.050, abs=1e-3)
    assert study.extrapolated == pytest.approx(56.07386, abs=1e-4)
    assert study.gci_fine is not None and study.gci_fine == pytest.approx(0.001848, abs=1e-6) and study.gci_fine <= 0.03


def test_refinement_ratios_allow_rounded_turbulent_levels() -> None:
    r21, r32 = refinement_ratios((1_769_284, 533_925, 158_200))
    assert r21 == pytest.approx(1.4909, abs=1e-4) and r32 == pytest.approx(1.5)
    with pytest.raises(MeshStudyError, match="not constant"):
        refinement_ratio((1_769_284, 533_925, 158_200))


def test_refinement_ratio_from_generated_mesh_levels() -> None:
    assert refinement_ratio((174_960, 51_840, 15_360)) == pytest.approx(1.5)
    with pytest.raises(MeshStudyError, match="not constant"):
        refinement_ratio((174_960, 60_000, 15_360))
    with pytest.raises(MeshStudyError, match="strictly decreasing"):
        refinement_ratio((15_360, 51_840, 174_960))
    with pytest.raises(MeshStudyError, match="below the recommended"):
        refinement_ratio((2_000, 1_500, 1_125))

# --- mesh check ---------------------------------------------------------------------------


def test_checkmesh_parsing_from_real_v2412_log() -> None:
    report = parse_checkmesh((FIXTURES / "checkMesh_ok.log").read_text())
    assert report == CheckMeshReport(cells=121800, max_non_orthogonality_deg=26.0357950025, max_skewness=0.53926781269,
                                     max_aspect_ratio=290.510122154, failed_checks=0)
    # The fixture is the level-0 mesh of the first 2 m C1 tube.
    contract = c1_turbulent().model_copy(update={"domain": derive_fluid_domain(tube_spec(length_mm=2000.0))})
    record = evaluate_mesh(contract, casegen.mesh_plan(contract), report)
    assert record.accepted and record.refusals == ()


def test_checkmesh_failure_forms_are_parsed() -> None:
    log = (FIXTURES / "checkMesh_ok.log").read_text()
    failing = log.replace("Max aspect ratio = 290.510122154 OK.",
                          " ***High aspect ratio cells found, Max aspect ratio: 1500.5, number of cells 3")
    failing = failing.replace("Mesh OK.", "Failed 1 mesh checks.")
    report = parse_checkmesh(failing)
    assert report.max_aspect_ratio == 1500.5 and report.failed_checks == 1
    with pytest.raises(MeshCheckParseError):
        parse_checkmesh("Mesh stats\n    cells: 10\n")
    with pytest.raises(MeshCheckParseError):
        parse_checkmesh(log.replace("Mesh OK.", ""))


def test_small_determinant_is_waived_but_other_failures_refuse() -> None:
    contract = c1_turbulent().model_copy(update={"domain": derive_fluid_domain(tube_spec(length_mm=2000.0))})
    plan = casegen.mesh_plan(contract)
    log = (FIXTURES / "checkMesh_ok.log").read_text()
    determinant = (" ***Cells with small determinant (< 0.001) found, number of cells: 62096\n"
                   "  <<Writing 62096 under-determined cells to set underdeterminedCells\n")
    waived_only = log.replace("Mesh OK.", determinant + "\nFailed 1 mesh checks.")
    report = parse_checkmesh(waived_only)
    assert report.failed_checks == 1
    assert report.waived_checks == ("Cells with small determinant (< 0.001) found, number of cells: 62096",)
    record = evaluate_mesh(contract, plan, report)
    assert record.accepted and record.refusals == ()
    concave = " ***Concave cells (using face planes) found, number of cells: 4\n"
    both = log.replace("Mesh OK.", determinant + concave + "\nFailed 2 mesh checks.")
    record = evaluate_mesh(contract, plan, parse_checkmesh(both))
    assert record.refusals == ("checkmesh_failed: 1 failed mesh checks",)
    assert parse_checkmesh(log).waived_checks == ()


def test_mesh_check_refuses_bad_meshes() -> None:
    contract = c1_turbulent()
    plan = casegen.mesh_plan(contract)
    good = parse_checkmesh((FIXTURES / "checkMesh_ok.log").read_text())
    bad = good.model_copy(update={"failed_checks": 2, "max_non_orthogonality_deg": 75.0, "max_skewness": 5.0,
                                  "max_aspect_ratio": 2000.0, "cells": 10})
    record = evaluate_mesh(contract, plan, bad)
    assert not record.accepted
    assert [reason.split(":")[0] for reason in record.refusals] == [
        "checkmesh_failed", "cell_count_mismatch", "non_orthogonality", "skewness", "aspect_ratio"]
    assert evaluate_mesh(contract, plan, None).refusals == ("check_missing: no checkMesh report",)
    coarse_wall = plan.model_copy(update={"estimated_wall_yplus": 3.0})
    assert evaluate_mesh(contract, coarse_wall, good).refusals[0].startswith("wall_yplus")
    wf = c1_turbulent(turbulence_model="kEpsilon", wall_treatment="wall_function", mesh_level=2)
    wf_plan = casegen.mesh_plan(wf)
    assert wf_plan.estimated_wall_yplus < 30  # wall functions cannot be refined into the buffer layer
    assert evaluate_mesh(wf, wf_plan, good.model_copy(update={"cells": wf_plan.cell_count})).refusals[0].startswith("wall_yplus")
    huge = plan.model_copy(update={"cell_count": 3_000_000})
    assert any(reason.startswith("cell_budget") for reason in evaluate_mesh(contract, huge, good).refusals)


# --- residual parsing ---------------------------------------------------------------------


def test_residual_log_from_real_v2412_run() -> None:
    header = "trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE).\n"
    rows = parse_log(header + (FIXTURES / "simpleFoam_kOmegaSST.log").read_text())
    assert [row.iteration for row in rows] == [1, 2, 3, 40]
    last = rows[-1]
    assert set(last.initial) == {"Ux", "Uy", "Uz", "p", "omega", "k"}
    assert last.initial["p"] == pytest.approx(0.000965657617802)
    assert last.continuity_global == pytest.approx(4.82195429026e-06)
    assert last.execution_time_s == pytest.approx(57.55)
    assert last.max_normalized_residual == pytest.approx(0.00475699508039)


def test_residual_stream_is_incremental_across_arbitrary_chunks() -> None:
    text = (FIXTURES / "simpleFoam_kOmegaSST.log").read_text()
    whole = parse_log(text)
    stream = ResidualStream()
    seen = []
    for index in range(0, len(text), 37):
        seen.extend(stream.feed(text[index:index + 37]))
        # Live: an iteration is published once its ExecutionTime line arrives, not at the end.
        if index < len(text) // 2:
            assert len(seen) < len(whole)
    seen.extend(stream.close())
    assert seen == whole
    assert stream.fatal is None and stream.solver_reported_end


def test_residual_stream_flags_fatal_and_rejects_hostile_values() -> None:
    stream = ResidualStream()
    stream.feed("Time = 1\n\n--> FOAM FATAL ERROR: \nsomething\n")
    assert stream.fatal is not None
    with pytest.raises(LogParseError):
        parse_log("Time = 1\nsmoothSolver:  Solving for Ux, Initial residual = nan, Final residual = 1, No Iterations 1\n")
    with pytest.raises(LogParseError):
        parse_log("Time = 1.5\n")
    # A trailing iteration cut off before its ExecutionTime line is not published.
    partial = (FIXTURES / "simpleFoam_kOmegaSST.log").read_text().split("\nEnd")[0].rsplit("ExecutionTime", 1)[0]
    assert [row.iteration for row in parse_log(partial)] == [1, 2, 3]


def test_residual_stream_drops_iteration_missing_execution_time_before_next_time() -> None:
    log = (
        "Time = 1\nSolving for Ux, Initial residual = 0.1, Final residual = 0.01, No Iterations 1\n"
        "Time = 2\nSolving for Ux, Initial residual = 0.02, Final residual = 0.001, No Iterations 1\n"
        "ExecutionTime = 2 s  ClockTime = 2 s\n"
    )
    assert [row.iteration for row in parse_log(log)] == [2]


def test_convergence_requires_every_field_below_target_over_window() -> None:
    def log(values: list[float]) -> str:
        return "".join(
            f"Time = {i}\nsmoothSolver:  Solving for Ux, Initial residual = {v}, Final residual = 0, No Iterations 1\n"
            f"smoothSolver:  Solving for Uy, Initial residual = {v}, Final residual = 0, No Iterations 1\n"
            f"smoothSolver:  Solving for Uz, Initial residual = {v}, Final residual = 0, No Iterations 1\n"
            f"GAMG:  Solving for p, Initial residual = {v}, Final residual = 0, No Iterations 1\n"
            f"ExecutionTime = {i} s  ClockTime = {i} s\n\n" for i, v in enumerate(values, start=1))
    fields = required_fields(())
    assert converged(parse_log(log([1e-3] + [1e-7] * 5)), fields, 1e-6)
    assert not converged(parse_log(log([1e-7] * 4)), fields, 1e-6)  # window not filled
    assert not converged(parse_log(log([1e-7] * 4 + [2e-6])), fields, 1e-6)
    assert not converged(parse_log(log([1e-7] * 6)), required_fields(("k", "omega")), 1e-6)  # missing fields


# --- real tools (skips cleanly when absent) -----------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("JARVISOS_BLUECAD_TOOL_REGISTRY") or shutil.which("true") is None,
    reason="operator tool registry with OpenFOAM is not configured",
)
def test_generated_case_meshes_and_passes_check_with_real_openfoam(tmp_path: Path) -> None:
    from app.modules.bluecad.registry import ToolRegistryError, resolve_tool, run_tool

    try:
        pins = {tool: resolve_tool(tool)["version_pin"] for tool in ("openfoam_blockmesh", "openfoam_checkmesh")}
    except ToolRegistryError as exc:
        pytest.skip(f"OpenFOAM tools unavailable: {exc.code}")
    contract = c1_turbulent()
    case_dir = tmp_path / "case"
    casegen.write_case(contract, case_dir, pins)
    mesh = run_tool("openfoam_blockmesh", [], case_dir, 120)
    assert mesh.returncode == 0, mesh.stdout[-2000:] + mesh.stderr[-2000:]
    check = run_tool("openfoam_checkmesh", [], case_dir, 120)
    record = evaluate_mesh(contract, casegen.mesh_plan(contract), parse_checkmesh(check.stdout))
    assert record.accepted, record.refusals


# --- solver controls ----------------------------------------------------------------------


def test_solver_controls_reuse_generated_solver_settings_and_are_deterministic() -> None:
    contract = solver_controls_contract()
    pipe = casegen.generate(contract)
    for control in solver_controls.CONTROL_NAMES:
        files = solver_controls.generate_control(control, contract)
        assert files == solver_controls.generate_control(control, contract)
        assert files["system/fvSchemes"] == pipe["system/fvSchemes"]
        assert files["system/fvSolution"] == pipe["system/fvSolution"]
        assert files["constant/transportProperties"] == pipe["constant/transportProperties"]
        for text in files.values():
            assert "#" not in text.split("\n", 1)[1] and "$" not in text
    duct = solver_controls.generate_control("square_duct", contract)
    assert "symmetry" not in duct["system/blockMeshDict"] and "volumetricFlowRate constant 1e-06;" in duct["0/U"]
    channel = solver_controls.generate_control("half_channel", contract)
    assert "symmetryPlane" in channel["system/blockMeshDict"] and "constant 2.5e-07;" in channel["0/U"]
    with pytest.raises(ValueError, match="laminar"):
        solver_controls.generate_control("square_duct", c1_turbulent())


def solver_controls_contract() -> PipeStudyContract:
    return laminar(mesh_level=0, inlet={"mean_velocity_m_s": 0.01}, domain=derive_fluid_domain(tube_spec(14.0, 2.0, 200.0)))


def _control_probe_text(control: str, upstream: float, downstream: float, iteration: int = 300) -> str:
    rows = [f"# Probe {i} ({x} {y} {z})" for i, (x, y, z) in enumerate(solver_controls.probe_locations(control))]
    return "\n".join([*rows, "# Time 0 1", f"{iteration} {upstream} {downstream}", ""])


def test_solver_control_comparison_against_analytic_answers() -> None:
    contract = solver_controls_contract()
    assert solver_controls.analytic_gradient_pa_m("square_duct", contract) == pytest.approx(2.8510908)
    assert solver_controls.analytic_gradient_pa_m("half_channel", contract) == pytest.approx(1.2024)
    # Pinned v2412 kinematic cell-centre probe values: cached grad(U) duct, and the uncached defective duct.
    good = solver_controls.compare_control("square_duct", contract,
                                           _control_probe_text("square_duct", 0.00020409051033, 9.1466979157e-05))
    assert good.passed and good.relative_error == pytest.approx(-0.01423, abs=1e-4)
    bad = solver_controls.compare_control("square_duct", contract,
                                          _control_probe_text("square_duct", 0.000253278479555, 0.000113705360111))
    assert not bad.passed and bad.relative_error > 0.2
    with pytest.raises(ProbeResultError, match="final iteration"):
        solver_controls.compare_control("square_duct", contract, _control_probe_text("square_duct", 1e-4, 0.0, 299))
    with pytest.raises(ProbeResultError, match="locations"):
        solver_controls.compare_control("square_duct", contract,
                                        _control_probe_text("square_duct", 1e-4, 0.0).replace("0.13 ", "0.12 "))


@pytest.mark.skipif(
    not os.environ.get("JARVISOS_BLUECAD_TOOL_REGISTRY"),
    reason="operator tool registry with OpenFOAM is not configured",
)
def test_solver_controls_with_real_openfoam_detect_the_transpose_defect(tmp_path: Path, monkeypatch) -> None:
    from app.modules.bluecad.registry import ToolRegistryError, resolve_tool, run_tool

    try:
        for tool in ("openfoam_blockmesh", "openfoam_simplefoam"):
            resolve_tool(tool)
    except ToolRegistryError as exc:
        pytest.skip(f"OpenFOAM tools unavailable: {exc.code}")
    contract = solver_controls_contract()

    def measure(control: str, case: Path) -> solver_controls.ControlComparison:
        for rel, text in solver_controls.generate_control(control, contract).items():
            (case / rel).parent.mkdir(parents=True, exist_ok=True)
            (case / rel).write_text(text, encoding="utf-8")
        for tool in ("openfoam_blockmesh", "openfoam_simplefoam"):
            result = run_tool(tool, [], case, 900)
            assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        pressure = (case / "postProcessing" / "control_probes" / "0" / "p").read_text()
        return solver_controls.compare_control(control, contract, pressure)

    for control in solver_controls.CONTROL_NAMES:
        assert measure(control, tmp_path / control).passed
    monkeypatch.setattr(casegen, "CACHED_FIELDS", ())
    assert not measure("square_duct", tmp_path / "uncached_duct").passed
