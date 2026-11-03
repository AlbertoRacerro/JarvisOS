import math

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import DraftOp


def test_design_versions_preserve_legacy_data_and_isolate_new_work() -> None:
    initialize_database()
    with TestClient(app) as client:
        created = client.post("/workspaces", json={"name": "Foundry", "slug": "foundry"})
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        original = draft.create_draft(project_id, "Original C1")

        path = f"/workspaces/{project_id}/design-versions"
        first = client.get(path).json()
        assert first["active_version_id"] == project_id
        assert first["versions"][0]["data_workspace_id"] == project_id
        assert first["versions"][0]["starts_empty"] is False

        created_version = client.post(path, json={
            "label": "C1 refinement", "expected_active_version_id": project_id, "mode": "empty",
        })
        assert created_version.status_code == 201, created_version.text
        candidate = created_version.json()["versions"][1]
        assert candidate["parent_version_id"] == project_id
        assert candidate["starts_empty"] is True
        assert candidate["data_workspace_id"] != project_id
        assert all(row["id"] != candidate["data_workspace_id"] for row in client.get("/workspaces").json())
        other_project = client.post("/workspaces", json={"name": "Other", "slug": "other"}).json()["id"]
        foreign = client.post(f"/workspaces/{other_project}/design-versions/activate", json={
            "version_id": candidate["id"], "expected_active_version_id": other_project,
        })
        assert foreign.status_code == 404

        switched = client.post(f"{path}/activate", json={
            "version_id": candidate["id"], "expected_active_version_id": project_id,
        })
        assert switched.status_code == 200, switched.text
        assert switched.json()["active_version_id"] == candidate["id"]
        assert draft.list_drafts(candidate["data_workspace_id"]) == []
        new_draft = draft.create_draft(candidate["data_workspace_id"], "New context")
        assert [row["draft_id"] for row in draft.list_drafts(project_id)] == [original["draft_id"]]
        assert [row["draft_id"] for row in draft.list_drafts(candidate["data_workspace_id"])] == [new_draft["draft_id"]]

        stale = client.post(f"{path}/activate", json={
            "version_id": project_id, "expected_active_version_id": project_id,
        })
        assert stale.status_code == 409
        stale_create = client.post(path, json={
            "label": "Stale child", "expected_active_version_id": project_id,
        })
        assert stale_create.status_code == 409
        restored = client.post(f"{path}/activate", json={
            "version_id": project_id, "expected_active_version_id": candidate["id"],
        })
        assert restored.status_code == 200
        assert restored.json()["active_version_id"] == project_id


def test_existing_workspace_is_backfilled_without_changing_its_identity() -> None:
    initialize_database()
    with open_sqlite_connection() as connection:
        connection.execute("""
            INSERT INTO workspaces (id, name, slug, status, created_at, updated_at)
            VALUES ('legacy', 'Legacy', 'legacy', 'active', '2025-01-01', '2025-01-01')
        """)
        connection.commit()
    original = draft.create_draft("legacy", "Preserved")

    initialize_database()
    with TestClient(app) as client:
        versions = client.get("/workspaces/legacy/design-versions")
        assert versions.status_code == 200
        assert versions.json()["active_version_id"] == "legacy"
        assert versions.json()["versions"][0]["data_workspace_id"] == "legacy"
        assert draft.list_drafts("legacy")[0]["draft_id"] == original["draft_id"]


