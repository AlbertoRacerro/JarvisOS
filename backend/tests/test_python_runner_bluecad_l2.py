from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _model_spec(client: TestClient) -> str:
    response = client.post(
        "/workspaces/main/model-specs",
        json={"title": "BLUECAD L2", "engineering_question": "Build bounded BLUECAD script."},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _geometry_spec() -> dict[str, object]:
    return {
        "spec_version": "bluecad_geometry_spec_v0_1",
        "name": "runner tube",
        "parts": [
            {
                "part_id": "tube_1",
                "kind": "tube_run",
                "params": {"outer_d": 10, "wall_t": 1, "length": 40},
            }
        ],
        "connections": [],
    }


def _safe_script() -> str:
    return """
import json
import math
from pathlib import Path

input_file = Path('input.json')
output_dir = Path('.')
json.loads(input_file.read_text(encoding='utf-8'))
for name, content in {
    'model.step': 'ISO-10303-21;',
    'model.stl': 'solid model endsolid model',
    'model.glb': 'glb-placeholder',
    'manifest.json': json.dumps({'unit': 'mm', 'value': math.sqrt(4)}),
}.items():
    (output_dir / name).write_text(content, encoding='utf-8')
(output_dir / 'result.json').write_text(json.dumps({
    'schema_version': 1,
    'status': 'succeeded',
    'outputs': {},
    'artifacts': [
        {'path': 'model.step', 'role': 'bluecad_step', 'artifact_type': 'bluecad_step'},
        {'path': 'model.stl', 'role': 'bluecad_stl', 'artifact_type': 'bluecad_stl'},
        {'path': 'model.glb', 'role': 'bluecad_glb', 'artifact_type': 'bluecad_glb'},
        {'path': 'manifest.json', 'role': 'bluecad_manifest', 'artifact_type': 'bluecad_manifest'},
    ],
}), encoding='utf-8')
""".strip()


def _create_l2(client: TestClient, script: str | None = None) -> dict[str, object]:
    response = client.post(
        "/workspaces/main/model-implementations",
        json={
            "model_spec_id": _model_spec(client),
            "version_label": "bluecad-l2-v0",
            "implementation_kind": "bluecad_l2_v0",
            "script_text": script or _safe_script(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_bluecad_l2_registration_stores_supplied_script_artifact(client: TestClient) -> None:
    script = "import json\nfrom pathlib import Path\n"
    implementation = _create_l2(client, script)
    script_path = Path(str(implementation["script_path"]))
    assert script_path.name == "bluecad_l2.py"
    assert script_path.read_text(encoding="utf-8") == script
    from app.modules.runner.safety import sha256_file

    assert implementation["script_sha256"] == sha256_file(script_path)


def test_bluecad_l2_valid_geometry_spec_queues_and_writes_input_on_run(client: TestClient) -> None:
    implementation = _create_l2(client)
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    )
    assert response.status_code == 201, response.text
    job = response.json()["runner_job"]
    assert job["status"] == "queued"
    assert Path(job["input_file"]).exists() is False

    run = client.post(f"/runner-jobs/{job['id']}/run")
    assert run.status_code == 200, run.text
    assert Path(job["input_file"]).exists() is True


def test_bluecad_l2_invalid_geometry_spec_fails_before_queueing(client: TestClient) -> None:
    implementation = _create_l2(client)
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": {"spec_version": "bad", "parts": []}},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "runner_input_invalid"


def test_bluecad_l2_safe_script_persists_required_artifacts(client: TestClient) -> None:
    implementation = _create_l2(client)
    job = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    ).json()["runner_job"]
    response = client.post(f"/runner-jobs/{job['id']}/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runner_job"]["status"] == "succeeded"
    artifacts = client.get(f"/workspaces/main/simulation-runs/{body['simulation_run']['id']}/artifacts").json()
    assert {artifact["role"] for artifact in artifacts} == {
        "bluecad_step",
        "bluecad_stl",
        "bluecad_glb",
        "bluecad_manifest",
    }
    assert all(artifact["sha256"] for artifact in artifacts)


@pytest.mark.parametrize("module", ["os", "sys", "subprocess", "socket", "requests", "httpx", "urllib", "importlib"])
def test_bluecad_l2_rejects_disallowed_imports(client: TestClient, module: str) -> None:
    implementation = _create_l2(client, f"import {module}\n")
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "SANDBOX_VIOLATION"


@pytest.mark.parametrize(
    "source",
    [
        "from . import x\n",
        "from math import *\n",
        "eval('1')\n",
        "exec('x=1')\n",
        "__import__('os')\n",
        "builtins.eval('1')\n",
        "builtins.exec('x=1')\n",
        "builtins.__import__('os')\n",
        "f = eval\nf('1')\n",
        "f = eval\nmod = f(\"__import__('o'+'s')\")\nmod.listdir('.')\n",
        "g = getattr\ng(x, 'y')\n",
    ],
)
def test_bluecad_l2_rejects_dynamic_or_unknown_import_forms(client: TestClient, source: str) -> None:
    implementation = _create_l2(client, source)
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "SANDBOX_VIOLATION"


def test_bluecad_l2_ast_policy_allows_build123d_and_stdlib_script() -> None:
    from app.modules.runner.safety import preflight_bluecad_l2_ast_policy

    preflight_bluecad_l2_ast_policy(
        """
import build123d
import json
import math
from pathlib import Path

output_dir = Path(".")
payload = {"ok": math.sqrt(4)}
(output_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
""".strip()
    )


def test_bluecad_l2_missing_required_artifact_fails(client: TestClient) -> None:
    script = _safe_script().replace(
        "{'path': 'model.glb', 'role': 'bluecad_glb', 'artifact_type': 'bluecad_glb'},", ""
    )
    implementation = _create_l2(client, script)
    job = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    ).json()["runner_job"]
    response = client.post(f"/runner-jobs/{job['id']}/run")
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "runner_bluecad_output_invalid"


def test_bluecad_l2_artifact_path_escape_fails(client: TestClient) -> None:
    script = _safe_script().replace("{'path': 'model.step'", "{'path': '../model.step'")
    implementation = _create_l2(client, script)
    job = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    ).json()["runner_job"]
    response = client.post(f"/runner-jobs/{job['id']}/run")
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "runner_artifact_path_outside_output_dir"


