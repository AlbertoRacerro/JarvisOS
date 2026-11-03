import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.setenv("DATABASE_URL", "must-not-enter-flowsheet")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _seed_graph_fixture(client: TestClient) -> dict[str, str]:
    second_workspace = client.post(
        "/workspaces",
        json={"name": "Other", "slug": "other", "description": "isolation fixture"},
    )
    assert second_workspace.status_code == 201, second_workspace.text
    other_workspace_id = second_workspace.json()["id"]

    ids = {
        "model_spec": "ms-050",
        "implementation_artifact": "artifact-implementation",
        "model_version": "mv-050",
        "input_parameter": "parameter-input",
        "output_parameter": "parameter-output",
        "assumption": "assumption-ai",
        "requirement": "requirement-context",
        "decision": "decision-run",
        "ai_job": "ai-job-linked",
        "orphan_ai_job": "ai-job-orphan",
        "run": "run-calc",
        "runner_job": "runner-calc",
        "result_artifact": "artifact-result",
        "parent_candidate": "candidate-parent",
        "candidate": "candidate-child",
        "attempt": "attempt-child-1",
        "spec_artifact": "artifact-bluecad-spec",
        "report_artifact": "artifact-bluecad-report",
        "bluecad_run": "run-bluecad",
        "evidence": "evidence-validation",
        "cross_parameter": "parameter-other-workspace",
        "other_workspace": other_workspace_id,
    }
    now = "2026-07-20T10:00:00+00:00"
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at)
            VALUES (?, 'main', 'Fixture model', 'Trace dependencies', ?, ?)
            """,
            (ids["model_spec"], now, now),
        )
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at, notes
            ) VALUES (?, 'main', 'implementation.py', '/private/implementation.py',
                      'python_script', 'text/x-python', ?, ?, 'registered', ?, 'private artifact note')
            """,
            (
                ids["implementation_artifact"],
                "1" * 64,
                f"model_spec:{ids['model_spec']}",
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO model_versions (
                id, workspace_id, model_spec_id, version_label, implementation_artifact_id,
                implementation_kind, status, created_at
            ) VALUES (?, 'main', ?, 'fixture-calc-v0', ?, 'calc_v0', 'ready', ?)
            """,
            (ids["model_version"], ids["model_spec"], ids["implementation_artifact"], now),
        )
        context_manifest = [
            {
                "source": f"parameter:{ids['input_parameter']}",
                "type": "parameter",
                "id": ids["input_parameter"],
            },
            {
                "source": f"requirement:{ids['requirement']}",
                "type": "requirement",
                "id": ids["requirement"],
            },
        ]
        for ai_id, context_sources in (
            (ids["ai_job"], json.dumps(context_manifest, sort_keys=True)),
            (ids["orphan_ai_job"], None),
        ):
            connection.execute(
                """
                INSERT INTO ai_jobs (
                    id, created_at, status, task_kind, route_reason_json, context_sources_json
                ) VALUES (?, ?, 'success', 'decision_support', '{}', ?)
                """,
                (ai_id, now, context_sources),
            )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, value_status, status, origin,
                source_ref, created_at, updated_at
            ) VALUES (?, 'main', 'Bound length', '20', 'm', 'explicit', 'accepted',
                      'user', NULL, ?, ?)
            """,
            (ids["input_parameter"], now, now),
        )
        connection.execute(
            """
            INSERT INTO assumptions (
                id, workspace_id, statement, status, origin, source_ref, source_ai_job_id,
                created_at, updated_at
            ) VALUES (?, 'main', 'FULL ASSUMPTION TEXT MUST NOT LEAK', 'proposed',
                      'ai_proposed', ?, ?, ?, ?)
            """,
            (
                ids["assumption"],
                f"parameter:{ids['input_parameter']}",
                ids["ai_job"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, status, rationale, created_at, updated_at
            ) VALUES (?, 'main', 'FULL REQUIREMENT TEXT MUST NOT LEAK', 'active',
                      'private rationale', ?, ?)
            """,
            (ids["requirement"], now, now),
        )
        calc_input = {
            "tube_length": {
                "value": 20.0,
                "unit": "m",
                "source_parameter_id": ids["input_parameter"],
            },
            "manual_value": {
                "value": 2.0,
                "unit": "1",
                "nested": {"other_id": ids["cross_parameter"]},
            },
        }
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, model_version_id, run_label, status, input_payload,
                created_at, notes
            ) VALUES (?, 'main', ?, 'fixture run', 'succeeded', ?, ?, 'private run note')
            """,
            (ids["run"], ids["model_version"], json.dumps(calc_input, sort_keys=True), now),
        )
        connection.execute(
            """
            INSERT INTO runner_jobs (
                id, workspace_id, simulation_run_id, runner_type, status, script_path,
                script_sha256, implementation_kind, working_dir, input_file, output_dir,
                timeout_seconds, max_stdout_bytes, max_stderr_bytes,
                max_output_json_bytes, max_artifact_bytes, created_at, updated_at
            ) VALUES (?, 'main', ?, 'python_local', 'succeeded', '/private/script.py', ?,
                      'calc_v0', '/private/work', '/private/input.json', '/private/output',
                      30, 1000, 1000, 1000, 1000, ?, ?)
            """,
            (ids["runner_job"], ids["run"], "2" * 64, now, now),
        )
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at
            ) VALUES (?, 'main', 'result.json', '/private/result.json', 'json',
                      'application/json', ?, ?, 'registered', ?)
            """,
            (ids["result_artifact"], "3" * 64, f"simulation_run:{ids['run']}", now),
        )
        connection.execute(
            """
            INSERT INTO run_artifacts (
                id, workspace_id, simulation_run_id, artifact_id, role, created_at
            ) VALUES ('run-artifact-link', 'main', ?, ?, 'calc_result_json', ?)
            """,
            (ids["run"], ids["result_artifact"], now),
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, value_status, status, origin,
                source_ref, created_at, updated_at
            ) VALUES (?, 'main', 'Calculated output', '42', '1', 'explicit', 'proposed',
                      'calc', ?, ?, ?)
            """,
            (ids["output_parameter"], f"runner_job:{ids['runner_job']}", now, now),
        )
        connection.execute(
            """
            INSERT INTO decisions (
                id, workspace_id, title, decision_text, rationale, status, origin,
                linked_run_id, source_ai_job_id, created_at, updated_at
            ) VALUES (?, 'main', 'Run decision', 'PRIVATE DECISION TEXT',
                      'PRIVATE DECISION RATIONALE', 'accepted', 'ai_proposed', ?, ?, ?, ?)
            """,
            (ids["decision"], ids["run"], ids["ai_job"], now, now),
        )
        for artifact_id, filename, source_ref, digest in (
            (
                ids["spec_artifact"],
                "geometry_spec.json",
                f"bluecad_candidate:{ids['candidate']}:attempt:1",
                "4" * 64,
            ),
            (
                ids["report_artifact"],
                "validation_report.json",
                f"bluecad_candidate:{ids['candidate']}:attempt:1:sim:{ids['bluecad_run']}",
                "5" * 64,
            ),
        ):
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, workspace_id, filename, stored_path, artifact_type, mime_type,
                    sha256, source_ref, status, created_at
                ) VALUES (?, 'main', ?, ?, 'bluecad_report', 'application/json',
                          ?, ?, 'registered', ?)
                """,
                (artifact_id, filename, f"/private/{filename}", digest, source_ref, now),
            )
        connection.execute(
            """
            INSERT INTO bluecad_candidates (
                id, workspace_id, brief_text, brief_digest, status, origin,
                loop_config_json, created_at, updated_at
            ) VALUES (?, 'main', 'PRIVATE PARENT BRIEF', ?, 'valid', 'ai', '{}', ?, ?)
            """,
            (ids["parent_candidate"], "sha256:" + "6" * 64, now, now),
        )
        connection.execute(
            """
            INSERT INTO bluecad_candidates (
                id, workspace_id, brief_text, brief_digest, status, spec_artifact_id,
                report_artifact_id, promoted_decision_id, origin, parent_candidate_id,
                loop_config_json, created_at, updated_at
            ) VALUES (?, 'main', 'PRIVATE CHILD BRIEF', ?, 'valid', ?, ?, ?, 'ai', ?,
                      '{}', ?, ?)
            """,
            (
                ids["candidate"],
                "sha256:" + "7" * 64,
                ids["spec_artifact"],
                ids["report_artifact"],
                ids["decision"],
                ids["parent_candidate"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO bluecad_attempts (
                id, candidate_id, attempt_no, route_class, proposal_ai_job_id,
                proposal_outcome, validation_verdict, spec_artifact_id,
                report_artifact_id, started_at
            ) VALUES (?, ?, 1, 'external:cheap', ?, 'ok', 'pass', ?, ?, ?)
            """,
            (
                ids["attempt"],
                ids["candidate"],
                ids["ai_job"],
                ids["spec_artifact"],
                ids["report_artifact"],
                now,
            ),
        )
        bluecad_input = {"candidate_id": ids["candidate"], "attempt_id": ids["attempt"]}
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, model_version_id, run_label, status, input_payload,
                parameter_payload, output_payload, created_at
            ) VALUES (?, 'main', NULL, 'bluecad advisory', 'completed', ?, '{}', '{}', ?)
            """,
            (ids["bluecad_run"], json.dumps(bluecad_input, sort_keys=True), now),
        )
        connection.execute(
            """
            INSERT INTO evidence_records (
                id, workspace_id, kind, verdict, metrics_json, source_run_id,
                candidate_id, attempt_id, report_artifact_id, created_at
            ) VALUES (?, 'main', 'validation_v0', 'pass', ?, ?, ?, ?, ?, ?)
            """,
            (
                ids["evidence"],
                json.dumps({"private_metric": 123}, sort_keys=True),
                ids["bluecad_run"],
                ids["candidate"],
                ids["attempt"],
                ids["report_artifact"],
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, value_status, status, origin,
                created_at, updated_at
            ) VALUES (?, ?, 'Other secret parameter', '999', 'kg', 'explicit', 'accepted',
                      'user', ?, ?)
            """,
            (ids["cross_parameter"], ids["other_workspace"], now, now),
        )
        connection.commit()
    return ids


