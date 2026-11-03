import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.setenv("DATABASE_URL", "should-not-enter-runner")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _model_spec(client: TestClient, title: str = "Calc V0") -> str:
    response = client.post(
        "/workspaces/main/model-specs",
        json={"title": title, "engineering_question": "Run a bounded engineering calculation."},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _safe_script() -> str:
    return r'''
import json
import math

with open('input.json', encoding='utf-8') as handle:
    inputs = json.load(handle)
force = inputs['density']['value'] * inputs['volume']['value'] * inputs['g']['value']
with open('result.json', 'w', encoding='utf-8') as handle:
    json.dump({
        'schema_version': 1,
        'status': 'succeeded',
        'outputs': {
            'buoyancy_force': {'value': force, 'unit': 'N'},
            'sqrt_g': {'value': math.sqrt(inputs['g']['value']), 'unit': 'sqrt(m/s^2)'},
        },
        'diagnostics': {'model': 'fixture'},
    }, handle, sort_keys=True, separators=(',', ':'))
'''.strip()


def _create_calc(client: TestClient, script: str | None = None) -> dict[str, object]:
    response = client.post(
        "/workspaces/main/model-implementations",
        json={
            "model_spec_id": _model_spec(client),
            "version_label": "calc-v0",
            "implementation_kind": "calc_v0",
            "script_text": script or _safe_script(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _valid_input() -> dict[str, object]:
    return {
        "density": {"value": 997, "unit": "kg/m^3", "source_parameter_id": "density-1"},
        "volume": {"value": 0.25, "unit": "m^3"},
        "g": {"value": 9.80665, "unit": "m/s^2"},
    }


def _create_job(client: TestClient, implementation: dict[str, object], input_set: dict[str, object] | None = None) -> dict[str, object]:
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": input_set or _valid_input()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_calc_v0_registration_stores_supplied_script_artifact(client: TestClient) -> None:
    implementation = _create_calc(client, "import json\n")
    script_path = Path(str(implementation["script_path"]))
    assert script_path.name == "calc_v0.py"
    assert script_path.read_text(encoding="utf-8") == "import json\n"
    assert implementation["script_sha256"] == hashlib.sha256(b"import json\n").hexdigest()

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        artifact = connection.execute(
            "SELECT artifact_type, sha256 FROM artifacts WHERE id = ?",
            (implementation["implementation_artifact_id"],),
        ).fetchone()
        version = connection.execute(
            "SELECT implementation_artifact_id, implementation_kind FROM model_versions WHERE id = ?",
            (implementation["id"],),
        ).fetchone()
    assert artifact["artifact_type"] == "python_script"
    assert artifact["sha256"] == implementation["script_sha256"]
    assert version["implementation_artifact_id"] == implementation["implementation_artifact_id"]
    assert version["implementation_kind"] == "calc_v0"


@pytest.mark.parametrize(
    "input_set",
    [
        {"x": {"unit": "m"}},
        {"x": {"value": True, "unit": "m"}},
        {"x": {"value": 1, "unit": ""}},
        {"x": {"value": 1, "unit": 3}},
    ],
)
def test_calc_v0_invalid_inputs_fail_before_queueing(client: TestClient, input_set: dict[str, object]) -> None:
    implementation = _create_calc(client)
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": input_set},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "runner_input_invalid"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM runner_jobs").fetchone()["count"] == 0


def test_calc_v0_success_persists_result_artifact_and_parameter_proposals(client: TestClient) -> None:
    implementation = _create_calc(client)
    job_response = _create_job(client, implementation)
    runner_job = job_response["runner_job"]
    input_file = Path(str(runner_job["input_file"]))
    assert input_file.exists() is False

    response = client.post(f"/runner-jobs/{runner_job['id']}/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runner_job"]["status"] == "succeeded"
    assert body["error"] is None
    assert input_file.exists() is True
    assert json.loads(input_file.read_text(encoding="utf-8")) == _valid_input()
    assert "should-not-enter-runner" not in json.dumps(body)
    assert body["runner_job"]["environment_metadata"] == {
        "inherited_environment": False,
        "allowlisted_keys": ["PYTHONHASHSEED", "PYTHONIOENCODING"],
    }

    artifacts = client.get(
        f"/workspaces/main/simulation-runs/{body['simulation_run']['id']}/artifacts"
    ).json()
    assert [(artifact["role"], artifact["filename"]) for artifact in artifacts] == [("calc_result_json", "result.json")]

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT name, value, unit, status, origin, source_ref FROM parameters ORDER BY name"
        ).fetchall()
    assert [row["name"] for row in rows] == ["buoyancy_force", "sqrt_g"]
    assert all(row["status"] == "proposed" for row in rows)
    assert all(row["origin"] == "calc" for row in rows)
    assert all(row["source_ref"] == f"runner_job:{runner_job['id']}" for row in rows)


@pytest.mark.parametrize(
    "script",
    [
        _safe_script().replace("'unit': 'N'", "'unit': ''"),
        _safe_script().replace(", 'unit': 'N'", ""),
        _safe_script().replace("'unit': 'N'", "'unit': 3"),
    ],
)
def test_calc_v0_missing_output_unit_fails_distinctly_without_parameter_records(
    client: TestClient, script: str
) -> None:
    implementation = _create_calc(client, script)
    runner_job = _create_job(client, implementation)["runner_job"]

    response = client.post(f"/runner-jobs/{runner_job['id']}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["runner_job"]["status"] == "failed"
    assert body["error"]["code"] == "runner_output_unit_missing"
    assert body["error"]["code"] not in {"runner_result_invalid_json", "SANDBOX_VIOLATION"}

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM parameters").fetchone()["count"] == 0


def test_calc_v0_nonfinite_output_value_fails_without_parameter_records(client: TestClient) -> None:
    script = _safe_script().replace("'value': force", "'value': 1e309")
    implementation = _create_calc(client, script)
    runner_job = _create_job(client, implementation)["runner_job"]

    response = client.post(f"/runner-jobs/{runner_job['id']}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["runner_job"]["status"] == "failed"
    assert body["error"]["code"] == "runner_result_invalid_json"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM parameters").fetchone()["count"] == 0


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        (_safe_script().replace(
            "'outputs': {\n            'buoyancy_force': {'value': force, 'unit': 'N'},\n            'sqrt_g': {'value': math.sqrt(inputs['g']['value']), 'unit': 'sqrt(m/s^2)'},\n        }",
            "'outputs': []",
        ), "runner_result_invalid_json"),
        (_safe_script().replace(", 'unit': 'N'", ""), "runner_output_unit_missing"),
        (
            _safe_script().replace("'diagnostics': {'model': 'fixture'}", "'diagnostics': float('nan')"),
            "runner_result_invalid_json",
        ),
    ],
)
def test_calc_v0_output_validation_errors_mark_job_failed_without_500(
    client: TestClient, script: str, expected_code: str
) -> None:
    implementation = _create_calc(client, script)
    runner_job = _create_job(client, implementation)["runner_job"]

    response = client.post(f"/runner-jobs/{runner_job['id']}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["runner_job"]["status"] == "failed"
    assert body["simulation_run"]["status"] == "failed"
    assert body["error"]["code"] == expected_code


@pytest.mark.parametrize(
    "source",
    [
        "import os\n",
        "import subprocess\n",
        "from os import path\n",
        "import sqlite3\n",
        "import http.client\n",
        "from . import x\n",
        "from math import *\n",
        "__import__('os')\n",
        "eval('1')\n",
        "exec('x=1')\n",
        "f = eval\nf(\"__import__('os')\")\n",
        "__builtins__.__dict__['eval']('__import__(\"os\").system(\"id\")')\n",
        "b = __builtins__\ne = b.__dict__['eval']\ne('import os')\n",
        "__builtins__.__dict__['exec']('x=1')\n",
        "__builtins__.__dict__['__import__']('pathlib').Path('/tmp/escape').write_text('x')\n",
        "import socket\n",
        "open('/tmp/escape', 'w')\n",
        "f = open\nf('/tmp/escape', 'w')\n",
        "__builtins__.open('/tmp/escape', 'w')\n",
        "__builtins__['open']('/tmp/escape', 'w')\n",
    ],
)
def test_calc_v0_policy_violations_are_sandbox_failures(client: TestClient, source: str) -> None:
    implementation = _create_calc(client, source)
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _valid_input()},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "SANDBOX_VIOLATION"


@pytest.mark.parametrize(
    "source",
    [
        "().__class__\n",
        "().__class__.__base__.__subclasses__()\n",
        "().__class__.__mro__\n",
        "().__class__.__dict__\n",
    ],
)
def test_calc_v0_dunder_introspection_fails_before_queueing(client: TestClient, source: str) -> None:
    implementation = _create_calc(client, source)
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _valid_input()},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "SANDBOX_VIOLATION"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM runner_jobs").fetchone()["count"] == 0


@pytest.mark.parametrize(
    "source",
    [
        "open('input.json', 'w')\n",
        "open('input.json', mode='w')\n",
        "mode = 'r'\nopen('input.json', mode=mode)\n",
        "open('input.json', 'r+')\n",
        "open('input.json', 'rb')\n",
        "open('result.json')\n",
        "open('result.json', 'r')\n",
        "open('result.json', mode='r')\n",
        "mode = 'w'\nopen('result.json', mode=mode)\n",
        "open('result.json', 'a')\n",
        "open('result.json', 'x')\n",
        "open('result.json', 'w+')\n",
        "open('result.json', 'wb')\n",
        "open('result.json', **{'mode': 'w'})\n",
    ],
)
def test_calc_v0_open_modes_are_checked(client: TestClient, source: str) -> None:
    implementation = _create_calc(client, source)
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _valid_input()},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "SANDBOX_VIOLATION"


