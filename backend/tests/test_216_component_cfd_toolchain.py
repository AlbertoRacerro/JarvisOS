"""A Design component reports the checked registry state without starting a solver."""

from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.main import app
from app.modules.bluecad.registry import ToolRegistryError
from tests.test_216_component_reduced_models import _project


def test_cfd_toolchain_reports_registry_refusals_and_verified_pins(monkeypatch) -> None:
    initialize_database()
    checked = []

    def resolve(tool_id: str) -> dict:
        checked.append(tool_id)
        if tool_id == "openfoam_checkmesh":
            raise ToolRegistryError("TOOL_DISABLED", "disabled")
        return {"version_pin": "v2412"}

    monkeypatch.setattr("app.modules.workspaces.design_components.resolve_tool", resolve)
    with TestClient(app) as client:
        _, base, component_id = _project(client, "cfd-tools")
        response = client.get(f"{base}/{component_id}/cfd-toolchain")
        assert response.status_code == 200, response.text
        assert response.json() == {"binary_verified": False, "tools": [
            {"tool_id": "openfoam_blockmesh", "binary_verified": True, "version_pin": "v2412", "reason": None},
            {"tool_id": "openfoam_checkmesh", "binary_verified": False, "version_pin": None,
             "reason": "TOOL_DISABLED"},
            {"tool_id": "openfoam_simplefoam", "binary_verified": True, "version_pin": "v2412", "reason": None},
        ]}
        assert checked == ["openfoam_blockmesh", "openfoam_checkmesh", "openfoam_simplefoam"]
        assert client.get(f"{base}/missing/cfd-toolchain").status_code == 404
