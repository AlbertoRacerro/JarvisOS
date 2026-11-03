from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.database import initialize_database, open_sqlite_connection
from app.core.paths import build_paths
from app.modules.bio_models import service
from app.modules.bio_models.service import BioModelError
from app.modules.memory.literature_models import LiteratureEntryCreate, LiteratureSourceCreate
from app.modules.memory.literature_service import create_literature_entry, create_literature_source
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "jarvis"))
    get_settings.cache_clear()
    initialize_database()
    return create_workspace(WorkspaceCreate(name="Bio models test", slug="bio-models-test", status="active"))


def make_numeric_basis(workspace_id: str, value: float = 0.08, unit: str | None = "1/hour", *, state: str = "accepted") -> dict[str, str]:
    backing_path = build_paths().artifacts_dir / "literature" / f"bio-model-source-{uuid.uuid4().hex}.md"
    backing_path.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Growth data\nSpecific growth rate: {value} {unit or ''}\n".encode()
    backing_path.write_bytes(content)
    artifact_id = str(uuid.uuid4())
    with open_sqlite_connection() as connection:
        connection.execute("INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, mime_type, sha256, source_ref, status, created_at, notes) VALUES (?, ?, ?, ?, 'literature_source', 'text/markdown', ?, 'fixture', 'registered', '2026-10-02T00:00:00Z', 'bio model verification test')",
                           (artifact_id, workspace_id, backing_path.name, str(backing_path), hashlib.sha256(content).hexdigest()))
        connection.commit()
    source = create_literature_source(workspace_id, LiteratureSourceCreate(title="Test source", source_kind="paper", citation="Fixture citation", state=state, artifact_id=artifact_id))
    entry = create_literature_entry(workspace_id, source.id, LiteratureEntryCreate(entry_kind="datum", value_number=value,
        unit=unit, status=state, locator_kind="line", locator_start=2, context_text="Specific growth rate"))
    return {"object_type": "literature_entry", "object_id": entry.id}


def test_sets_revision_cas_duplicate_edit_and_units(workspace) -> None:
    created = service.create_set(workspace.id, "Empty", "N. gaditana", "T1")
    first = service.edit_set_value(workspace.id, created["id"], "mu_max", {"value": 0.08, "unit": "1/hour", "expected_unit": "1/hour"}, created["revision"], created["digest"])
    assert first["revision"] != created["revision"]
    assert first["history"][-1]["parent_revision"] == created["revision"]
    assert first["values"]["mu_max"]["state"] == "candidate"
    assert first["values"]["mu_max"]["validity_range"] == "> 0"
    duplicate = service.duplicate_set(workspace.id, created["id"], first["revision"], first["digest"])
    assert duplicate["id"] != created["id"]
    assert duplicate["values"]["mu_max"]["state"] == "candidate"
    assert duplicate["values"]["mu_max"]["verification"] is None
    with pytest.raises(BioModelError) as duplicate_conflict:
        service.duplicate_set(workspace.id, created["id"], created["revision"], created["digest"])
    assert duplicate_conflict.value.status == 409
    with pytest.raises(BioModelError) as conflict:
        service.edit_set_value(workspace.id, created["id"], "mu_max", {"value": 1, "unit": "1/hour", "expected_unit": "1/hour"}, created["revision"], created["digest"])
    assert conflict.value.status == 409
    with pytest.raises(BioModelError, match="not compatible"):
        service.edit_set_value(workspace.id, created["id"], "mu_max", {"value": 1, "unit": "m", "expected_unit": "1/hour"}, first["revision"], first["digest"])


