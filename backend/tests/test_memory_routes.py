from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.core.paths import build_paths
from app.main import create_app
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def _workspace(name: str = "test-ws") -> str:
    initialize_database()
    return create_workspace(WorkspaceCreate(name=name, slug=f"ws-{uuid4().hex[:8]}", status="active")).id


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_search_isolation_across_workspaces(client: TestClient, tmp_path: Path) -> None:
    ws_a = _workspace("ws-alpha")
    ws_b = _workspace("ws-beta")

    folder_a = tmp_path / "docs_a"
    folder_a.mkdir()
    (folder_a / "alpha.md").write_text("# Biomass\n\nBiomass yield reached 4.2 g/L.\n", encoding="utf-8")

    # Register and refresh in workspace A
    res_reg = client.put(f"/workspaces/{ws_a}/memory/documents", json={"folder": str(folder_a)})
    assert res_reg.status_code == 200
    assert res_reg.json() == {"registered": True, "folder_name": "docs_a"}

    res_ref = client.post(f"/workspaces/{ws_a}/memory/documents/refresh")
    assert res_ref.status_code == 200
    assert res_ref.json()["included"] == 1

    # Search in ws_a returns the hit
    res_search_a = client.get(f"/workspaces/{ws_a}/memory/search?q=Biomass")
    assert res_search_a.status_code == 200
    items_a = res_search_a.json()
    assert len(items_a) == 1
    assert items_a[0]["owner"] == "workspace_documents"
    assert items_a[0]["authority_owner"] == "workspace_documents"
    assert "Biomass" in items_a[0]["excerpt"]
    assert items_a[0]["relative_path"] == "alpha.md"
    assert items_a[0]["heading"] == "Biomass"

    # Search in ws_b returns nothing
    res_search_b = client.get(f"/workspaces/{ws_b}/memory/search?q=Biomass")
    assert res_search_b.status_code == 200
    assert res_search_b.json() == []


def test_register_refresh_search_and_delete_lifecycle(client: TestClient, tmp_path: Path) -> None:
    ws = _workspace("ws-lifecycle")

    folder = tmp_path / "project_docs"
    sub = folder / "sub"
    sub.mkdir(parents=True)
    (sub / "guide.md").write_text(
        "# Catalysis\n\nEnzyme catalysis acceleration is measured.\n\n## Kinetics\n\nMichaelis-Menten kinetics apply.\n",
        encoding="utf-8",
    )

    # 1. Register folder
    put_res = client.put(f"/workspaces/{ws}/memory/documents", json={"folder": str(folder)})
    assert put_res.status_code == 200
    assert put_res.json() == {"registered": True, "folder_name": "project_docs"}
    assert str(folder) not in put_res.text

    # 2. Inspect registered folder via GET
    get_docs = client.get(f"/workspaces/{ws}/memory/documents")
    assert get_docs.status_code == 200
    assert get_docs.json() == {"registered": True, "folder_name": "project_docs"}
    assert str(folder) not in get_docs.text

    # 3. Refresh folder
    ref_res = client.post(f"/workspaces/{ws}/memory/documents/refresh")
    assert ref_res.status_code == 200
    ref_data = ref_res.json()
    assert ref_data["included"] == 2
    assert ref_data["changed"] == 2
    assert ref_data["skipped"] == 0
    assert ref_data["deleted"] == 0
    assert str(folder) not in ref_res.text

    # 4. Search finds the passage with relative path and heading
    search_res = client.get(f"/workspaces/{ws}/memory/search?q=catalysis")
    assert search_res.status_code == 200
    items = search_res.json()
    assert len(items) >= 1
    match = next((item for item in items if "catalysis" in item["excerpt"].lower()), None)
    assert match is not None
    assert match["relative_path"] == "sub/guide.md"
    assert match["heading"] == "Catalysis"
    assert match["title"] == "Enzyme catalysis acceleration is measured."
    assert match["location"] == "sub/guide.md#Catalysis"
    assert match["owner"] == "workspace_documents"
    assert "digest" not in match
    assert "content_digest" not in match
    assert "revision" not in match
    assert "object_id" not in match
    assert str(folder) not in search_res.text

    # 5. DELETE removes registration and passages are no longer found
    del_res = client.delete(f"/workspaces/{ws}/memory/documents")
    assert del_res.status_code == 200
    assert del_res.json() == {"registered": False, "folder_name": None}

    get_docs_after = client.get(f"/workspaces/{ws}/memory/documents")
    assert get_docs_after.json() == {"registered": False, "folder_name": None}

    search_after = client.get(f"/workspaces/{ws}/memory/search?q=catalysis")
    assert search_after.status_code == 200
    assert search_after.json() == []