def test_bluecad_l2_tamper_rejected_at_job_creation_and_execution(client: TestClient) -> None:
    implementation = _create_l2(client)
    script_path = Path(str(implementation["script_path"]))
    script_path.write_text(script_path.read_text(encoding="utf-8") + "\n# tamper", encoding="utf-8")
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "runner_script_hash_mismatch"

    from app.core.database import open_sqlite_connection
    from app.modules.runner.safety import sha256_file

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE artifacts SET sha256 = ? WHERE id = ?",
            (sha256_file(script_path), implementation["implementation_artifact_id"]),
        )
        connection.commit()
    job = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    ).json()["runner_job"]
    script_path.write_text(script_path.read_text(encoding="utf-8") + "\n# tamper again", encoding="utf-8")
    response = client.post(f"/runner-jobs/{job['id']}/run")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "runner_script_hash_mismatch"


def test_bluecad_l2_execution_policy_failure_marks_job_failed(client: TestClient) -> None:
    implementation = _create_l2(client)
    job = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _geometry_spec()},
    ).json()["runner_job"]
    script_path = Path(str(implementation["script_path"]))
    script_path.write_text("f = eval\nf('1')\n", encoding="utf-8")

    response = client.post(f"/runner-jobs/{job['id']}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["runner_job"]["status"] == "failed"
    assert body["simulation_run"]["status"] == "failed"
    assert body["error"]["code"] == "SANDBOX_VIOLATION"


@pytest.mark.parametrize(
    "module",
    [
        "build123d",
        "collections",
        "collections.abc",
        "dataclasses",
        "decimal",
        "enum",
        "functools",
        "itertools",
        "json",
        "math",
        "operator",
        "pathlib",
        "statistics",
        "typing",
    ],
)
def test_bluecad_l2_ast_policy_allows_explicit_import_roots(module: str) -> None:
    from app.modules.runner.safety import preflight_bluecad_l2_ast_policy

    preflight_bluecad_l2_ast_policy(f"import {module}\n")
    preflight_bluecad_l2_ast_policy(f"from {module} import placeholder\n")
