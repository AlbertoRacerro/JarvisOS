from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from app.modules.coding.repository_truth import (
    RepositoryTruthError,
    RepositoryTruthResult,
    RepositoryTruthService,
)

STATUS_PATH = "docs/specs/STATUS.md"
CLAUDE_MARKER = "JARVIS_CLAUDE_REVIEW_V3_2_JSON:"
MAX_WARNINGS = 16
MAX_REASON_CHARS = 256
# CI jobs that must be green on the exact head (names of the jobs in .github/workflows/ci.yml; keep in sync with
# required_check_contexts in .github/merge-authority-policy.json).
REQUIRED_CHECKS = ("backend", "gen-check", "doc-paths", "leak-guard")

StageState = Literal[
    "pending",
    "complete",
    "blocked",
    "stale",
    "not_applicable",
    "unknown",
]

_STAGE_NAMES = (
    "proposal",
    "plan",
    "implementation",
    "tests",
    "independent_review",
    "reconciliation",
    "merge",
)
_RECOGNIZED_STATUS = frozenset(
    {"planned", "blocked", "ready", "in_progress", "in_review", "merged", "cancelled"}
)
_SPEC_ID_RE = re.compile(r"^[0-9]{3}[a-z]?$", re.ASCII)
_PR_LINK_RE = re.compile(r"(?:\[#|#)([1-9][0-9]*)(?:\]|\b)")
_BLOCKING_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
)
_REVIEW_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
_REVIEW_DISPOSITIONS = frozenset({"BLOCK", "PARK", "DROP"})


class PipelineStateInputError(ValueError):
    pass


@dataclass(frozen=True)
class StatusRow:
    spec_id: str
    status: str
    implementation_pr: int | None
    name: str
    depends_on: str


def _warning(code: str) -> str:
    return code[:MAX_REASON_CHARS]


def _stage(
    name: str,
    state: StageState,
    reason: str,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "state": state,
        "reason": reason[:MAX_REASON_CHARS],
        "evidence": evidence or {},
    }


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _implementation_pr(cell: str) -> int | None:
    if cell in {"", "—", "-"}:
        return None
    match = _PR_LINK_RE.search(cell)
    if match is None:
        return None
    return int(match.group(1))


def _parse_status_row(text: object, spec_id: str) -> tuple[StatusRow | None, str]:
    if not isinstance(text, str):
        return None, "status_text_unavailable"
    matches: list[list[str]] = []
    for line in text.splitlines():
        cells = _cells(line)
        if cells is not None and cells and cells[0] == spec_id:
            matches.append(cells)
    if len(matches) != 1:
        return None, "status_row_missing" if not matches else "status_row_ambiguous"
    row = matches[0]
    if len(row) != 6:
        return None, "status_row_malformed"
    status = row[1]
    if status not in _RECOGNIZED_STATUS:
        return None, "status_value_unknown"
    implementation_pr = _implementation_pr(row[2])
    if row[2] not in {"", "—", "-"} and implementation_pr is None:
        return None, "implementation_pr_malformed"
    return (
        StatusRow(
            spec_id=spec_id,
            status=status,
            implementation_pr=implementation_pr,
            name=row[3],
            depends_on=row[4],
        ),
        "status_row_exact",
    )


def _result_text(result: RepositoryTruthResult | None) -> object:
    return None if result is None else result.payload.get("text")


def _safe_call(call, warnings: list[str], label: str):
    try:
        return call()
    except RepositoryTruthError as exc:
        warnings.append(_warning(f"{label}:{exc.code}"))
        return None


def _check_runs(result: RepositoryTruthResult | None) -> list[dict[str, object]]:
    if result is None:
        return []
    runs = result.payload.get("check_runs")
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict)]


def _run_id(run: dict[str, object]) -> int:
    value = run.get("id")
    return value if isinstance(value, int) else -1


def _latest_exact_run(
    runs: list[dict[str, object]], name: str, head_sha: str
) -> tuple[dict[str, object] | None, bool]:
    exact = [
        run
        for run in runs
        if run.get("name") == name
        and run.get("head_sha") == head_sha
        and run.get("stale") is False
    ]
    stale_seen = any(run.get("name") == name and run.get("stale") is True for run in runs)
    if not exact:
        return None, stale_seen
    with_ids = [run for run in exact if isinstance(run.get("id"), int)]
    if len(exact) > 1 and len(with_ids) != len(exact):
        return None, stale_seen
    return (max(with_ids, key=_run_id) if with_ids else exact[0]), stale_seen