def test_component_tree_scopes_identity_and_rejects_cycles_and_stale_edits() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id = client.post("/workspaces", json={"name": "Tree", "slug": "tree"}).json()["id"]
        base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
        root = client.post(base, json={
            "name": "Plant unit", "expected_active_version_id": project_id,
        })
        assert root.status_code == 201, root.text
        root_id = root.json()["id"]
        parent_id = root_id
        for depth in range(4):
            child = client.post(base, json={
                "name": f"Assembly {depth}", "parent_id": parent_id,
                "expected_active_version_id": project_id,
            })
            assert child.status_code == 201, child.text
            parent_id = child.json()["id"]
        assert len(client.get(base).json()) == 5

        cycle = client.put(f"{base}/{root_id}", json={
            "name": "Plant unit", "parent_id": parent_id, "expected_revision": 1,
            "expected_active_version_id": project_id,
        })
        assert cycle.status_code == 409
        rename = client.put(f"{base}/{root_id}", json={
            "name": "Renamed unit", "parent_id": None, "expected_revision": 1,
            "expected_active_version_id": project_id,
        })
        assert rename.status_code == 200, rename.text
        assert rename.json()["revision"] == 2
        assert client.put(f"{base}/{root_id}", json={
            "name": "Stale", "expected_revision": 1,
            "expected_active_version_id": project_id,
        }).status_code == 409

        versions_path = f"/workspaces/{project_id}/design-versions"
        new_version = client.post(versions_path, json={
            "label": "New", "expected_active_version_id": project_id, "mode": "empty",
        }).json()["versions"][1]["id"]
        assert client.get(f"{versions_path}/{new_version}/components").json() == []
        assert client.post(f"{versions_path}/{new_version}/components", json={
            "name": "Cross-version child", "parent_id": root_id,
            "expected_active_version_id": project_id,
        }).status_code == 409
        client.post(f"{versions_path}/activate", json={
            "version_id": new_version, "expected_active_version_id": project_id,
        })
        new_base = f"{versions_path}/{new_version}/components"
        assert client.post(new_base, json={
            "name": "Cross-version child", "parent_id": root_id,
            "expected_active_version_id": new_version,
        }).status_code == 404
        assert client.post(base, json={
            "name": "Stale version", "expected_active_version_id": project_id,
        }).status_code == 409


def test_process_unit_opens_one_version_scoped_component() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id = client.post("/workspaces", json={"name": "Unit entry", "slug": "unit-entry"}).json()["id"]
        process = draft.create_draft(project_id, "Plant")
        changed = draft.patch(project_id, process["draft_id"], process["revision"], [
            TypeAdapter(DraftOp).validate_python({"op": "add_unit", "id": "pump", "type": "Pump", "tag": "P1", "x": 0, "y": 0})
        ])
        base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
        payload = {"draft_id": process["draft_id"], "unit_id": "pump",
                   "expected_draft_revision": changed["revision"], "expected_active_version_id": project_id}
        first = client.post(f"{base}/from-unit", json=payload)
        assert first.status_code == 200, first.text
        assert first.json()["source_draft_id"] == process["draft_id"]
        assert first.json()["source_unit_id"] == "pump"
        again = client.post(f"{base}/from-unit", json=payload)
        assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
        assert len(client.get(base).json()) == 1
        assert client.post(f"{base}/from-unit", json={**payload, "unit_id": "missing"}).status_code == 404
        assert client.post(f"{base}/from-unit", json={**payload, "expected_draft_revision": "stale"}).status_code == 409

        with open_sqlite_connection() as connection:
            connection.execute(
                "INSERT INTO bluecad_candidates (id, workspace_id, brief_text, brief_digest, status, "
                "origin, loop_config_json, created_at, updated_at) VALUES "
                "('pump-cad', ?, 'pump', 'digest', 'valid', 'template', '{}', '2026-01-01', '2026-01-01')",
                (project_id,),
            )
            connection.commit()
        geometry_path = f"{base}/{first.json()['id']}/geometry"
        geometry_payload = {"candidate_id": "pump-cad", "expected_revision": 1,
                            "expected_active_version_id": project_id}
        assert client.put(geometry_path, json=geometry_payload).status_code == 422
        assert client.put(geometry_path, json={
            **geometry_payload, "expected_draft_revision": "stale",
        }).status_code == 409
        attached = client.put(geometry_path, json={
            **geometry_payload, "expected_draft_revision": changed["revision"],
        })
        assert attached.status_code == 200, attached.text
        assert attached.json()["geometry_source_draft_revision"] == changed["revision"]

        version = client.post(f"/workspaces/{project_id}/design-versions", json={
            "label": "Next", "expected_active_version_id": project_id, "mode": "empty",
        }).json()["versions"][1]
        client.post(f"/workspaces/{project_id}/design-versions/activate", json={
            "version_id": version["id"], "expected_active_version_id": project_id,
        })
        next_base = f"/workspaces/{project_id}/design-versions/{version['id']}/components"
        assert client.post(f"{next_base}/from-unit", json={
            **payload, "expected_active_version_id": version["id"],
        }).status_code == 404


