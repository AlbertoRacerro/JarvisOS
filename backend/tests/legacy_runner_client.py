from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from app.modules.runner.models import ModelImplementationCreate, RunnerJobCreate
from app.modules.runner.safety import RunnerSafetyError
from app.modules.runner.service import (
    create_model_implementation,
    create_runner_job,
    run_runner_job,
)

_RUN_PATH = re.compile(r"^/runner-jobs/([^/]+)/run$")


@dataclass(frozen=True)
class LegacyServiceResponse:
    """Small HTTP-like response for internal runner-engine regression tests."""

    status_code: int
    payload: Any

    @property
    def text(self) -> str:
        return json.dumps(self.payload, sort_keys=True, default=str)

    def json(self) -> Any:
        return self.payload


class LegacyRunnerTestClient:
    """Route legacy runner tests to the internal engine, never the public API.

    The public executable-source boundary is covered by test_runner_bundled_boundary.
    These older suites continue to verify the retained AST, execution, output,
    artifact, timeout, and MemoryStore engine behavior without restoring caller
    source authority to FastAPI request models.
    """

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._client.get(url, **kwargs)

    def post(self, url: str, *, json: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        payload = json or {}
        run_match = _RUN_PATH.fullmatch(url)
        try:
            if run_match is not None:
                response = run_runner_job(run_match.group(1))
                return LegacyServiceResponse(200, response.model_dump(mode="json"))
            if url.endswith("/model-implementations") and "script_text" in payload:
                workspace_id = _workspace_id(url)
                response = create_model_implementation(
                    workspace_id,
                    ModelImplementationCreate.model_validate(payload),
                )
                return LegacyServiceResponse(201, response.model_dump(mode="json"))
            if url.endswith("/runner-jobs"):
                workspace_id = _workspace_id(url)
                response = create_runner_job(
                    workspace_id,
                    RunnerJobCreate.model_validate(payload),
                )
                return LegacyServiceResponse(201, response.model_dump(mode="json"))
        except RunnerSafetyError as exc:
            return _runner_error(exc)
        return self._client.post(url, json=json, **kwargs)


def _workspace_id(url: str) -> str:
    parts = url.split("/")
    try:
        index = parts.index("workspaces")
        return parts[index + 1]
    except (ValueError, IndexError) as exc:
        raise AssertionError(f"Legacy runner test URL has no workspace: {url}") from exc


def _runner_error(exc: RunnerSafetyError) -> LegacyServiceResponse:
    not_found = {
        "runner_workspace_not_found",
        "runner_model_spec_not_found",
        "runner_model_version_not_found",
        "runner_job_not_found",
        "runner_simulation_run_not_found",
    }
    conflicts = {"runner_job_not_queued"}
    status = 404 if exc.code in not_found else 409 if exc.code in conflicts else 400
    return LegacyServiceResponse(
        status,
        {"detail": {"code": exc.code, "message": exc.message}},
    )