def _tests_stage(
    checks: RepositoryTruthResult | None,
    *,
    head_sha: str,
) -> dict[str, object]:
    if checks is None:
        return _stage("tests", "unknown", "check_evidence_unavailable")
    if checks.partial:
        return _stage("tests", "unknown", "check_collection_partial")
    runs = _check_runs(checks)
    selected: dict[str, dict[str, object]] = {}
    for name in REQUIRED_CHECKS:
        run, stale_seen = _latest_exact_run(runs, name, head_sha)
        if run is None:
            if stale_seen:
                return _stage("tests", "stale", "required_check_only_stale", {"check": name})
            return _stage("tests", "unknown", "required_check_missing", {"check": name})
        selected[name] = run

    evidence: dict[str, object] = {
        name: {
            "id": run.get("id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
        }
        for name, run in selected.items()
    }
    for name, run in selected.items():
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            return _stage("tests", "pending", "required_check_nonterminal", {"check": name, **evidence})
        if conclusion in _BLOCKING_CONCLUSIONS or conclusion not in {"success"}:
            return _stage("tests", "blocked", "required_check_unsuccessful", {"check": name, **evidence})
    return _stage("tests", "complete", "required_checks_green", evidence)


def _structured_markers(
    comments: RepositoryTruthResult,
    *,
    head_sha: str,
    base_sha: str,
) -> tuple[list[dict[str, object]], bool]:
    raw_comments = comments.payload.get("comments")
    if not isinstance(raw_comments, list):
        return [], False
    exact: list[dict[str, object]] = []
    stale_seen = False
    decoder = json.JSONDecoder()
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        search_from = 0
        while True:
            offset = body.find(CLAUDE_MARKER, search_from)
            if offset < 0:
                break
            search_from = offset + len(CLAUDE_MARKER)
            fragment = body[search_from:].lstrip()
            try:
                marker, _ = decoder.raw_decode(fragment)
            except json.JSONDecodeError:
                continue
            if not isinstance(marker, dict) or marker.get("schema") != "jarvis.claude-review.v3.2":
                continue
            marker_head = marker.get("head_sha")
            marker_base = marker.get("base_sha")
            if marker_head != head_sha or marker_base != base_sha:
                stale_seen = True
                continue
            verdict = marker.get("verdict")
            findings = marker.get("findings")
            if verdict not in {"APPROVE", "REQUEST_CHANGES"} or not isinstance(findings, list):
                continue
            bounded_findings: list[dict[str, object]] = []
            malformed = False
            for finding in findings[:100]:
                if not isinstance(finding, dict):
                    malformed = True
                    break
                severity = finding.get("severity")
                disposition = finding.get("disposition")
                if severity not in _REVIEW_SEVERITIES or disposition not in _REVIEW_DISPOSITIONS:
                    malformed = True
                    break
                bounded_findings.append({"severity": severity, "disposition": disposition})
            if malformed or len(findings) > 100:
                continue
            exact.append(
                {
                    "comment_id": comment.get("id"),
                    "verdict": verdict,
                    "findings": bounded_findings,
                }
            )
    return exact, stale_seen


def _review_stage(
    comments: RepositoryTruthResult | None,
    checks: RepositoryTruthResult | None,
    *,
    head_sha: str,
    base_sha: str,
) -> dict[str, object]:
    if comments is None:
        return _stage("independent_review", "unknown", "review_marker_evidence_unavailable")
    if comments.partial:
        return _stage("independent_review", "unknown", "review_comment_collection_partial")
    markers, stale_seen = _structured_markers(comments, head_sha=head_sha, base_sha=base_sha)
    if markers:
        for marker in markers:
            findings = marker["findings"]
            assert isinstance(findings, list)
            blocked = marker["verdict"] == "REQUEST_CHANGES" or any(
                isinstance(finding, dict) and finding.get("disposition") == "BLOCK"
                for finding in findings
            )
            if blocked:
                return _stage(
                    "independent_review",
                    "blocked",
                    "structured_review_blocks",
                    {"comment_id": marker.get("comment_id"), "verdict": marker.get("verdict")},
                )
        marker = markers[-1]
        return _stage(
            "independent_review",
            "complete",
            "structured_review_approved",
            {"comment_id": marker.get("comment_id"), "verdict": marker.get("verdict")},
        )
    if stale_seen:
        return _stage("independent_review", "stale", "structured_review_identity_stale")
    for run in _check_runs(checks):
        name = run.get("name")
        if (
            isinstance(name, str)
            and ("expert review" in name.lower() or "claude" in name.lower())
            and run.get("head_sha") == head_sha
            and run.get("stale") is False
            and run.get("status") != "completed"
        ):
            return _stage("independent_review", "pending", "review_workflow_nonterminal")
    return _stage("independent_review", "unknown", "structured_review_marker_missing")


def _merge_stage(pr: dict[str, object]) -> dict[str, object]:
    if pr.get("merged") is True:
        return _stage("merge", "complete", "provider_reports_merged")
    state = pr.get("state")
    if state == "open":
        return _stage("merge", "pending", "pull_request_open")
    if state == "closed":
        return _stage("merge", "blocked", "pull_request_closed_unmerged")
    return _stage("merge", "unknown", "pull_request_state_unknown")


class DevelopmentPipelineStateService:
    def __init__(self, repository_truth: RepositoryTruthService) -> None:
        self._truth = repository_truth

    def inspect(self, *, repository: str, pr_number: int, spec_id: str) -> dict[str, object]:
        if pr_number <= 0:
            raise PipelineStateInputError("pr_number_invalid")
        if not _SPEC_ID_RE.fullmatch(spec_id) or int(spec_id[:3]) <= 0:
            raise PipelineStateInputError("spec_id_invalid")

        warnings: list[str] = []
        initial = _safe_call(
            lambda: self._truth.pull_request_truth(repository, pr_number),
            warnings,
            "pr_truth",
        )
        if initial is None:
            stages = [_stage(name, "unknown", "pr_truth_unavailable") for name in _STAGE_NAMES]
            return {
                "repository": repository,
                "pr_number": pr_number,
                "spec_id": spec_id,
                "head_sha": None,
                "base_sha": None,
                "head_ref": None,
                "base_ref": None,
                "master_sha": None,
                "stages": stages,
                "partial": True,
                "warnings": warnings[:MAX_WARNINGS],
            }

        pr = initial.payload
        head_sha = pr.get("head_sha")
        base_sha = pr.get("base_sha")
        head_ref = pr.get("head_ref")
        base_ref = pr.get("base_ref")
        if not isinstance(head_sha, str) or not isinstance(base_sha, str):
            stages = [_stage(name, "unknown", "pr_identity_malformed") for name in _STAGE_NAMES]
            return {
                "repository": repository,
                "pr_number": pr_number,
                "spec_id": spec_id,
                "head_sha": None,
                "base_sha": None,
                "head_ref": head_ref,
                "base_ref": base_ref,
                "master_sha": None,
                "stages": stages,
                "partial": True,
                "warnings": [_warning("pr_identity_malformed")],
            }

        head_status = _safe_call(
            lambda: self._truth.file_preview(repository, head_sha, STATUS_PATH),
            warnings,
            "head_status",
        )
        checks = _safe_call(
            lambda: self._truth.check_truth(
                repository, pr_number, expected_head_sha=head_sha
            ),
            warnings,
            "checks",
        )
        comments = _safe_call(
            lambda: self._truth.pull_request_comments_truth(
                repository, pr_number, expected_head_sha=head_sha
            ),
            warnings,
            "review_comments",
        )
        master = _safe_call(
            lambda: self._truth.repository_ref_truth(repository, "master"),
            warnings,
            "master_ref",
        )
        master_sha = master.resolved_sha if master is not None else None
        master_status = None
        if isinstance(master_sha, str):
            master_status = _safe_call(
                lambda: self._truth.file_preview(repository, master_sha, STATUS_PATH),
                warnings,
                "master_status",
            )

        final_pr = _safe_call(
            lambda: self._truth.pull_request_truth(repository, pr_number),
            warnings,
            "pr_revalidation",
        )
        final_master = _safe_call(
            lambda: self._truth.repository_ref_truth(repository, "master"),
            warnings,
            "master_revalidation",
        )

        head_moved = final_pr is None or any(
            final_pr.payload.get(key) != pr.get(key) for key in ("head_sha", "head_ref")
        )
        base_moved = final_pr is None or any(
            final_pr.payload.get(key) != pr.get(key) for key in ("base_sha", "base_ref")
        )
        master_moved = (
            master is None
            or final_master is None
            or final_master.resolved_sha != master.resolved_sha
        )
        if head_moved:
            warnings.append(_warning("pr_head_moved"))
        if base_moved:
            warnings.append(_warning("pr_base_moved"))
        if master_moved:
            warnings.append(_warning("master_moved"))

        head_row, head_row_reason = _parse_status_row(_result_text(head_status), spec_id)
        master_row, master_row_reason = _parse_status_row(_result_text(master_status), spec_id)

        if master_moved:
            proposal = _stage("proposal", "stale", "master_identity_changed")
            plan = _stage("plan", "stale", "master_identity_changed")
        elif master_row is None:
            proposal = _stage("proposal", "unknown", master_row_reason)
            plan = _stage("plan", "unknown", master_row_reason)
        else:
            proposal = _stage(
                "proposal",
                "complete",
                "canonical_status_row_exists",
                {"status": master_row.status},
            )
            if master_row.status == "planned":
                plan = _stage("plan", "pending", "canonical_status_planned")
            elif master_row.status == "blocked":
                plan = _stage("plan", "blocked", "canonical_status_blocked")
            else:
                plan = _stage("plan", "complete", "canonical_planning_advanced")

        if head_moved:
            implementation = _stage("implementation", "stale", "pr_head_identity_changed")
        elif head_row is None:
            implementation = _stage("implementation", "unknown", head_row_reason)
        elif (
            head_row.implementation_pr == pr_number
            and head_row.status in {"in_review", "merged"}
        ):
            implementation = _stage(
                "implementation",
                "complete",
                "exact_pr_registry_association",
                {"status": head_row.status, "implementation_pr": pr_number},
            )
        else:
            implementation = _stage(
                "implementation",
                "unknown",
                "registry_association_not_proven",
                {"status": head_row.status, "implementation_pr": head_row.implementation_pr},
            )

        tests = (
            _stage("tests", "stale", "pr_head_identity_changed")
            if head_moved
            else _tests_stage(checks, head_sha=head_sha)
        )
        review = (
            _stage("independent_review", "stale", "pr_head_identity_changed")
            if head_moved
            else _stage("independent_review", "stale", "pr_base_identity_changed")
            if base_moved
            else _review_stage(comments, checks, head_sha=head_sha, base_sha=base_sha)
        )
        merge = _merge_stage(pr)

        if pr.get("merged") is not True:
            reconciliation = _stage(
                "reconciliation", "not_applicable", "implementation_not_merged"
            )
        elif master_moved:
            reconciliation = _stage("reconciliation", "stale", "master_identity_changed")
        elif master_row is None:
            reconciliation = _stage("reconciliation", "unknown", master_row_reason)
        elif master_row.implementation_pr != pr_number:
            reconciliation = _stage(
                "reconciliation", "unknown", "canonical_implementation_pr_mismatch"
            )
        elif master_row.status == "merged":
            reconciliation = _stage(
                "reconciliation",
                "complete",
                "canonical_status_merged",
                {"implementation_pr": pr_number},
            )
        elif master_row.status == "in_review":
            reconciliation = _stage(
                "reconciliation", "pending", "canonical_status_not_reconciled"
            )
        else:
            reconciliation = _stage(
                "reconciliation", "unknown", "canonical_reconciliation_state_unknown"
            )

        stages = [proposal, plan, implementation, tests, review, reconciliation, merge]
        partial = bool(warnings) or any(
            result is not None and result.partial
            for result in (head_status, checks, comments, master, master_status, final_pr, final_master)
        )
        return {
            "repository": repository,
            "pr_number": pr_number,
            "spec_id": spec_id,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "head_ref": head_ref,
            "base_ref": base_ref,
            "master_sha": master_sha,
            "stages": stages,
            "partial": partial,
            "warnings": warnings[:MAX_WARNINGS],
        }