def test_geometry_attachment_checks_version_candidate_and_component_revision() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id = client.post("/workspaces", json={"name": "Geometry", "slug": "geometry"}).json()["id"]
        base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
        component = client.post(base, json={
            "name": "Tube", "expected_active_version_id": project_id,
        }).json()
        next_version = client.post(f"/workspaces/{project_id}/design-versions", json={
            "label": "Next", "expected_active_version_id": project_id,
        }).json()["versions"][1]
        with open_sqlite_connection() as connection:
            for candidate_id, workspace_id, status in (
                ("valid-local", project_id, "valid"),
                ("pending-local", project_id, "generating"),
                ("valid-other", next_version["data_workspace_id"], "valid"),
            ):
                connection.execute(
                    "INSERT INTO bluecad_candidates (id, workspace_id, brief_text, brief_digest, status, "
                    "origin, loop_config_json, created_at, updated_at) VALUES (?, ?, 'tube', 'digest', ?, "
                    "'template', '{}', '2026-01-01', '2026-01-01')",
                    (candidate_id, workspace_id, status),
                )
            connection.commit()
        path = f"{base}/{component['id']}/geometry"
        payload = {"candidate_id": "valid-local", "expected_revision": 1,
                   "expected_active_version_id": project_id}
        assert client.put(path, json={**payload, "candidate_id": "valid-other"}).status_code == 404
        assert client.put(path, json={**payload, "candidate_id": "pending-local"}).status_code == 409
        attached = client.put(path, json=payload)
        assert attached.status_code == 200, attached.text
        assert attached.json()["geometry_candidate_id"] == "valid-local"
        assert attached.json()["revision"] == 2
        assert client.get(base).json()[0]["geometry_candidate_id"] == "valid-local"
        assert client.put(path, json=payload).status_code == 409
        client.post(f"/workspaces/{project_id}/design-versions/activate", json={
            "version_id": next_version["id"], "expected_active_version_id": project_id,
        })
        assert client.put(path, json={**payload, "expected_revision": 2}).status_code == 409


def test_deepen_maps_typed_process_dimensions_into_attached_tube_geometry() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id = client.post("/workspaces", json={"name": "Deepen", "slug": "deepen"}).json()["id"]
        process = draft.create_draft(project_id, "Plant")
        adapter = TypeAdapter(DraftOp)
        changed = draft.patch(project_id, process["draft_id"], process["revision"], [
            adapter.validate_python({"op": "add_unit", "id": "pfr", "type": "PFR", "tag": "R1", "x": 0, "y": 0}),
            adapter.validate_python({"op": "add_unit", "id": "pump", "type": "Pump", "tag": "P1", "x": 1, "y": 0}),
            adapter.validate_python({"op": "set_unit_params", "unit": "pfr", "mode": "adiabatic", "values": {
                "volume": {"value": 0.002, "unit": "m3"}, "length": {"value": 1, "unit": "m"}}}),
        ])
        base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
        entry = {"draft_id": process["draft_id"], "expected_draft_revision": changed["revision"],
                 "expected_active_version_id": project_id}
        reactor = client.post(f"{base}/from-unit", json={**entry, "unit_id": "pfr"}).json()
        pump = client.post(f"{base}/from-unit", json={**entry, "unit_id": "pump"}).json()

        mapping = client.get(f"{base}/{reactor['id']}/geometry/process-mapping").json()
        assert mapping["supported"] is True and mapping["template"] == "tube"
        assert abs(mapping["inner_d_mm"] - 50.46265) < 1e-4 and mapping["length_mm"] == 1000
        unsupported = client.get(f"{base}/{pump['id']}/geometry/process-mapping").json()
        assert unsupported["supported"] is False and "Pump" in unsupported["basis"]

        handoff = {"wall_t_mm": 3, "expected_revision": 1, "expected_active_version_id": project_id,
                   "expected_draft_revision": changed["revision"]}
        assert client.post(f"{base}/{pump['id']}/geometry/from-process", json=handoff).status_code == 422
        assert client.post(f"{base}/{reactor['id']}/geometry/from-process",
                           json={**handoff, "expected_draft_revision": "stale"}).status_code == 409
        assert client.post(f"{base}/{reactor['id']}/geometry/from-process",
                           json={**handoff, "wall_t_mm": 150}).status_code == 422
        deepened = client.post(f"{base}/{reactor['id']}/geometry/from-process", json=handoff)
        assert deepened.status_code == 200, deepened.text
        component = deepened.json()
        assert component["revision"] == 2
        assert component["geometry_source_draft_revision"] == changed["revision"]
        candidate = client.get(f"/workspaces/{project_id}/bluecad/candidates/{component['geometry_candidate_id']}")
        assert candidate.json()["status"] == "valid" and candidate.json()["origin"] == "template"
        assert client.post(f"{base}/{reactor['id']}/geometry/from-process", json=handoff).status_code == 409


