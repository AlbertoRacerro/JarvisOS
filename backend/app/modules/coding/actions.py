from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.ai.execution import AiTaskOutcome, run_ai_task
from app.modules.ai.jarvis_context import (
    PRODUCTION_ADAPTER_REGISTRY,
    JarvisContextAdapterRegistry,
    JarvisContextConflictError,
    JarvisContextError,
    require_dispatchable_preview,
)
from app.modules.ai.jarvis_context_models import JarvisContextRequest, JarvisExactRef, JarvisRouteDescriptor
from app.modules.coding.repository_truth import RepositoryTruthError, RepositoryTruthService

MAX_PROPOSAL_BYTES = 128 * 1024
MAX_CHANGE_BYTES = 32 * 1024
_HEX_SHA = re.compile(r"^[0-9a-f]{40}$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_DIFF_HEADER = re.compile(r"^(---|\+\+\+) (?:[ab]/)?(.+)$")
_FORBIDDEN_DIFF_MARKERS = (
    "GIT binary patch",
    "Binary files ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "Subproject commit ",
)
_PROTECTED_EXACT = {"AGENTS.md", "CLAUDE.md", "docs/PROCESS.md", "CODEOWNERS"}
_PROTECTED_PREFIXES = (
    ".git/",
    ".github/",
    "data/",
    "secrets/",
    "backend/app/modules/secrets/",
    ".env",
)
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".7z", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".bin",
}

CodingRefusalReason = Literal[
    "missing_evidence",
    "stale_target",
    "identity_conflict",
    "unsupported_target",
    "policy_denied",
    "provider_unavailable",
    "proposal_invalid",
    "proposal_too_large",
    "unknown",
]


class CodingActionError(ValueError):
    def __init__(self, reason: CodingRefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class CodingInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=128)
    repository: str = Field(min_length=1, max_length=256)
    base_ref: str = Field(min_length=1, max_length=512)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_paths: list[str] = Field(min_length=1, max_length=16)


class CodingSuggestModificationRequest(CodingInspectRequest):
    intent: str = Field(min_length=1, max_length=4000)
    added_context_refs: list[JarvisExactRef] = Field(default_factory=list, max_length=50)
    expected_context_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    expected_checks: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def context_digest_pairing(self) -> CodingSuggestModificationRequest:
        if bool(self.added_context_refs) != bool(self.expected_context_digest):
            raise ValueError("added_context_refs and expected_context_digest must be supplied together")
        if any(not check or len(check) > 128 for check in self.expected_checks):
            raise ValueError("expected_checks entries must be 1-128 characters")
        return self


class CodingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=256)
    diff: str | None = None
    plan: str | None = None

    @model_validator(mode="after")
    def exactly_one_representation(self) -> CodingChange:
        if (self.diff is None) == (self.plan is None):
            raise ValueError("each change requires exactly one of diff or plan")
        return self


class CodingGeneratedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=2000)
    changes: list[CodingChange] = Field(min_length=1, max_length=16)
    assumptions: list[str] = Field(default_factory=list, max_length=16)
    warnings: list[str] = Field(default_factory=list, max_length=16)
    expected_checks: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def bounded_strings(self) -> CodingGeneratedProposal:
        if any(len(item) > 256 for item in [*self.assumptions, *self.warnings]):
            raise ValueError("assumptions and warnings entries must be at most 256 characters")
        if any(not item or len(item) > 128 for item in self.expected_checks):
            raise ValueError("expected_checks entries must be 1-128 characters")
        return self


@dataclass(frozen=True)
class _FrozenTarget:
    repository: str
    base_ref: str
    base_sha: str
    target_paths: tuple[str, ...]


def _admit_paths(paths: list[str]) -> tuple[str, ...]:
    admitted: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw or len(raw) > 256 or "\x00" in raw or "\\" in raw:
            raise CodingActionError("unsupported_target", "target path is not a bounded POSIX repository path")
        if raw.startswith("/") or _DRIVE_PREFIX.match(raw):
            raise CodingActionError("unsupported_target", "absolute or drive-prefixed target path is forbidden")
        path = PurePosixPath(raw)
        normalized = path.as_posix()
        if normalized != raw or any(part in {"", ".", ".."} for part in path.parts):
            raise CodingActionError("unsupported_target", "ambiguous or traversing target path is forbidden")
        lowered = normalized.lower()
        if normalized in _PROTECTED_EXACT or any(lowered.startswith(prefix.lower()) for prefix in _PROTECTED_PREFIXES):
            raise CodingActionError("unsupported_target", "protected governance, data, or credential target is forbidden")
        if path.suffix.lower() in _BINARY_SUFFIXES:
            raise CodingActionError("unsupported_target", "binary target is unsupported")
        if normalized in seen:
            raise CodingActionError("unsupported_target", "duplicate target path is forbidden")
        seen.add(normalized)
        admitted.append(normalized)
    if not 1 <= len(admitted) <= 16:
        raise CodingActionError("unsupported_target", "target count must be 1-16")
    return tuple(admitted)