def test_refusals_criterion_3(client: TestClient, tmp_path: Path) -> None:
    ws = _workspace("ws-refusals")

    # Relative path
    res = client.put(f"/workspaces/{ws}/memory/documents", json={"folder": "relative/folder"})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "folder_invalid"
    assert "relative" not in res.text

    # Missing folder
    missing = tmp_path / "does_not_exist"
    res = client.put(f"/workspaces/{ws}/memory/documents", json={"folder": str(missing)})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "folder_invalid"
    assert str(missing) not in res.text

    # Symlink
    real_target = tmp_path / "real_dir"
    real_target.mkdir()
    symlink_dir = tmp_path / "symlink_dir"
    symlink_dir.symlink_to(real_target, target_is_directory=True)
    res = client.put(f"/workspaces/{ws}/memory/documents", json={"folder": str(symlink_dir)})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "folder_invalid"
    assert str(symlink_dir) not in res.text

    # File
    real_file = tmp_path / "single_file.md"
    real_file.write_text("Hello", encoding="utf-8")
    res = client.put(f"/workspaces/{ws}/memory/documents", json={"folder": str(real_file)})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "folder_invalid"
    assert str(real_file) not in res.text

    # Folder inside data root
    data_dir = build_paths().data_root / "inside_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    res = client.put(f"/workspaces/{ws}/memory/documents", json={"folder": str(data_dir)})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "folder_invalid"
    assert str(data_dir) not in res.text

    # Folder inside repository checkout
    import app
    repo_sub = Path(app.__file__).resolve().parent / "modules"
    res = client.put(f"/workspaces/{ws}/memory/documents", json={"folder": str(repo_sub)})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "folder_invalid"
    assert str(repo_sub) not in res.text


def test_refresh_409_without_registration(client: TestClient) -> None:
    ws = _workspace("ws-no-reg")
    res = client.post(f"/workspaces/{ws}/memory/documents/refresh")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "folder_not_registered"


def test_404_on_unknown_workspace(client: TestClient, tmp_path: Path) -> None:
    initialize_database()
    unknown = "ws-nonexistent-12345"

    res = client.get(f"/workspaces/{unknown}/memory/search?q=test")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "workspace_not_found"

    res = client.get(f"/workspaces/{unknown}/memory/documents")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "workspace_not_found"

    res = client.put(f"/workspaces/{unknown}/memory/documents", json={"folder": str(tmp_path)})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "workspace_not_found"

    res = client.post(f"/workspaces/{unknown}/memory/documents/refresh")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "workspace_not_found"

    res = client.delete(f"/workspaces/{unknown}/memory/documents")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "workspace_not_found"


def test_query_validation_422(client: TestClient) -> None:
    ws = _workspace("ws-query-val")

    # Empty query
    res = client.get(f"/workspaces/{ws}/memory/search?q=")
    assert res.status_code == 422

    # Whitespace query
    res = client.get(f"/workspaces/{ws}/memory/search?q=%20%20%20")
    assert res.status_code == 422

    # Query > 2000 chars
    long_q = "a" * 2001
    res = client.get(f"/workspaces/{ws}/memory/search?q={long_q}")
    assert res.status_code == 422


def test_no_agent_tool_references_registration_route_or_register_folder() -> None:
    """Assert no agent tool, broker method or MCP descriptor mentions /memory/documents or register_folder."""
    import app
    from app.modules.agents.hermes.broker_mcp import _DECISION_TOOL, _RETRIEVAL_TOOL, _TOOL
    from app.modules.engineering.bench import list_evaluators

    # Check hermes broker MCP descriptors
    for descriptor in (_TOOL, _RETRIEVAL_TOOL, _DECISION_TOOL):
        desc_str = str(descriptor)
        assert "/memory/documents" not in desc_str
        assert "register_folder" not in desc_str

    # Check engineering bench evaluator descriptors
    evaluators = list_evaluators()
    for ev in evaluators:
        ev_str = str(ev)
        assert "/memory/documents" not in ev_str
        assert "register_folder" not in ev_str

    # Scan python source files under agents, ai, engineering/bench for descriptors
    repo_root = Path(app.__file__).resolve().parents[1]
    targets = [
        repo_root / "backend/app/modules/agents",
        repo_root / "backend/app/modules/ai",
        repo_root / "backend/app/modules/engineering/bench.py",
    ]
    for target in targets:
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        for file in files:
            # Skip the test file itself, memory_routes.py and document_intake.py (which are the intake implementation and HTTP route)
            if file.name in ("memory_routes.py", "document_intake.py", "test_memory_routes.py", "test_document_intake.py"):
                continue
            text = file.read_text(encoding="utf-8")
            assert "/memory/documents" not in text, f"Found /memory/documents in {file}"
            assert "register_folder" not in text, f"Found register_folder in {file}"