def test_deepen_represents_an_80_m_loop_as_a_serpentine_and_keeps_short_tubes_unchanged() -> None:
    from app.modules.workspaces.design_components import _tube_mapping

    def pbr(length_m: float) -> dict:
        return {"type": "PhotobioreactorT1", "params": {"tube_inner_diameter": {"si": 0.045}, "tube_length": {"si": length_m}}}

    short = _tube_mapping(pbr(5.0), "r")
    assert short.template == "tube" and short.length_mm == 5000 and short.segment_count is None
    long = _tube_mapping(pbr(80.0), "r")
    assert long.supported and long.template == "serpentine" and long.bend_radius_mm == 100
    assert long.segment_count == 16 and long.segment_length_mm <= 5000
    centreline = long.segment_count * long.segment_length_mm + (long.segment_count - 1) * math.pi * long.bend_radius_mm
    assert abs(centreline - 80000) < 1e-6
    assert "16 runs + 15 U-bends" in long.basis and "centreline length preserved" in long.basis
    too_long = _tube_mapping(pbr(1000.0), "r")
    assert not too_long.supported and "64" in too_long.basis

    initialize_database()
    with TestClient(app) as client:
        project_id = client.post("/workspaces", json={"name": "Loop", "slug": "loop"}).json()["id"]
        process = draft.create_draft(project_id, "Plant")
        changed = draft.patch(project_id, process["draft_id"], process["revision"], [
            TypeAdapter(DraftOp).validate_python({"op": "add_unit", "id": "pfr", "type": "PFR", "tag": "R1", "x": 0, "y": 0}),
            TypeAdapter(DraftOp).validate_python({"op": "set_unit_params", "unit": "pfr", "mode": "adiabatic", "values": {
                "volume": {"value": 0.127, "unit": "m3"}, "length": {"value": 80, "unit": "m"}}}),
        ])
        base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
        reactor = client.post(f"{base}/from-unit", json={
            "draft_id": process["draft_id"], "expected_draft_revision": changed["revision"],
            "expected_active_version_id": project_id, "unit_id": "pfr"}).json()
        mapping = client.get(f"{base}/{reactor['id']}/geometry/process-mapping").json()
        assert mapping["template"] == "serpentine" and mapping["segment_count"] > 1
        deepened = client.post(f"{base}/{reactor['id']}/geometry/from-process", json={
            "wall_t_mm": 3, "expected_revision": 1, "expected_active_version_id": project_id,
            "expected_draft_revision": changed["revision"]})
        assert deepened.status_code == 200, deepened.text
        candidate = client.get(f"/workspaces/{project_id}/bluecad/candidates/{deepened.json()['geometry_candidate_id']}")
        assert candidate.json()["status"] == "valid" and candidate.json()["origin"] == "template"


