"""Spec 216 contract 8/9: read-only fluid domain of a component's attached tube, the source of GUI study contracts."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.main import app
from app.modules.bluecad.cfd_pipe.domain import derive_fluid_domain
from tests.test_216_component_reduced_models import _project, _ready, _tube


def test_fluid_domain_is_derived_from_the_attached_geometry_without_changing_it() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "fd-ok")
        response = client.get(f"{base}/{component_id}/fluid-domain")
        assert response.status_code == 200, response.text
        expected = derive_fluid_domain(_tube(2600.0)).model_dump(mode="json")
        assert response.json() == expected
        assert expected["inner_diameter_m"] == 0.046 and expected["length_m"] == 2.6
        assert client.get(base).json()[0]["revision"] == revision


def test_fluid_domain_is_unknown_without_geometry_and_for_a_missing_component() -> None:
    initialize_database()
    with TestClient(app) as client:
        _, base, component_id = _project(client, "fd-nogeo")
        response = client.get(f"{base}/{component_id}/fluid-domain")
        assert response.status_code == 409
        assert response.json()["detail"] == "Component tube geometry is unknown (component has no attached geometry)."
        assert client.get(f"{base}/missing/fluid-domain").status_code == 404