def test_calc_v0_noncanonical_diagnostics_fails_before_artifact_or_memory_side_effects(client: TestClient) -> None:
    script = _safe_script().replace("'diagnostics': {'model': 'fixture'}", "'diagnostics': float('nan')")
    implementation = _create_calc(client, script)
    runner_job = _create_job(client, implementation)["runner_job"]

    response = client.post(f"/runner-jobs/{runner_job['id']}/run")

    assert response.status_code == 200
    body = response.json()
    assert body["runner_job"]["status"] == "failed"
    assert body["error"]["code"] == "runner_result_invalid_json"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM parameters").fetchone()["count"] == 0
        assert connection.execute("SELECT COUNT(*) AS count FROM run_artifacts").fetchone()["count"] == 0


def test_calc_v0_numeric_overflow_errors_map_to_validation_codes() -> None:
    from app.modules.runner.safety import RunnerSafetyError, validate_calc_v0_input
    from app.modules.runner.service import _validate_calc_v0_output

    class OverflowNumber:
        def __float__(self) -> float:
            raise OverflowError("too large")

    with pytest.raises(RunnerSafetyError) as input_error:
        validate_calc_v0_input({"x": {"value": OverflowNumber(), "unit": "m"}})
    assert input_error.value.code == "runner_input_invalid"

    with pytest.raises(RunnerSafetyError) as output_error:
        _validate_calc_v0_output({"outputs": {"x": {"value": OverflowNumber(), "unit": "m"}}})
    assert output_error.value.code == "runner_result_invalid_json"


def test_calc_v0_tamper_rejected_at_job_creation_and_execution(client: TestClient) -> None:
    implementation = _create_calc(client)
    script_path = Path(str(implementation["script_path"]))
    script_path.write_text(script_path.read_text(encoding="utf-8") + "\n# tamper", encoding="utf-8")
    response = client.post(
        "/workspaces/main/runner-jobs",
        json={"model_version_id": implementation["id"], "input_set": _valid_input()},
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
    runner_job = _create_job(client, implementation)["runner_job"]
    script_path.write_text(script_path.read_text(encoding="utf-8") + "\n# tamper again", encoding="utf-8")
    response = client.post(f"/runner-jobs/{runner_job['id']}/run")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "runner_script_hash_mismatch"


def test_calc_v0_result_json_is_deterministic_for_identical_input_bytes(client: TestClient) -> None:
    implementation = _create_calc(client)
    digests: list[str] = []
    for _ in range(2):
        runner_job = _create_job(client, implementation)["runner_job"]
        response = client.post(f"/runner-jobs/{runner_job['id']}/run")
        assert response.status_code == 200
        result_path = Path(str(runner_job["output_dir"])) / "result.json"
        digests.append(hashlib.sha256(result_path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]