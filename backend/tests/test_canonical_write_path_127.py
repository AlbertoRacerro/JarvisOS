from __future__ import annotations

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.modeling.models import RequirementCreate
from app.modules.modeling.routes import router
from app.modules.modeling.service import create_requirement


def _initialize() -> None:
    initialize_storage(seed_default=True)


def _create_requirement(statement: str = "Maximum pressure must be bounded"):
    return create_requirement(
        "main",
        RequirementCreate(statement=statement, status="active"),
    )


def _event_count(requirement_id: str) -> int:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM events
            WHERE event_type = 'RequirementCanonicalEdited' AND target_id = ?
            """,
            (requirement_id,),
        ).fetchone()
    return int(row["count"])


def _stored(requirement_id: str):
    with open_sqlite_connection() as connection:
        return connection.execute(
            "SELECT * FROM requirements WHERE id = ?",
            (requirement_id,),
        ).fetchone()


def test_requirement_patch_uses_workspace_cas_and_one_owner_audit() -> None:
    _initialize()
    client = TestClient(create_app())
    requirement = _create_requirement()

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={
            "workspace_id": "main",
            "expected_updated_at": requirement.updated_at,
            "statement": "Maximum pressure must remain below the design limit",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["statement"] == "Maximum pressure must remain below the design limit"
    assert body["updated_at"] != requirement.updated_at
    assert _event_count(requirement.id) == 1


def test_requirement_patch_stale_and_wrong_workspace_are_atomic_and_coded() -> None:
    _initialize()
    client = TestClient(create_app())
    requirement = _create_requirement()
    before = _stored(requirement.id)

    stale = client.patch(
        f"/requirements/{requirement.id}",
        json={
            "workspace_id": "main",
            "expected_updated_at": "stale-token",
            "statement": "stale overwrite",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "owner_stale"

    foreign = client.patch(
        f"/requirements/{requirement.id}",
        json={
            "workspace_id": "other-workspace",
            "expected_updated_at": requirement.updated_at,
            "statement": "cross-workspace overwrite",
        },
    )
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["code"] == "owner_not_found"

    after = _stored(requirement.id)
    assert dict(after) == dict(before)
    assert _event_count(requirement.id) == 0


def test_requirement_patch_rejects_generic_lifecycle_authority() -> None:
    _initialize()
    client = TestClient(create_app())
    requirement = _create_requirement()
    before = _stored(requirement.id)

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={
            "workspace_id": "main",
            "expected_updated_at": requirement.updated_at,
            "status": "retired",
        },
    )

    assert response.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])
    assert dict(_stored(requirement.id)) == dict(before)
    assert _event_count(requirement.id) == 0


def test_requirement_patch_validates_complete_merged_state_before_commit() -> None:
    _initialize()
    client = TestClient(create_app())
    requirement = _create_requirement()
    before = _stored(requirement.id)

    invalid = client.patch(
        f"/requirements/{requirement.id}",
        json={
            "workspace_id": "main",
            "expected_updated_at": requirement.updated_at,
            "criterion_output_name": "pressure",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "requirement_invalid"
    assert dict(_stored(requirement.id)) == dict(before)
    assert _event_count(requirement.id) == 0

    valid = client.patch(
        f"/requirements/{requirement.id}",
        json={
            "workspace_id": "main",
            "expected_updated_at": requirement.updated_at,
            "basis_kind": "acceptance_criterion",
            "criterion_output_name": "pressure",
            "criterion_operator": "<=",
            "criterion_expected_value": "10",
            "criterion_expected_unit": "bar",
            "criterion_rule_version": "v1",
        },
    )
    assert valid.status_code == 200
    body = valid.json()
    assert body["basis_kind"] == "acceptance_criterion"
    assert body["criterion_output_name"] == "pressure"
    assert _event_count(requirement.id) == 1


def test_requirement_patch_noop_preserves_timestamp_and_audit() -> None:
    _initialize()
    client = TestClient(create_app())
    requirement = _create_requirement()

    response = client.patch(
        f"/requirements/{requirement.id}",
        json={
            "workspace_id": "main",
            "expected_updated_at": requirement.updated_at,
        },
    )

    assert response.status_code == 200
    assert response.json()["updated_at"] == requirement.updated_at
    assert _event_count(requirement.id) == 0


def test_modeling_mutation_route_inventory_is_explicit() -> None:
    observed = {
        (method, route.path)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    expected = {
        ("POST", "/workspaces/{workspace_id}/model-specs"),
        ("POST", "/workspaces/{workspace_id}/assumptions"),
        ("POST", "/workspaces/{workspace_id}/parameters"),
        ("PATCH", "/parameters/{parameter_id}"),
        ("POST", "/parameters/{parameter_id}/lifecycle"),
        ("POST", "/workspaces/{workspace_id}/requirements"),
        ("PATCH", "/requirements/{requirement_id}"),
        ("POST", "/workspaces/{workspace_id}/simulation-runs"),
        ("POST", "/workspaces/{workspace_id}/decisions"),
    }
    assert observed == expected