def test_verification_matching_mismatch_snapshot_and_review(workspace) -> None:
    basis = make_numeric_basis(workspace.id)
    created = service.create_set(workspace.id, "Empty")
    edited = service.edit_set_value(workspace.id, created["id"], "mu_max", {"value": 0.08, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": basis}, created["revision"], created["digest"])
    verified = service.verify_value(workspace.id, created["id"], "mu_max", edited["revision"], edited["digest"])
    value = verified["values"]["mu_max"]
    assert value["state"] == "source_verified"
    assert value["verification"]["snapshot_digest"]
    assert value["verification"]["no_backing_document"] is False
    assert value["verification"]["snapshot"]["backing_sha256"]
    reviewed = service.review_value(workspace.id, created["id"], "mu_max", "Reviewer A", "Compared with source section 2.", verified["revision"], verified["digest"])
    assert reviewed["values"]["mu_max"]["state"] == "expert_reviewed"
    assert not any("qualified" in str(event) for event in reviewed["history"])

    mismatch_set = service.create_set(workspace.id, "Mismatch")
    mismatch = service.edit_set_value(workspace.id, mismatch_set["id"], "mu_max", {"value": 0.2, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": basis}, mismatch_set["revision"], mismatch_set["digest"])
    with pytest.raises(BioModelError, match="entered 0.2 1/hour; source reports 0.08 1/hour") as refusal:
        service.verify_value(workspace.id, mismatch_set["id"], "mu_max", mismatch["revision"], mismatch["digest"])
    assert refusal.value.detail["entered"] == "0.2 1/hour"
    assert refusal.value.detail["source"] == "0.08 1/hour"


def test_zero_and_near_zero_matching_and_locator_confirmation(workspace) -> None:
    zero_basis = make_numeric_basis(workspace.id, 0.0)
    zero_set = service.create_set(workspace.id, "Zero")
    zero_edit = service.edit_set_value(workspace.id, zero_set["id"], "mu_max", {"value": 0.0, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": zero_basis}, zero_set["revision"], zero_set["digest"])
    assert service.verify_value(workspace.id, zero_set["id"], "mu_max", zero_edit["revision"], zero_edit["digest"])["values"]["mu_max"]["state"] == "source_verified"

    tiny_basis = make_numeric_basis(workspace.id, 1e-14)
    tiny = service.create_set(workspace.id, "Tiny")
    tiny_edit = service.edit_set_value(workspace.id, tiny["id"], "mu_max", {"value": 1.0001e-14, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": tiny_basis}, tiny["revision"], tiny["digest"])
    assert service.verify_value(workspace.id, tiny["id"], "mu_max", tiny_edit["revision"], tiny_edit["digest"])["values"]["mu_max"]["state"] == "source_verified"

    source = create_literature_source(workspace.id, LiteratureSourceCreate(title="Claim source", source_kind="book"))
    claim_set = service.create_set(workspace.id, "Claim")
    claim_edit = service.edit_set_value(workspace.id, claim_set["id"], "mu_max", {"value": 1, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": {"object_type": "literature_source", "object_id": source.id, "locator_kind": "section", "locator": "Table 3"}}, claim_set["revision"], claim_set["digest"])
    with pytest.raises(BioModelError, match="Confirm the shown locator: Table 3"):
        service.verify_value(workspace.id, claim_set["id"], "mu_max", claim_edit["revision"], claim_edit["digest"])
    assert service.verify_value(workspace.id, claim_set["id"], "mu_max", claim_edit["revision"], claim_edit["digest"], locator_confirmed=True)["values"]["mu_max"]["state"] == "source_verified"


def test_changed_snapshot_displays_stale_and_edit_resets_state(workspace) -> None:
    basis = make_numeric_basis(workspace.id)
    created = service.create_set(workspace.id, "Stale")
    edited = service.edit_set_value(workspace.id, created["id"], "mu_max", {"value": 0.08, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": basis}, created["revision"], created["digest"])
    service.verify_value(workspace.id, created["id"], "mu_max", edited["revision"], edited["digest"])
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE literature_sources SET title = ? WHERE workspace_id = ?", ("Changed metadata", workspace.id))
        connection.commit()
    stale = service.get_set(workspace.id, created["id"])
    assert stale["values"]["mu_max"]["state"] == "source_verified"
    assert stale["values"]["mu_max"]["display_state"] == "source_changed_since_verification"
    other_edit = service.edit_set_value(workspace.id, created["id"], "beta", {"value": 0.7, "unit": "1", "expected_unit": "1"}, stale["revision"], stale["digest"])
    assert other_edit["values"]["mu_max"]["state"] == "candidate"
    assert other_edit["values"]["mu_max"]["display_state"] == "source_changed_since_verification"
    edited_again = service.edit_set_value(workspace.id, created["id"], "mu_max", {"value": 0.09, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": basis}, other_edit["revision"], other_edit["digest"])
    assert edited_again["values"]["mu_max"]["state"] == "candidate"
    assert edited_again["values"]["mu_max"]["verification"] is None


def test_expert_review_requires_source_verification(workspace) -> None:
    created = service.create_set(workspace.id, "Review")
    edited = service.edit_set_value(workspace.id, created["id"], "mu_max", {"value": 1, "unit": "1/hour", "expected_unit": "1/hour"}, created["revision"], created["digest"])
    with pytest.raises(BioModelError, match="source_verified"):
        service.review_value(workspace.id, created["id"], "mu_max", "Reviewer", "Note", edited["revision"], edited["digest"])


def test_no_shipping_parameter_values_and_card_refusal(workspace) -> None:
    assert service.list_forms()
    template = service.create_set(workspace.id, "N. gaditana T1 — empty", "N. gaditana")
    assert template["values"] == {}
    with pytest.raises(BioModelError, match="form"):
        service.create_card(workspace.id, "Invalid", template["id"], {"light": "invented.form"}, {"value": 0.1, "unit": "1/hour"})


def test_model_card_pins_parameter_revision_evaluates_and_requires_typed_inputs(workspace) -> None:
    current = service.create_set(workspace.id, "Growth parameters")
    parameters = {"K_I": (200.0, "umol/(m**2*s)"), "K_i": (900.0, "umol/(m**2*s)"),
                  "T_min": (285.0, "K"), "T_opt": (298.15, "K"), "T_max": (315.0, "K"),
                  "K_j_0": (0.02, "kg/m3"), "k_d": (0.003, "1/hour"), "a": (1.8, "1"),
                  "b": (0.5, "1"), "c": (0.1, "1"), "d": (0.01, "1"), "w_ash": (0.05, "1")}
    for symbol, (value, unit) in parameters.items():
        current = service.edit_set_value(workspace.id, current["id"], symbol,
            {"value": value, "unit": unit, "expected_unit": unit}, current["revision"], current["digest"])
    card = service.create_card(workspace.id, "PBR growth", current["id"],
        {"light": "light.haldane", "optics": "optics.slab_response_average", "temperature": "temperature.ctmi",
         "nutrients": ["nutrient.monod"], "combination": "combine.liebig", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"}, {"value": 0.08, "unit": "1/hour"})
    assert card["parameter_set_revision"] == current["revision"]
    operating = {"I0": {"value": 800, "unit": "umol/(m**2*s)"}, "k_X": {"value": 120, "unit": "m**2/kg"},
                 "X": {"value": 0.7, "unit": "kg/m3"}, "L": {"value": 0.05, "unit": "m"},
                 "T": {"value": 298.15, "unit": "K"}, "S_0": {"value": 0.1, "unit": "kg/m3"}}
    output = service.evaluate_card(workspace.id, card["id"], operating)
    assert output["mu_net"]["unit"] == "h⁻¹"
    assert output["breakdown"]["light_average"]["value"] > 0
    assert output["breakdown"]["nutrients"]["value"] == pytest.approx(0.1 / 0.12)
    assert output["stoichiometry"]["coefficients_mol_per_C_mol"]["H2O"] == pytest.approx(0.735)
    assert output["stoichiometry"]["coefficients_mol_per_C_mol"]["O2"] == pytest.approx(-1.1375)
    changed = service.edit_set_value(workspace.id, current["id"], "k_d", {"value": 0.1, "unit": "1/hour", "expected_unit": "1/hour"}, current["revision"], current["digest"])
    assert changed["revision"] != card["parameter_set_revision"]
    assert service.evaluate_card(workspace.id, card["id"], operating) == output
    with pytest.raises(BioModelError, match="value and unit"):
        service.evaluate_card(workspace.id, card["id"], {**operating, "T": 298.15})
    with pytest.raises(BioModelError, match="not compatible"):
        service.evaluate_card(workspace.id, card["id"], {**operating, "T": {"value": 298.15, "unit": "m"}})


def test_operator_units_validity_and_basis_refs_are_preserved_and_checked(workspace) -> None:
    basis = make_numeric_basis(workspace.id, 0.08, "1/hour")
    created = service.create_set(workspace.id, "Unit and validity")
    edited = service.edit_set_value(workspace.id, created["id"], "mu_max", {
        "value": 1.92, "unit": "1/day", "expected_unit": "1/hour", "basis_ref": basis,
        "validity_range": {"min": 0.01, "max": 0.1, "inclusive_min": True, "inclusive_max": False}},
        created["revision"], created["digest"])
    record = edited["values"]["mu_max"]
    assert record["entered_value"] == 1.92 and record["entered_unit"] == "1/day"
    assert record["canonical_value"] == pytest.approx(0.08)
    assert record["si_value"] == pytest.approx(0.08 / 3600)
    assert record["validity_range"] == {"min": 0.01, "max": 0.1, "inclusive_min": True, "inclusive_max": False}
    with pytest.raises(BioModelError, match="unsupported or missing"):
        service.edit_set_value(workspace.id, created["id"], "mu_max", {
            "value": 0.08, "unit": "1/hour", "expected_unit": "1/hour",
            "basis_ref": {"object_type": "literature_entry", "object_id": "../outside", "locator_confirmed": True}},
            edited["revision"], edited["digest"])
    with pytest.raises(BioModelError, match="Literature"):
        service.edit_set_value(workspace.id, created["id"], "mu_max", {
            "value": 0.08, "unit": "1/hour", "expected_unit": "1/hour",
            "basis_ref": {"object_type": "literature_entry", "object_id": str(uuid.uuid4())}},
            edited["revision"], edited["digest"])


def test_dimensionless_numeric_entry_without_unit_is_compared_and_raw_needs_action_confirmation(workspace) -> None:
    basis = make_numeric_basis(workspace.id, 0.7, None)
    created = service.create_set(workspace.id, "Dimensionless")
    edited = service.edit_set_value(workspace.id, created["id"], "beta", {
        "value": 0.7, "unit": "1", "expected_unit": "1", "basis_ref": basis}, created["revision"], created["digest"])
    assert service.verify_value(workspace.id, created["id"], "beta", edited["revision"], edited["digest"])["values"]["beta"]["state"] == "source_verified"
    mismatch = service.create_set(workspace.id, "Dimensionless mismatch")
    mismatch_edit = service.edit_set_value(workspace.id, mismatch["id"], "beta", {
        "value": 5, "unit": "1", "expected_unit": "1", "basis_ref": basis}, mismatch["revision"], mismatch["digest"])
    with pytest.raises(BioModelError, match=r"entered 5\.0 dimensionless; source reports 0\.7 dimensionless"):
        service.verify_value(workspace.id, mismatch["id"], "beta", mismatch_edit["revision"], mismatch_edit["digest"])

    dimensional_basis = make_numeric_basis(workspace.id, 0.08, None)
    dimensioned = service.create_set(workspace.id, "Missing source unit")
    dimensioned_edit = service.edit_set_value(workspace.id, dimensioned["id"], "mu_max", {
        "value": 0.08, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": dimensional_basis}, dimensioned["revision"], dimensioned["digest"])
    with pytest.raises(BioModelError, match="no unit"):
        service.verify_value(workspace.id, dimensioned["id"], "mu_max", dimensioned_edit["revision"], dimensioned_edit["digest"])

    raw_basis = make_numeric_basis(workspace.id, 0.08, "1/hour", state="raw")
    raw_set = service.create_set(workspace.id, "Raw operator source")
    raw_edit = service.edit_set_value(workspace.id, raw_set["id"], "mu_max", {
        "value": 0.08, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": raw_basis}, raw_set["revision"], raw_set["digest"])
    with pytest.raises(BioModelError, match="Confirm the shown locator"):
        service.verify_value(workspace.id, raw_set["id"], "mu_max", raw_edit["revision"], raw_edit["digest"])
    verified = service.verify_value(workspace.id, raw_set["id"], "mu_max", raw_edit["revision"], raw_edit["digest"], locator_confirmed=True,
                                    locator_confirmation="line 2")
    provenance = verified["values"]["mu_max"]
    assert provenance["basis_ref"] == raw_basis
    assert provenance["verification"]["locator_confirmed"] is True
    assert provenance["verification"]["locator_confirmation"] == "line 2"
    assert provenance["verification"]["resolved"]["source"]["state"] == "raw"
    assert provenance["provenance"]["entry_state"] == "raw"


def test_current_revision_recomputes_digest_and_rejects_tampered_head_ids(workspace) -> None:
    created = service.create_set(workspace.id, "Tamper check")
    root = service._path(workspace.id, "sets", created["id"])
    revision_path = root / "revisions" / f"{created['revision']}.json"
    revision = service._read(revision_path)
    revision["document"]["name"] = "Tampered"
    service._write(revision_path, revision)
    with pytest.raises(BioModelError, match="digest"):
        service.get_set(workspace.id, created["id"])
    service._write(root / "head.json", {"revision": "../../outside", "digest": created["digest"], "history": []})
    with pytest.raises(BioModelError, match="revision id"):
        service.get_set(workspace.id, created["id"])


def test_duplicate_drops_source_verification_and_stale_read_is_not_persisted(workspace) -> None:
    basis = make_numeric_basis(workspace.id)
    created = service.create_set(workspace.id, "Verified set")
    edited = service.edit_set_value(workspace.id, created["id"], "mu_max", {
        "value": 0.08, "unit": "1/hour", "expected_unit": "1/hour", "basis_ref": basis}, created["revision"], created["digest"])
    verified = service.verify_value(workspace.id, created["id"], "mu_max", edited["revision"], edited["digest"])
    duplicate = service.duplicate_set(workspace.id, created["id"], verified["revision"], verified["digest"])
    assert duplicate["values"]["mu_max"]["state"] == "candidate"
    assert duplicate["values"]["mu_max"]["verification"] is None
    assert "display_state" not in service._read(service._path(workspace.id, "sets", duplicate["id"]) / "revisions" / f"{duplicate['revision']}.json")["document"]["values"]["mu_max"]


def test_bio_model_http_routes_refuse_conflicts_invalid_ids_and_traversal(workspace) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        base = f"/workspaces/{workspace.id}/bio-models"
        created_response = client.post(f"{base}/sets", json={"name": "HTTP set"})
        assert created_response.status_code == 201
        created = created_response.json()
        write = {"expected_revision": created["revision"], "expected_digest": created["digest"],
                 "value": 0.1, "unit": "1/hour", "expected_unit": "1/hour"}
        updated_response = client.put(f"{base}/sets/{created['id']}/values/mu_max", json=write)
        assert updated_response.status_code == 200, updated_response.text
        stale_response = client.put(f"{base}/sets/{created['id']}/values/mu_max", json=write)
        assert stale_response.status_code == 409
        invalid_symbol = client.put(f"{base}/sets/{created['id']}/values/not-a-form-symbol", json={**write, "expected_revision": updated_response.json()["revision"], "expected_digest": updated_response.json()["digest"]})
        assert invalid_symbol.status_code == 422
        invalid_id = client.get(f"{base}/sets/not%2Fvalid")
        assert invalid_id.status_code == 404
        traversal = client.get(f"{base}/sets/%2E%2E%2Foutside")
        assert traversal.status_code in {404, 422}


def test_indexed_nutrients_eilers_droop_light_dark_arrhenius_and_optics_evaluate(workspace) -> None:
    current = service.create_set(workspace.id, "Extended evaluation")
    parameters = {"I_opt": (400, "umol/(m**2*s)"), "beta": (0.7, "1"), "T_ref": (298.15, "K"),
                  "E_a": (40000, "J/mol"), "K_j_0": (0.02, "kg/m3"), "K_j_1": (0.03, "kg/m3"),
                  "I_dark": (5, "umol/(m**2*s)"), "m_L": (0.001, "1/hour"), "m_D": (0.004, "1/hour"),
                  "a": (1.8, "1"), "b": (0.5, "1"), "c": (0.1, "1"), "d": (0.01, "1"), "w_ash": (0.05, "1")}
    for symbol, (value, unit) in parameters.items():
        current = service.edit_set_value(workspace.id, current["id"], symbol,
            {"value": value, "unit": unit, "expected_unit": unit}, current["revision"], current["digest"])
    factors = {"light": "light.eilers_peeters_steady", "optics": "optics.slab_mean_irradiance",
               "temperature": "temperature.arrhenius_ref", "nutrients": ["nutrient.monod", "nutrient.monod"],
               "combination": "combine.liebig", "loss": "loss.light_dark", "stoichiometry": "stoich.photoautotrophic"}
    card = service.create_card(workspace.id, "Two nutrient light model", current["id"], factors, {"value": 0.08, "unit": "1/hour"})
    operating = {"I0": {"value": 800, "unit": "umol/(m**2*s)"}, "k_X": {"value": 120, "unit": "m**2/kg"},
                 "X": {"value": 0.7, "unit": "kg/m3"}, "L": {"value": 0.05, "unit": "m"},
                 "T": {"value": 298.15, "unit": "K"}, "S_0": {"value": 0.1, "unit": "kg/m3"},
                 "S_1": {"value": 0.2, "unit": "kg/m3"}}
    result = service.evaluate_card(workspace.id, card["id"], operating)
    assert result["breakdown"]["nutrients"]["value"] == pytest.approx(min(0.1 / 0.12, 0.2 / 0.23))
    assert result["breakdown"]["loss"]["value"] == pytest.approx(0.001)
    assert result["stoichiometry"]["coefficients_mol_per_C_mol"]["O2"] == pytest.approx(-1.1375)
    droop_params = service.edit_set_value(workspace.id, current["id"], "Q_min_0",
        {"value": 0.1, "unit": "kg/kg", "expected_unit": "kg/kg"}, current["revision"], current["digest"])
    droop_card = service.create_card(workspace.id, "Droop model", current["id"],
        {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.isothermal",
         "nutrients": ["nutrient.droop"], "combination": "combine.multiplicative", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"},
        {"value": 0.08, "unit": "1/hour"})
    with pytest.raises(BioModelError, match="K_I is required"):
        service.evaluate_card(workspace.id, droop_card["id"], {**operating, "Q_0": {"value": 0.2, "unit": "kg/kg"}})
    # Complete the remaining required parameters and prove the quota path itself evaluates.
    for symbol, value, unit in (("K_I", 200, "umol/(m**2*s)"), ("k_d", 0.003, "1/hour")):
        droop_params = service.edit_set_value(workspace.id, current["id"], symbol,
            {"value": value, "unit": unit, "expected_unit": unit}, droop_params["revision"], droop_params["digest"])
    droop_card = service.create_card(workspace.id, "Droop model complete", current["id"],
        {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.isothermal",
         "nutrients": ["nutrient.droop"], "combination": "combine.multiplicative", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"},
        {"value": 0.08, "unit": "1/hour"})
    droop_result = service.evaluate_card(workspace.id, droop_card["id"], {**operating, "Q_0": {"value": 0.2, "unit": "kg/kg"}})
    assert droop_result["breakdown"]["nutrients"]["value"] == pytest.approx(0.5)
