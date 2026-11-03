"""Workflow safety policy for a public repository: GitHub-hosted runners only, no untrusted code with write authority.

* every job runs on a literal GitHub-hosted label (no self-hosted switch, no variable indirection);
* ``pull_request_target`` / ``issue_comment`` workflows never check out PR-controlled refs;
* secrets are never exposed to a job that checks out a pull-request head.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
HOSTED_LABELS = {"ubuntu-latest", "ubuntu-24.04"}
PR_CONTROLLED_REF = re.compile(r"pull_request\.head|github\.head_ref|inputs\.(expected_head_sha|branch)|pr\.data\.head")
# The browser proof deliberately executes the exact head of a same-repository PR on a disposable hosted VM, with a
# read-only token, as an unprivileged user, after an owner-applied label (see exact-head-browser-proof*.yml).
OWNER_GATED_HEAD_EXECUTION = {"exact-head-browser-proof.yml"}


def _workflows() -> dict[str, dict[str, Any]]:
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(WORKFLOWS.glob("*.yml"))}


def _triggers(workflow: dict[str, Any]) -> set[str]:
    on = workflow.get("on", workflow.get(True))  # PyYAML parses a bare `on` key as boolean True
    return set(on) if isinstance(on, dict) else ({on} if isinstance(on, str) else set(on or []))


def test_every_job_uses_a_literal_github_hosted_runner() -> None:
    for name, workflow in _workflows().items():
        for job_id, job in workflow["jobs"].items():
            assert job["runs-on"] in HOSTED_LABELS, f"{name}:{job_id} runs-on must be a literal hosted label"


def test_no_self_hosted_switch_remains() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "JARVIS_RUNNER" not in text and "self-hosted" not in text, path.name


def test_privileged_triggers_never_check_out_pull_request_controlled_refs() -> None:
    for name, workflow in _workflows().items():
        if not _triggers(workflow) & {"pull_request_target", "issue_comment", "workflow_run"}:
            continue
        for job_id, job in workflow["jobs"].items():
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    ref = str(step.get("with", {}).get("ref", ""))
                    assert not PR_CONTROLLED_REF.search(ref), f"{name}:{job_id} checks out a PR-controlled ref"


def test_secrets_are_not_exposed_to_jobs_that_check_out_a_pull_request_head() -> None:
    for name, workflow in _workflows().items():
        for job_id, job in workflow["jobs"].items():
            steps = job["steps"]
            checks_out_head = any(
                str(step.get("uses", "")).startswith("actions/checkout")
                and PR_CONTROLLED_REF.search(str(step.get("with", {}).get("ref", "")))
                for step in steps
            )
            if checks_out_head:
                assert name in OWNER_GATED_HEAD_EXECUTION, f"{name}:{job_id} runs PR-head code"
                text = yaml.safe_dump(job)
                assert "secrets." not in text, f"{name}:{job_id} exposes secrets next to PR-head code"


def test_ci_cancels_superseded_runs() -> None:
    ci = _workflows()["ci.yml"]
    assert ci["concurrency"]["cancel-in-progress"] is True
    assert "pull_request.number" in ci["concurrency"]["group"]