def test_studio_models_attach_only_to_compatible_component_nodes() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id = client.post("/workspaces", json={"name": "Models", "slug": "models"}).json()["id"]
        base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
        component = client.post(base, json={"name": "Reactor", "expected_active_version_id": project_id}).json()
        next_version = client.post(f"/workspaces/{project_id}/design-versions", json={
            "label": "Next", "expected_active_version_id": project_id,
        }).json()["versions"][1]
        with open_sqlite_connection() as connection:
            for version_id, workspace_id, kind in (
                ("calc-local", project_id, "studio_calc_v1"),
                ("unit-local", project_id, "studio_unit_v1"),
                ("calc-other", next_version["data_workspace_id"], "studio_calc_v1"),
            ):
                connection.execute(
                    "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
                    "VALUES (?, ?, 'Correlation', 'q', '2026-01-01', '2026-01-01')", (f"spec-{version_id}", workspace_id),
                )
                connection.execute(
                    "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, implementation_kind, "
                    "created_at) VALUES (?, ?, ?, 'v1', ?, '2026-01-01')",
                    (version_id, workspace_id, f"spec-{version_id}", kind),
                )
                connection.execute(
                    "INSERT INTO studio_published_versions (version_id, model_spec_id, contract_json, source_sha256, "
                    "contract_sha256, created_at) VALUES (?, ?, '{}', 'src', 'contract', '2026-01-01')",
                    (version_id, f"spec-{version_id}"),
                )
            connection.commit()
        path = f"{base}/{component['id']}/models"
        payload = {"model_version_id": "calc-local", "expected_revision": 1, "expected_active_version_id": project_id}
        assert client.put(path, json={**payload, "model_version_id": "calc-other"}).status_code == 404
        plant_only = client.put(path, json={**payload, "model_version_id": "unit-local"})
        assert plant_only.status_code == 422 and "Plant" in plant_only.json()["detail"]
        attached = client.put(path, json=payload)
        assert attached.status_code == 200, attached.text
        assert attached.json()["revision"] == 2
        assert [(item["model_version_id"], item["node"], item["source_sha256"])
                for item in attached.json()["models"]] == [("calc-local", "Physics", "src")]
        assert client.put(path, json=payload).status_code == 409
        assert client.put(path, json={**payload, "expected_revision": 2}).status_code == 409
        assert client.get(base).json()[0]["models"][0]["model_version_id"] == "calc-local"


def _fork_fixture(client) -> dict:
    """A project with a hierarchy, a Process unit link, attached geometry and a pinned model."""

    project_id = client.post("/workspaces", json={"name": "Fork", "slug": "fork"}).json()["id"]
    process = draft.create_draft(project_id, "Plant")
    changed = draft.patch(project_id, process["draft_id"], process["revision"], [
        TypeAdapter(DraftOp).validate_python({"op": "add_unit", "id": "pump", "type": "Pump", "tag": "P1", "x": 0, "y": 0})
    ])
    base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
    root = client.post(base, json={"name": "Plant", "expected_active_version_id": project_id}).json()
    child = client.post(base, json={"name": "Skid", "parent_id": root["id"], "expected_active_version_id": project_id}).json()
    leaf = client.post(base, json={"name": "Joint", "parent_id": child["id"], "expected_active_version_id": project_id}).json()
    pump = client.post(f"{base}/from-unit", json={
        "draft_id": process["draft_id"], "unit_id": "pump", "expected_draft_revision": changed["revision"],
        "expected_active_version_id": project_id}).json()
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO bluecad_candidates (id, workspace_id, brief_text, brief_digest, status, origin, "
            "loop_config_json, created_at, updated_at) VALUES ('fork-cad', ?, 'tube', 'digest', 'valid', "
            "'template', '{}', '2026-01-01', '2026-01-01')", (project_id,))
        connection.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
            "VALUES ('fork-spec', ?, 'Correlation', 'q', '2026-01-01', '2026-01-01')", (project_id,))
        connection.execute(
            "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, implementation_kind, "
            "created_at) VALUES ('fork-model', ?, 'fork-spec', 'v1', 'studio_calc_v1', '2026-01-01')", (project_id,))
        connection.execute(
            "INSERT INTO studio_published_versions (version_id, model_spec_id, contract_json, source_sha256, "
            "contract_sha256, created_at) VALUES ('fork-model', 'fork-spec', '{}', 'src', 'contract', '2026-01-01')")
        connection.commit()
    assert client.put(f"{base}/{pump['id']}/geometry", json={
        "candidate_id": "fork-cad", "expected_revision": 1, "expected_active_version_id": project_id,
        "expected_draft_revision": changed["revision"]}).status_code == 200
    assert client.put(f"{base}/{leaf['id']}/models", json={
        "model_version_id": "fork-model", "expected_revision": 1, "expected_active_version_id": project_id}).status_code == 200
    return {"project_id": project_id, "draft": changed, "base": base, "root": root["id"], "child": child["id"],
            "leaf": leaf["id"], "pump": pump["id"]}