def _freeze_target(service: RepositoryTruthService, request: CodingInspectRequest) -> _FrozenTarget:
    if not _HEX_SHA.fullmatch(request.base_sha):
        raise CodingActionError("identity_conflict", "base SHA must be exact")
    paths = _admit_paths(request.target_paths)
    try:
        truth = service.repository_ref_truth(request.repository, request.base_ref)
    except RepositoryTruthError as exc:
        raise CodingActionError("missing_evidence", "repository/ref truth is unavailable") from exc
    if truth.partial or truth.resolved_sha is None:
        raise CodingActionError("missing_evidence", "repository/ref truth is partial or unknown")
    if truth.resolved_sha != request.base_sha:
        raise CodingActionError("stale_target", "base ref no longer resolves to requested SHA")
    return _FrozenTarget(request.repository, request.base_ref, request.base_sha, paths)


def _safe_file_evidence(service: RepositoryTruthService, frozen: _FrozenTarget) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for path in frozen.target_paths:
        try:
            result = service.file_preview(frozen.repository, frozen.base_sha, path)
        except RepositoryTruthError as exc:
            raise CodingActionError("missing_evidence", f"bounded file evidence unavailable for {path}") from exc
        if result.partial:
            raise CodingActionError("missing_evidence", f"bounded file evidence partial for {path}")
        evidence.append({"path": path, "sha": result.resolved_sha, "payload": result.payload})
    return evidence


def _validate_diff(diff: str, expected_path: str, admitted: set[str]) -> None:
    if len(diff.encode("utf-8")) > MAX_CHANGE_BYTES:
        raise CodingActionError("proposal_too_large", "change exceeds per-change limit")
    if any(marker in diff for marker in _FORBIDDEN_DIFF_MARKERS):
        raise CodingActionError("proposal_invalid", "unsupported diff artifact type")
    headers: list[str] = []
    expected_git_header = f"diff --git a/{expected_path} b/{expected_path}"
    for line in diff.splitlines():
        if line.startswith("diff --git ") and line != expected_git_header:
            raise CodingActionError("proposal_invalid", "diff header escapes admitted target set")
        match = _DIFF_HEADER.match(line)
        if not match:
            continue
        path = match.group(2)
        if path == "/dev/null":
            raise CodingActionError("proposal_invalid", "file creation or deletion is unsupported")
        headers.append(path)
        if path not in admitted or path != expected_path:
            raise CodingActionError("proposal_invalid", "diff header escapes admitted target set")
    if not headers:
        raise CodingActionError("proposal_invalid", "diff change requires exact target headers")


def _validate_generated(raw_text: str, frozen: _FrozenTarget) -> CodingGeneratedProposal:
    try:
        proposal = CodingGeneratedProposal.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise CodingActionError("proposal_invalid", "model proposal did not match the closed schema") from exc
    admitted = set(frozen.target_paths)
    seen: set[str] = set()
    for change in proposal.changes:
        try:
            normalized = _admit_paths([change.path])[0]
        except CodingActionError as exc:
            raise CodingActionError("proposal_invalid", "model proposal contains unsupported path") from exc
        if normalized not in admitted or normalized in seen:
            raise CodingActionError("proposal_invalid", "model proposal path is undeclared or duplicated")
        seen.add(normalized)
        if change.diff is not None:
            _validate_diff(change.diff, normalized, admitted)
        elif change.plan is not None and len(change.plan.encode("utf-8")) > MAX_CHANGE_BYTES:
            raise CodingActionError("proposal_too_large", "change exceeds per-change limit")
    return proposal


def _refusal(reason: CodingRefusalReason) -> dict[str, object]:
    return {"state": "refused", "reason": reason}