def _table_counts() -> dict[str, int]:
    from app.core.database import open_sqlite_connection

    tables = (
        "model_specs",
        "model_versions",
        "simulation_runs",
        "runner_jobs",
        "artifacts",
        "assumptions",
        "parameters",
        "decisions",
        "requirements",
        "ai_jobs",
        "bluecad_candidates",
        "bluecad_attempts",
        "evidence_records",
        "events",
    )
    with open_sqlite_connection() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
            for table in tables
        }


def _edge_set(graph: dict[str, object]) -> set[tuple[str, str, str, str]]:
    return {
        (
            edge["upstream_ref"],
            edge["downstream_ref"],
            edge["relation"],
            edge["edge_class"],
        )
        for edge in graph["edges"]
    }


def test_flowsheet_graph_is_complete_deterministic_and_data_minimized(client: TestClient) -> None:
    ids = _seed_graph_fixture(client)
    before = _table_counts()
    first = client.get("/workspaces/main/flowsheet/graph")
    second = client.get("/workspaces/main/flowsheet/graph")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert _table_counts() == before

    graph = first.json()
    assert graph["is_acyclic"] is True
    assert len(graph["topological_order"]) == len(graph["nodes"])
    assert graph["diagnostics"]["manual_binding_count"] == 1
    assert "generated_at" not in graph
    assert graph["nodes"] == sorted(graph["nodes"], key=lambda item: (item["kind"], item["id"]))
    assert graph["edges"] == sorted(
        graph["edges"],
        key=lambda item: (
            item["upstream_ref"],
            item["downstream_ref"],
            item["relation"],
            item["edge_class"],
        ),
    )

    edges = _edge_set(graph)
    expected = {
        (f"model_spec:{ids['model_spec']}", f"model_version:{ids['model_version']}", "has_version", "dependency"),
        (
            f"artifact:{ids['implementation_artifact']}",
            f"model_version:{ids['model_version']}",
            "implementation_artifact",
            "dependency",
        ),
        (f"model_version:{ids['model_version']}", f"simulation_run:{ids['run']}", "configured_run", "dependency"),
        (f"parameter:{ids['input_parameter']}", f"simulation_run:{ids['run']}", "bound_input", "dependency"),
        (f"simulation_run:{ids['run']}", f"runner_job:{ids['runner_job']}", "executed_by", "provenance"),
        (
            f"simulation_run:{ids['run']}",
            f"artifact:{ids['result_artifact']}",
            "produced_artifact",
            "dependency",
        ),
        (f"runner_job:{ids['runner_job']}", f"parameter:{ids['output_parameter']}", "source_reference", "dependency"),
        (f"simulation_run:{ids['run']}", f"decision:{ids['decision']}", "informed_decision", "dependency"),
        (f"ai_job:{ids['ai_job']}", f"decision:{ids['decision']}", "proposed_record", "provenance"),
        (f"ai_job:{ids['ai_job']}", f"assumption:{ids['assumption']}", "proposed_record", "provenance"),
        (f"parameter:{ids['input_parameter']}", f"assumption:{ids['assumption']}", "source_reference", "dependency"),
        (f"parameter:{ids['input_parameter']}", f"ai_job:{ids['ai_job']}", "context_for", "provenance"),
        (f"requirement:{ids['requirement']}", f"ai_job:{ids['ai_job']}", "context_for", "provenance"),
        (
            f"bluecad_candidate:{ids['parent_candidate']}",
            f"bluecad_candidate:{ids['candidate']}",
            "parent_candidate",
            "dependency",
        ),
        (f"bluecad_candidate:{ids['candidate']}", f"bluecad_attempt:{ids['attempt']}", "has_attempt", "provenance"),
        (f"bluecad_attempt:{ids['attempt']}", f"artifact:{ids['spec_artifact']}", "attempt_artifact", "dependency"),
        (
            f"bluecad_candidate:{ids['candidate']}",
            f"simulation_run:{ids['bluecad_run']}",
            "candidate_simulation",
            "dependency",
        ),
        (
            f"bluecad_attempt:{ids['attempt']}",
            f"simulation_run:{ids['bluecad_run']}",
            "attempt_simulation",
            "dependency",
        ),
        (
            f"simulation_run:{ids['bluecad_run']}",
            f"artifact:{ids['report_artifact']}",
            "source_reference",
            "dependency",
        ),
        (
            f"simulation_run:{ids['bluecad_run']}",
            f"evidence:{ids['evidence']}",
            "supports_evidence",
            "dependency",
        ),
        (
            f"artifact:{ids['report_artifact']}",
            f"evidence:{ids['evidence']}",
            "reported_evidence",
            "dependency",
        ),
    }
    assert expected.issubset(edges)
    assert not any(ids["cross_parameter"] in value for edge in edges for value in edge)

    serialized = first.text
    for forbidden in (
        "20\"",
        "PRIVATE DECISION TEXT",
        "PRIVATE DECISION RATIONALE",
        "FULL ASSUMPTION TEXT MUST NOT LEAK",
        "FULL REQUIREMENT TEXT MUST NOT LEAK",
        "PRIVATE CHILD BRIEF",
        "private_metric",
        "/private/",
        "nested",
        "other_id",
    ):
        assert forbidden not in serialized