def _counts() -> tuple[int, int]:
    with open_sqlite_connection() as connection:
        return (connection.execute("SELECT COUNT(*) FROM bluecad_candidates").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])


def test_fork_version_inherits_content_by_reference_and_isolates_edits() -> None:
    initialize_database()
    with TestClient(app) as client:
        fx = _fork_fixture(client)
        project_id, path = fx["project_id"], f"/workspaces/{fx['project_id']}/design-versions"
        before = _counts()
        created = client.post(path, json={"label": "v2", "expected_active_version_id": project_id})
        assert created.status_code == 201, created.text
        version = created.json()["versions"][1]
        assert version["source_mode"] == "fork" and version["starts_empty"] is False
        assert _counts() == before
        ws = version["data_workspace_id"]

        forked = draft.list_drafts(ws)
        assert [row["draft_id"] for row in forked] == [fx["draft"]["draft_id"]]
        parent_view, child_view = draft.projection(project_id, fx["draft"]["draft_id"]), draft.projection(ws, forked[0]["draft_id"])
        assert child_view["objects"] == parent_view["objects"]
        directory = draft.draft_dir(ws, forked[0]["draft_id"])
        provenance = draft.load_revision(directory, forked[0]["revision"])["provenance"]["forked_from"]
        assert provenance == {"workspace_id": project_id, "draft_id": fx["draft"]["draft_id"],
                              "revision": fx["draft"]["revision"]}

        parent_rows = {c["name"]: c for c in client.get(fx["base"]).json()}
        child_base = f"{path}/{version['id']}/components"
        child_rows = {c["name"]: c for c in client.get(child_base).json()}
        assert set(child_rows) == set(parent_rows) and len(child_rows) == 4
        assert {c["id"] for c in child_rows.values()}.isdisjoint({c["id"] for c in parent_rows.values()})
        assert child_rows["Plant"]["parent_id"] is None
        assert child_rows["Skid"]["parent_id"] == child_rows["Plant"]["id"]
        assert child_rows["Joint"]["parent_id"] == child_rows["Skid"]["id"]
        pump = next(c for c in child_rows.values() if c["source_unit_id"] == "pump")
        assert pump["source_draft_id"] == fx["draft"]["draft_id"]
        assert pump["geometry_candidate_id"] == "fork-cad" and pump["geometry_workspace_id"] == project_id
        assert pump["geometry_source_draft_revision"] == forked[0]["revision"]
        assert [(m["model_version_id"], m["node"], m["source_sha256"], m["contract_sha256"])
                for m in child_rows["Joint"]["models"]] == [("fork-model", "Physics", "src", "contract")]

        client.post(f"{path}/activate", json={"version_id": version["id"], "expected_active_version_id": project_id})
        draft.patch(ws, forked[0]["draft_id"], forked[0]["revision"], [
            TypeAdapter(DraftOp).validate_python({"op": "add_unit", "id": "tank", "type": "Pump", "tag": "T1", "x": 1, "y": 1})])
        assert client.put(f"{child_base}/{child_rows['Joint']['id']}", json={
            "name": "Joint v2", "parent_id": child_rows["Skid"]["id"], "expected_revision": 1,
            "expected_active_version_id": version["id"]}).status_code == 200
        assert client.post(child_base, json={"name": "Extra", "expected_active_version_id": version["id"]}).status_code == 201
        assert draft.list_drafts(project_id)[0]["revision"] == fx["draft"]["revision"]
        assert {c["name"] for c in client.get(fx["base"]).json()} == set(parent_rows)

        # and the other direction: parent edits do not reach the child
        client.post(f"{path}/activate", json={"version_id": project_id, "expected_active_version_id": version["id"]})
        draft.patch(project_id, fx["draft"]["draft_id"], fx["draft"]["revision"], [
            TypeAdapter(DraftOp).validate_python({"op": "add_unit", "id": "valve", "type": "Pump", "tag": "P2", "x": 2, "y": 2})])
        assert client.put(f"{fx['base']}/{fx['root']}", json={
            "name": "Plant renamed", "parent_id": None, "expected_revision": 1,
            "expected_active_version_id": project_id}).status_code == 200
        assert {o["id"] for o in draft.projection(ws, forked[0]["draft_id"])["objects"]} >= {"pump", "tank"}
        assert "valve" not in {o["id"] for o in draft.projection(ws, forked[0]["draft_id"])["objects"]}
        assert "Plant" in {c["name"] for c in client.get(child_base).json()}

        with open_sqlite_connection() as connection:
            event = connection.execute("SELECT payload FROM events WHERE event_type = 'DesignVersionCreated' "
                                       "ORDER BY created_at DESC LIMIT 1").fetchone()
        assert '"mode": "fork"' in event[0] or '"mode":"fork"' in event[0]
        assert client.post(path, json={"label": "stale", "expected_active_version_id": version["id"]}).status_code == 409


