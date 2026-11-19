from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.bootstrap import initialize_storage
from app.modules.engineering import bench, bench_cards
from app.modules.engineering.evaluator_contracts import EvaluationRequest
from app.modules.engineering.marine_motion import RigidBodyWavesEvaluator
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def motion_values(**overrides: float) -> dict[str, float]:
    values = {
        "mass_kg": 500000.0,
        "added_mass_surge_kg": 50000.0,
        "added_mass_heave_kg": 150000.0,
        "pitch_inertia_kg_m2": 5e8,
        "waterplane_area_m2": 400.0,
        "gm_pitch_m": 2.0,
        "damping_surge_n_s_m": 500000.0,
        "damping_heave_n_s_m": 800000.0,
        "damping_pitch_n_m_s_rad": 2e8,
        "mooring_kx_n_m": 500000.0,
        "mooring_kz_n_m": 0.0,
        "wave_amplitude_m": 1.0,
        "wave_period_s": 8.0,
        "water_density_kg_m3": 1025.0,
        "gravity_m_s2": 9.80665,
    }
    values.update(overrides)
    return values


def motion_request(workspace_id: str = "main", **overrides: float) -> dict[str, Any]:
    now = datetime.now(UTC)
    values = motion_values(**overrides)
    units = {
        "mass_kg": "kg",
        "added_mass_surge_kg": "kg",
        "added_mass_heave_kg": "kg",
        "pitch_inertia_kg_m2": "kg*m**2",
        "waterplane_area_m2": "m2",
        "gm_pitch_m": "m",
        "damping_surge_n_s_m": "N*s/m",
        "damping_heave_n_s_m": "N*s/m",
        "damping_pitch_n_m_s_rad": "N*m*s/rad",
        "mooring_kx_n_m": "N/m",
        "mooring_kz_n_m": "N/m",
        "wave_amplitude_m": "m",
        "wave_period_s": "s",
        "water_density_kg_m3": "kg/m3",
        "gravity_m_s2": "m/s2",
    }
    return {
        "schema_version": "engineering_evaluator.v1",
        "request_ref": {
            "authority_owner": "marine",
            "object_type": "evaluation_request",
            "object_id": "req-motion-324",
            "workspace_id": workspace_id,
            "revision": "1",
        },
        "evaluator_id": "marine.rigid_body_waves",
        "subject_ref": {
            "authority_owner": "marine",
            "object_type": "material_state",
            "object_id": "rigid-body-324",
            "workspace_id": workspace_id,
            "revision": "1",
        },
        "inputs": [
            {"name": name, "value": {"value": value, "unit": units[name]}}
            for name, value in values.items()
        ],
        "backend_options": {},
        "requested_at": (now - timedelta(seconds=1)).isoformat(),
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
    }


def evaluate_direct(workspace_id: str = "main", **overrides: float):
    evaluator = RigidBodyWavesEvaluator()
    request = EvaluationRequest.model_validate(motion_request(workspace_id, **overrides))
    return evaluator.evaluate(request)


def output_map(result) -> dict[str, float]:
    return {item.name: item.value.value for item in result.outputs}


def test_exact_heave_resonance_and_quasi_static_limit() -> None:
    values = motion_values()
    rho = values["water_density_kg_m3"]
    g = values["gravity_m_s2"]
    aw = values["waterplane_area_m2"]
    kz = values["mooring_kz_n_m"]
    total_mass = values["mass_kg"] + values["added_mass_heave_kg"]
    heave_k = rho * g * aw + kz
    omega_n = math.sqrt(heave_k / total_mass)
    period_n = 2.0 * math.pi / omega_n

    resonance = output_map(evaluate_direct(wave_period_s=period_n))
    expected = rho * g * aw / (omega_n * values["damping_heave_n_s_m"])
    assert math.isclose(resonance["heave_rao"], expected, rel_tol=1e-9, abs_tol=1e-12)

    lower = output_map(evaluate_direct(wave_period_s=period_n * 0.8))
    higher = output_map(evaluate_direct(wave_period_s=period_n * 1.2))
    assert resonance["heave_rao"] > lower["heave_rao"]
    assert resonance["heave_rao"] > higher["heave_rao"]

    quasi = output_map(evaluate_direct(wave_period_s=1e6))
    expected_quasi = rho * g * aw / (rho * g * aw + kz)
    assert math.isclose(quasi["heave_rao"], expected_quasi, rel_tol=1e-9, abs_tol=1e-12)


def test_mooring_sensitivity_and_zero_wave() -> None:
    low_kx = 100.0
    high_kx = 2_000_000.0
    low = output_map(evaluate_direct(mooring_kx_n_m=low_kx, wave_period_s=100.0))
    high = output_map(evaluate_direct(mooring_kx_n_m=high_kx, wave_period_s=100.0))
    assert high["surge_amplitude"] < low["surge_amplitude"]
    assert high["mooring_tension"] > low["mooring_tension"]

    zero = output_map(evaluate_direct(wave_amplitude_m=0.0))
    assert zero["heave_amplitude"] == 0.0
    assert zero["surge_amplitude"] == 0.0
    assert zero["pitch_amplitude"] == 0.0
    assert zero["mooring_tension"] == 0.0


def test_bench_and_card_integration(tmp_path: Path) -> None:
    initialize_storage(seed_default=True)
    workspace = create_workspace(WorkspaceCreate(name="Moto onde 324", slug="motion-324", status="active"))

    result, view = bench.run_evaluation(
        "marine.rigid_body_waves",
        motion_request(workspace.id),
        out_dir=tmp_path / "bench-run",
        root=tmp_path,
    )
    assert view["verdict"] == "usable"
    assert result["status"] == "succeeded"
    assert len(result["outputs"]) == 10

    cards = bench_cards.list_cards(workspace.id)
    ids = {card["card_id"] for card in cards["cards"]}
    assert "marine.rigid_body_waves" in ids

    card = bench_cards.CreateBenchRunRequest(
        card_id="marine.rigid_body_waves",
        inputs={field.name: field.default for field in bench_cards.CARD_BY_ID["marine.rigid_body_waves"].fields},
        options={},
    )
    detail = bench_cards.run_card(workspace.id, card)
    assert detail["evaluator_id"] == "marine.rigid_body_waves"
    assert detail["run_id"].startswith("run_")


def test_validation_rejects_nonpositive_mass_and_period() -> None:
    mass = evaluate_direct(mass_kg=0.0)
    period = evaluate_direct(wave_period_s=0.0)
    assert mass.status == "refused"
    assert mass.failure is not None
    assert mass.failure.backend_code == "input_non_positive"
    assert period.status == "refused"
    assert period.failure is not None
    assert period.failure.backend_code == "input_non_positive"