def test_node_resolver_alias_and_workspace_isolation(client: TestClient) -> None:
    ids = _seed_graph_fixture(client)
    parameter = client.get(f"/workspaces/main/flowsheet/nodes/parameter:{ids['input_parameter']}")
    assert parameter.status_code == 200
    assert parameter.json()["ref"] == f"parameter:{ids['input_parameter']}"
    assert '"value":' not in parameter.text

    evidence = client.get(f"/workspaces/main/flowsheet/nodes/evidence_record:{ids['evidence']}")
    assert evidence.status_code == 200
    assert evidence.json()["ref"] == f"evidence:{ids['evidence']}"

    linked_ai = client.get(f"/workspaces/main/flowsheet/nodes/ai_job:{ids['ai_job']}")
    orphan_ai = client.get(f"/workspaces/main/flowsheet/nodes/ai_job:{ids['orphan_ai_job']}")
    assert linked_ai.status_code == 200
    assert orphan_ai.status_code == 404

    cross = client.get(f"/workspaces/main/flowsheet/nodes/parameter:{ids['cross_parameter']}")
    absent = client.get("/workspaces/main/flowsheet/nodes/parameter:not-present")
    assert cross.status_code == absent.status_code == 404
    assert cross.json() == absent.json()

    for invalid_ref in ("parameter", "parameter:", "unknown:value", "parameter:a:b"):
        response = client.get(f"/workspaces/main/flowsheet/nodes/{invalid_ref}")
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "flowsheet_ref_invalid"