def test_empty_mode_and_legacy_null_mode_rows_read_as_empty_starts() -> None:
    initialize_database()
    with TestClient(app) as client:
        fx = _fork_fixture(client)
        project_id, path = fx["project_id"], f"/workspaces/{fx['project_id']}/design-versions"
        empty = client.post(path, json={"label": "blank", "expected_active_version_id": project_id, "mode": "empty"})
        version = empty.json()["versions"][1]
        assert version["starts_empty"] is True and version["source_mode"] == "empty"
        assert draft.list_drafts(version["data_workspace_id"]) == []
        assert client.get(f"{path}/{version['id']}/components").json() == []
        with open_sqlite_connection() as connection:
            connection.execute("UPDATE design_project_versions SET source_mode = NULL WHERE id = ?", (version["id"],))
            connection.commit()
        rows = client.get(path).json()["versions"]
        assert rows[0]["starts_empty"] is False and rows[0]["source_mode"] is None
        assert rows[1]["starts_empty"] is True and rows[1]["source_mode"] is None


def test_failed_fork_leaves_no_version_row_or_workspace_directory(monkeypatch) -> None:
    from app.modules.workspaces import design_versions

    initialize_database()
    with TestClient(app, raise_server_exceptions=False) as client:
        fx = _fork_fixture(client)
        project_id, path = fx["project_id"], f"/workspaces/{fx['project_id']}/design-versions"
        real = draft.fork_drafts

        def boom(source: str, target: str):
            real(source, target)
            raise RuntimeError("disk failure")

        monkeypatch.setattr(design_versions.draft, "fork_drafts", boom)
        assert client.post(path, json={"label": "v2", "expected_active_version_id": project_id}).status_code == 500
        assert len(client.get(path).json()["versions"]) == 1
        with open_sqlite_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM workspaces WHERE status = 'design_version'").fetchone()[0] == 0