class CodingActionsService:
    def __init__(
        self,
        repository_truth: RepositoryTruthService,
        *,
        context_registry: JarvisContextAdapterRegistry = PRODUCTION_ADAPTER_REGISTRY,
        ai_runner: Callable[..., AiTaskOutcome] = run_ai_task,
    ) -> None:
        self._truth = repository_truth
        self._context_registry = context_registry
        self._ai_runner = ai_runner

    def inspect(self, request: CodingInspectRequest) -> dict[str, object]:
        frozen = _freeze_target(self._truth, request)
        evidence = _safe_file_evidence(self._truth, frozen)
        return {
            "state": "current",
            "workspace_id": request.workspace_id,
            "repository": frozen.repository,
            "base_ref": frozen.base_ref,
            "base_sha": frozen.base_sha,
            "target_paths": list(frozen.target_paths),
            "evidence": evidence,
            "generated_by": "deterministic",
        }

    def suggest_modification(self, request: CodingSuggestModificationRequest) -> dict[str, object]:
        try:
            frozen = _freeze_target(self._truth, request)
            file_evidence = _safe_file_evidence(self._truth, frozen)
            context_blocks: list[dict[str, object]] = []
            context_digest: str | None = None
            context_sources: list[dict[str, object]] = []
            if request.added_context_refs:
                context_request = JarvisContextRequest(
                    workspace_id=request.workspace_id,
                    route=JarvisRouteDescriptor(route_id="coding-repository", canonical_path="/coding/repository"),
                    added_context_refs=request.added_context_refs,
                )
                try:
                    preview = require_dispatchable_preview(
                        context_request,
                        request.expected_context_digest or "",
                        registry=self._context_registry,
                    )
                except JarvisContextConflictError as exc:
                    raise CodingActionError("identity_conflict", "exact context identity is stale or conflicting") from exc
                except JarvisContextError as exc:
                    raise CodingActionError("missing_evidence", "exact context evidence is unavailable") from exc
                context_blocks = list(preview.blocks)
                context_digest = preview.context_digest
                context_sources = list(preview.context_sources_manifest)

            evidence_block = {
                "source": "coding:repository-truth",
                "type": "exact_repository_files",
                "id": frozen.base_sha,
                "content": json.dumps(
                    {
                        "repository": frozen.repository,
                        "base_ref": frozen.base_ref,
                        "base_sha": frozen.base_sha,
                        "target_paths": list(frozen.target_paths),
                        "files": file_evidence,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            }
            prompt = (
                "Return JSON only with keys summary, changes, assumptions, warnings, expected_checks. "
                "Each changes item must contain path and exactly one of diff or plan. Never name a path "
                "outside the admitted target paths. Intent: " + request.intent
            )
            try:
                outcome = self._ai_runner(
                    user_prompt=prompt,
                    task_kind="synthesis",
                    route_class="local:coder",
                    context_blocks=[evidence_block, *context_blocks],
                    workspace_id=request.workspace_id,
                )
            except Exception as exc:
                raise CodingActionError("provider_unavailable", "proposal generation was unavailable") from exc
            if outcome.status != "success" or outcome.response is None or outcome.response.text is None:
                raise CodingActionError("provider_unavailable", "proposal generation was unavailable")
            generated = _validate_generated(outcome.response.text, frozen)

            try:
                refreshed = self._truth.repository_ref_truth(frozen.repository, frozen.base_ref)
            except RepositoryTruthError as exc:
                raise CodingActionError("missing_evidence", "repository/ref truth became unavailable") from exc
            if refreshed.partial or refreshed.resolved_sha != frozen.base_sha:
                raise CodingActionError("stale_target", "base ref moved while proposal was generated")

            response: dict[str, object] = {
                "state": "proposed",
                "workspace_id": request.workspace_id,
                "repository": frozen.repository,
                "base_ref": frozen.base_ref,
                "base_sha": frozen.base_sha,
                "intent": request.intent,
                "target_paths": list(frozen.target_paths),
                "summary": generated.summary,
                "changes": [change.model_dump(exclude_none=True) for change in generated.changes],
                "assumptions": generated.assumptions,
                "warnings": generated.warnings,
                "expected_checks": request.expected_checks or generated.expected_checks,
                "provenance": {
                    "context_digest": context_digest,
                    "context_sources": context_sources,
                    "repository_truth_sha": frozen.base_sha,
                },
                "generated_by": {
                    "kind": "ai_task",
                    "ai_job_id": outcome.ledger_id,
                    "selected_route_class": outcome.selected_route_class,
                    "provider_id": outcome.decision.provider_id,
                    "model_id": outcome.decision.model_id,
                },
            }
            if len(json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_PROPOSAL_BYTES:
                raise CodingActionError("proposal_too_large", "proposal exceeds total payload limit")
            return response
        except CodingActionError as exc:
            return _refusal(exc.reason)