def test_diagnostics_are_bounded_and_no_recursive_id_inference_occurs(client: TestClient) -> None:
    ids = _seed_graph_fixture(client)
    from app.core.database import open_sqlite_connection

    now = "2026-07-20T10:01:00+00:00"
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE parameters SET source_ref = 'https://example.invalid/source' WHERE id = ?",
            (ids["output_parameter"],),
        )
        connection.execute(
            "UPDATE artifacts SET source_ref = 'runner_job:missing-job' WHERE id = ?",
            (ids["result_artifact"],),
        )
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, model_version_id, run_label, status, input_payload, created_at
            ) VALUES ('run-invalid-json', 'main', ?, 'invalid payload', 'failed', '{', ?)
            """,
            (ids["model_version"], now),
        )
        connection.commit()

    response = client.get("/workspaces/main/flowsheet/graph")
    assert response.status_code == 200, response.text
    graph = response.json()
    diagnostics = graph["diagnostics"]
    assert diagnostics["unsupported_reference_count"] == 1
    assert diagnostics["dangling_reference_count"] >= 1
    assert any(item["code"] == "payload_invalid" for item in diagnostics["unresolved_references"])
    assert not any(ids["cross_parameter"] in edge["upstream_ref"] for edge in graph["edges"])


def test_dependency_cycles_are_reported_without_mutation(client: TestClient) -> None:
    _seed_graph_fixture(client)
    from app.core.database import open_sqlite_connection

    now = "2026-07-20T10:02:00+00:00"
    with open_sqlite_connection() as connection:
        for parameter_id, source_ref in (
            ("cycle-a", "parameter:cycle-b"),
            ("cycle-b", "parameter:cycle-a"),
        ):
            connection.execute(
                """
                INSERT INTO parameters (
                    id, workspace_id, name, unit, value_status, status, origin,
                    source_ref, created_at, updated_at
                ) VALUES (?, 'main', ?, '1', 'missing', 'proposed', 'user', ?, ?, ?)
                """,
                (parameter_id, parameter_id, source_ref, now, now),
            )
        connection.commit()
    before = _table_counts()
    response = client.get("/workspaces/main/flowsheet/graph")
    assert response.status_code == 200
    graph = response.json()
    assert graph["is_acyclic"] is False
    assert graph["topological_order"] is None
    assert graph["diagnostics"]["cycle_count"] >= 1
    assert ["parameter:cycle-a", "parameter:cycle-b", "parameter:cycle-a"] in graph["diagnostics"]["cycles"]
    assert _table_counts() == before


def test_graph_limits_fail_without_partial_response(client: TestClient, monkeypatch) -> None:
    _seed_graph_fixture(client)
    from app.modules.flowsheet import service

    monkeypatch.setattr(service, "MAX_GRAPH_NODES", 1)
    response = client.get("/workspaces/main/flowsheet/graph")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "flowsheet_graph_limit_exceeded"
    assert detail["bound"] == "nodes"
    assert "nodes" not in response.json()
