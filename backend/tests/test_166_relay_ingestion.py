from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.modules.relay_gateway import service


def test_failed_relay_run_does_not_ingest_actions(monkeypatch, tmp_path: Path) -> None:
    run = {
        "workspace_id": "workspace-166",
        "thread_id": "thread-166",
        "model": "relay-model",
        "source_interaction_id": "interaction-166",
        "action_context_json": json.dumps({"surface": "process", "base_revision": "rev-1"}),
    }
    finished: dict[str, object] = {}
    called = False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params):
            nonlocal called
            if sql.startswith("SELECT workspace_id"):
                return SimpleNamespace(fetchone=lambda: run)
            if "UPDATE relay_runs SET actions_ingested" in sql:
                called = True
                return SimpleNamespace(rowcount=1)
            raise AssertionError(sql)

        def commit(self):
            pass

    def forbid_ingest(*_args, **_kwargs):
        raise AssertionError("failed Relay output must not be ingested")

    monkeypatch.setattr(service, "open_sqlite_connection", Connection)
    monkeypatch.setattr(service, "_ingest_relay_actions", forbid_ingest)
    monkeypatch.setattr(service, "_record_changes", lambda *_args: None)
    monkeypatch.setattr(service, "_finish", lambda run_id, state, **kwargs: finished.update(state=state, **kwargs))
    output = tmp_path / "relay-result.json"
    output.write_text(json.dumps({"turns": [{"exit_code": 1, "text": "```jarvis-actions\n{}\n```"}]}))

    service._record_result({}, "run-166", "relay-workspace", 0, output)

    assert called  # The once-only ingestion CAS is still consumed for this result.
    assert finished["state"] == "failed"
    assert finished["reason_code"] == "relay_agent_error"
