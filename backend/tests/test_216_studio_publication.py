"""Spec 216 reduced-model publication through the Studio published-version owner."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.bluecad.cfd_pipe.reduced_model import PublicationRefused, evaluate
from app.modules.bluecad.cfd_pipe.studio_publication import (
    publish_to_studio,
    reduced_model_of_source,
    studio_contract,
    studio_source,
    studio_tests,
)
from app.modules.studio_runtime.runtime import status
from tests.test_216_reduced_model import c1_model


def _evaluate(source: str, **inputs: float) -> dict[str, float]:
    namespace: dict = {}
    exec(compile(source, "studio_calc.py", "exec"), namespace)  # generated source, typed constants only
    return namespace["main"](inputs)


def _c1_inputs(model, reynolds: float) -> dict[str, float]:
    v = model.segments[0].validity
    return {"mean_velocity": reynolds * (v.dynamic_viscosity_pa_s / v.density_kg_m3) / v.diameter_m,
            "diameter": v.diameter_m, "density": v.density_kg_m3, "dynamic_viscosity": v.dynamic_viscosity_pa_s}


def test_generated_source_is_deterministic_and_matches_the_reduced_model() -> None:
    model = c1_model()
    source = studio_source(model)
    assert source == studio_source(c1_model())
    assert model.version_id in source
    for re in (1e4, 1.5e4, 2.8e4):
        inputs = _c1_inputs(model, re)
        expected = evaluate(model, reynolds=re, diameter_m=inputs["diameter"], density_kg_m3=inputs["density"],
                            dynamic_viscosity_pa_s=inputs["dynamic_viscosity"])
        out = _evaluate(source, **inputs)
        assert out["reynolds"] == pytest.approx(re, rel=1e-12)
        assert out["darcy_friction_factor"] == pytest.approx(expected.darcy, rel=1e-12)
        assert out["pressure_gradient"] == pytest.approx(expected.pressure_gradient_pa_m, rel=1e-12)
        assert out["wall_shear_stress"] == pytest.approx(expected.wall_shear_pa, rel=1e-12)
        assert out["relative_uncertainty"] == expected.relative_uncertainty


def test_generated_source_refuses_outside_the_validated_range_and_fluid() -> None:
    model = c1_model()
    source = studio_source(model)
    with pytest.raises(ValueError, match="outside the validated range"):
        _evaluate(source, **_c1_inputs(model, 3e3))
    with pytest.raises(ValueError, match="outside the validated range"):
        _evaluate(source, **_c1_inputs(model, 4e4))
    with pytest.raises(ValueError, match="density"):
        _evaluate(source, **{**_c1_inputs(model, 2e4), "density": 1020.0})


def test_contract_bounds_pin_bore_and_fluid_and_span_the_validated_velocity() -> None:
    model = c1_model()
    contract = studio_contract(model)
    by_name = {item.name: item for item in contract.inputs}
    v = model.segments[0].validity
    for name, value in (("diameter", v.diameter_m), ("density", v.density_kg_m3),
                        ("dynamic_viscosity", v.dynamic_viscosity_pa_s)):
        assert by_name[name].min < value < by_name[name].max
        assert by_name[name].max - by_name[name].min == pytest.approx(2e-9 * value, rel=1e-6)
    nu = v.dynamic_viscosity_pa_s / v.density_kg_m3
    assert by_name["mean_velocity"].min == pytest.approx(v.reynolds_min * nu / v.diameter_m, rel=1e-8)
    assert by_name["mean_velocity"].max == pytest.approx(v.reynolds_max * nu / v.diameter_m, rel=1e-8)
    assert [item.name for item in contract.outputs] == [
        "reynolds", "darcy_friction_factor", "pressure_gradient", "wall_shear_stress", "relative_uncertainty"]


def test_test_cases_are_the_measured_points_and_pass_offline() -> None:
    model = c1_model()
    source, tests = studio_source(model), studio_tests(model)
    (segment,) = model.segments
    assert len(tests.cases) == len(segment.points)
    contract = studio_contract(model)
    for case, p in zip(tests.cases, segment.points, strict=True):
        for item in contract.inputs:
            assert item.min is None or item.min <= case.inputs[item.name] <= item.max
        out = _evaluate(source, **case.inputs)
        assert case.expected_outputs == {"darcy_friction_factor": p.darcy_cfd}
        assert abs(out["darcy_friction_factor"] - p.darcy_cfd) <= case.tolerance


def test_tampered_model_and_foreign_source_are_refused() -> None:
    model = c1_model()
    forged = model.model_copy(update={"geometry_digest": "sha256:" + "f" * 64})
    with pytest.raises(PublicationRefused, match="version identity"):
        studio_source(forged)
    with pytest.raises(PublicationRefused, match="version identity"):
        publish_to_studio("main", forged)
    source = studio_source(model)
    assert reduced_model_of_source(source) == model
    with pytest.raises(PublicationRefused, match="does not match"):
        reduced_model_of_source(source.replace("RANGE_SLACK = 1e-09", "RANGE_SLACK = 0.5"))
    with pytest.raises(PublicationRefused, match="not a generated"):
        reduced_model_of_source("def main(inputs):\n    return {}\n")


@pytest.fixture
def workspace(monkeypatch):
    if os.getenv("JARVISOS_STUDIO_CI_SANDBOX_UNAVAILABLE") == "1":
        pytest.skip("CI runner does not permit bubblewrap user namespaces; WSL2 qualification is required")
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    if not status()["available"]:
        pytest.skip("Studio runtime unavailable on this host")
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_publication_creates_an_immutable_studio_version_carrying_the_model(workspace) -> None:
    model = c1_model()
    version = publish_to_studio("main", model)
    assert version["reduced_model_version_id"] == model.version_id
    assert version["category"] == "calculation" and version["test_report"]["passed"]
    assert version["scientific_status"] == "unqualified"
    with open_sqlite_connection() as connection:
        row = connection.execute("""SELECT mv.implementation_kind, a.stored_path, a.sha256, sp.contract_json
            FROM model_versions mv JOIN artifacts a ON a.id = mv.implementation_artifact_id
            JOIN studio_published_versions sp ON sp.version_id = mv.id WHERE mv.id = ?""",
                                 (version["id"],)).fetchone()
    assert row["implementation_kind"] == "studio_calc_v1"
    with open(row["stored_path"], encoding="utf-8") as handle:
        source = handle.read()
    assert reduced_model_of_source(source) == model
    assert row["sha256"] == version["source_sha256"]
    assert json.loads(row["contract_json"])["inputs"][0]["name"] == "mean_velocity"
    model_page = workspace.get(f"/workspaces/main/studio/models/{version['model_spec_id']}").json()
    assert model.version_id in model_page["assumptions"]
    assert model_page["status"] == "published"


def test_publication_refuses_before_creating_anything_when_the_runtime_is_off(monkeypatch) -> None:
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "0")
    get_settings.cache_clear()
    try:
        initialize_storage(seed_default=True)
        with open_sqlite_connection() as connection:
            before = connection.execute("SELECT COUNT(*) FROM studio_drafts").fetchone()[0]
        with pytest.raises(PublicationRefused, match="runtime unavailable"):
            publish_to_studio("main", c1_model())
        with open_sqlite_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM studio_drafts").fetchone()[0] == before
    finally:
        get_settings.cache_clear()
