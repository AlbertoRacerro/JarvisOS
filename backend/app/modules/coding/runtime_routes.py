from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.config import get_settings
from app.modules.ai.jarvis_context import (
    PRODUCTION_CAPABILITY_REGISTRY,
    JarvisContextAdapterRegistry,
    JarvisContextError,
    build_jarvis_context_preview,
)
from app.modules.ai.jarvis_context_models import (
    JarvisCapabilityDescriptor,
    JarvisContextRequest,
    JarvisExactRef,
    JarvisResolvedRef,
    JarvisRouteDescriptor,
)
from app.modules.coding import provider_quota, relay_monitor
from app.modules.coding.actions import (
    CodingActionError,
    CodingActionsService,
    CodingInspectRequest,
    CodingSuggestModificationRequest,
)
from app.modules.coding.control_room import ControlRoomProjection, valid_target_ref
from app.modules.coding.pipeline_state import (
    DevelopmentPipelineStateService,
    PipelineStateInputError,
)
from app.modules.coding.repository_truth import RepositoryTruthError, RepositoryTruthService
from app.modules.coding.runtime_truth import (
    CANONICAL_RUNTIME_REPOSITORY,
    RuntimeSnapshot,
    RuntimeTruthService,
)

router = APIRouter(prefix="/api/coding", tags=["coding"])

for _capability in (
    JarvisCapabilityDescriptor(
        capability_id="coding.inspect",
        route_id="coding-repository",
        action_class="READ",
        label="Inspect exact Coding repository evidence",
    ),
    JarvisCapabilityDescriptor(
        capability_id="coding.inspect",
        route_id="coding-runtime",
        action_class="READ",
        label="Inspect exact Coding runtime evidence",
    ),
    JarvisCapabilityDescriptor(
        capability_id="coding.add-context",
        route_id="coding-repository",
        action_class="CONTEXT",
        label="Add exact Coding repository evidence to proposal context",
    ),
    JarvisCapabilityDescriptor(
        capability_id="coding.suggest-modification",
        route_id="coding-repository",
        action_class="PROPOSE",
        label="Suggest an exact-base repository modification",
    ),
):
    PRODUCTION_CAPABILITY_REGISTRY.register(_capability)


def _repository_service() -> RepositoryTruthService:
    return RepositoryTruthService(get_settings().coding_repositories)


class _CodingRepositoryFileContextAdapter:
    def __init__(
        self,
        service: RepositoryTruthService,
        repository: str,
        *,
        expected_sha: str,
        allowed_paths: frozenset[str],
    ) -> None:
        self._service = service
        self._repository = repository
        self._expected_sha = expected_sha
        self._allowed_paths = allowed_paths

    def resolve(self, ref: JarvisExactRef) -> JarvisResolvedRef:
        provenance: dict[str, object] = {
            "source": "118:repository-truth",
            "repository": self._repository,
            "path": ref.id,
            "base_sha": ref.version,
        }
        if (
            ref.owner != "coding"
            or ref.kind != "repository-file"
            or ref.version != self._expected_sha
            or ref.id not in self._allowed_paths
        ):
            return JarvisResolvedRef(
                ref=ref,
                state="unknown",
                reason="Coding context ref is outside the exact admitted target",
                provenance=provenance,
            )
        try:
            result = self._service.file_preview(self._repository, self._expected_sha, ref.id)
        except RepositoryTruthError as exc:
            return JarvisResolvedRef(
                ref=ref,
                state="unavailable",
                reason=exc.code,
                provenance=provenance,
            )
        provenance["resolved_sha"] = result.resolved_sha
        if result.partial:
            return JarvisResolvedRef(
                ref=ref,
                state="unavailable",
                reason="partial_repository_evidence",
                provenance=provenance,
            )
        if result.resolved_sha != self._expected_sha:
            return JarvisResolvedRef(
                ref=ref,
                state="stale",
                reason="repository evidence no longer matches exact context SHA",
                provenance=provenance,
            )
        return JarvisResolvedRef(
            ref=ref,
            state="current",
            content={
                "repository": self._repository,
                "base_sha": self._expected_sha,
                "path": ref.id,
                "payload": result.payload,
            },
            provenance=provenance,
            action_classes=["READ", "CONTEXT"],
        )


def _coding_context_registry(
    service: RepositoryTruthService,
    repository: str,
    *,
    expected_sha: str,
    allowed_paths: list[str],
) -> JarvisContextAdapterRegistry:
    registry = JarvisContextAdapterRegistry()
    registry.register(
        owner="coding",
        kind="repository-file",
        adapter=_CodingRepositoryFileContextAdapter(
            service,
            repository,
            expected_sha=expected_sha,
            allowed_paths=frozenset(allowed_paths),
        ),
        action_classes=("READ", "CONTEXT"),
    )
    return registry


def _repository_result(call: object) -> dict[str, object]:
    try:
        result = call()  # type: ignore[operator]
    except RepositoryTruthError as exc:
        status = 400
        if exc.code in {"authentication_required"}:
            status = 401
        elif exc.code in {"not_found"}:
            status = 404
        elif exc.code in {"rate_limited"}:
            status = 429
        elif exc.code in {"provider_unavailable", "timeout"}:
            status = 503
        raise HTTPException(
            status_code=status,
            detail={
                "code": exc.code,
                "partial": exc.partial,
                "metadata": exc.metadata,
            },
        ) from exc
    return asdict(result)


@router.get("/repository/ref")
def repository_ref(repository: str, ref: str) -> dict[str, object]:
    service = _repository_service()
    return _repository_result(lambda: service.repository_ref_truth(repository, ref))


@router.get("/repository/tree")
def repository_tree(repository: str, ref: str, path: str = "") -> dict[str, object]:
    service = _repository_service()
    return _repository_result(lambda: service.path_list(repository, ref, path))


@router.get("/repository/file")
def repository_file(repository: str, ref: str, path: str) -> dict[str, object]:
    service = _repository_service()
    return _repository_result(lambda: service.file_preview(repository, ref, path))


@router.get("/repository/search")
def repository_search(repository: str, ref: str, literal: str) -> dict[str, object]:
    service = _repository_service()
    return _repository_result(lambda: service.literal_search(repository, ref, literal))


@router.get("/repository/pull-request")
def repository_pull_request(repository: str, pr_number: int) -> dict[str, object]:
    service = _repository_service()
    return _repository_result(lambda: service.pull_request_truth(repository, pr_number))


@router.get("/repository/checks")
def repository_checks(
    repository: str,
    pr_number: int,
    expected_head_sha: str,
) -> dict[str, object]:
    service = _repository_service()
    return _repository_result(
        lambda: service.check_truth(
            repository,
            pr_number,
            expected_head_sha=expected_head_sha,
        )
    )


@router.get("/repository/reviews")
def repository_reviews(
    repository: str,
    pr_number: int,
    expected_head_sha: str,
) -> dict[str, object]:
    service = _repository_service()
    return _repository_result(
        lambda: service.review_truth(
            repository,
            pr_number,
            expected_head_sha=expected_head_sha,
        )
    )


@router.get("/repository/url")
def repository_url(
    repository: str,
    commit_sha: str | None = None,
    pr_number: int | None = None,
    path: str | None = None,
) -> dict[str, object]:
    service = _repository_service()
    return _repository_result(
        lambda: service.safe_github_url(
            repository,
            commit_sha=commit_sha,
            pr_number=pr_number,
            path=path,
        )
    )


@router.get("/runtime-truth")
def runtime_truth(
    request: Request,
    repository: str,
    target_ref: str,
) -> dict[str, object]:
    settings = get_settings()
    if repository != CANONICAL_RUNTIME_REPOSITORY or repository not in settings.coding_repositories:
        raise HTTPException(status_code=400, detail={"code": "repository_mismatch"})

    startup = getattr(request.app.state, "runtime_startup_snapshot", None)
    if not isinstance(startup, RuntimeSnapshot):
        raise HTTPException(status_code=503, detail={"code": "startup_snapshot_unavailable"})

    service = RuntimeTruthService(_repository_service())
    return service.inspect(repository=repository, target_ref=target_ref, startup=startup)


def _control_room_projection() -> ControlRoomProjection:
    return ControlRoomProjection()


@router.get("/control-room")
def control_room(
    request: Request,
    repository: str,
    target_ref: str,
) -> dict[str, object]:
    """Read-only maintainer projection: runtime truth, recent merges, upcoming registry rows."""
    settings = get_settings()
    if repository != CANONICAL_RUNTIME_REPOSITORY or repository not in settings.coding_repositories:
        raise HTTPException(status_code=400, detail={"code": "repository_mismatch"})
    if not valid_target_ref(target_ref):
        raise HTTPException(status_code=400, detail={"code": "target_ref_invalid"})

    projection = _control_room_projection().project(target_ref)
    warnings = projection["warnings"]
    assert isinstance(warnings, list)
    runtime: dict[str, object] | None = None
    startup = getattr(request.app.state, "runtime_startup_snapshot", None)
    if isinstance(startup, RuntimeSnapshot):
        runtime = RuntimeTruthService(_repository_service()).inspect(
            repository=repository,
            target_ref=target_ref,
            startup=startup,
        )
        remote = runtime.get("remote")
        remote_sha = remote.get("resolved_sha") if isinstance(remote, dict) else None
        source = projection["source"]
        local_sha = source.get("sha") if isinstance(source, dict) else None
        if isinstance(remote_sha, str) and isinstance(local_sha, str) and remote_sha != local_sha:
            # The projection reads the local remote-tracking ref; it never fetches.
            warnings.append("local_target_ref_differs_from_remote")
    else:
        warnings.append("startup_snapshot_unavailable")
    return {
        "repository": repository,
        "target_ref": target_ref,
        "runtime": runtime,
        **projection,
    }


def _require_runtime_repository(repository: str) -> None:
    settings = get_settings()
    if repository != CANONICAL_RUNTIME_REPOSITORY or repository not in settings.coding_repositories:
        raise HTTPException(status_code=400, detail={"code": "repository_mismatch"})


@router.get("/relay-monitor")
def relay_monitor_snapshot(repository: str) -> dict[str, object]:
    """Read-only spec 205 owner snapshot: mission, task, state and reason. Never locks or writes."""
    _require_runtime_repository(repository)
    return relay_monitor.snapshot()


@router.get("/provider-quotas")
def provider_quotas(repository: str, refresh: bool = False) -> dict[str, object]:
    """Read-only account usage; explicit refresh bypasses the TTL without starting a model turn."""
    _require_runtime_repository(repository)
    return provider_quota.quotas(refresh=refresh)


@router.get("/relay-monitor/timeline")
def relay_monitor_timeline(
    repository: str,
    cursor: str | None = Query(default=None, max_length=512),
    start: Literal["turn", "mission"] = "turn",
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    """The next bounded, scrubbed page of the owner's live timeline after an opaque cursor."""
    _require_runtime_repository(repository)
    try:
        return relay_monitor.timeline(cursor, start=start, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "cursor_invalid"}) from exc


@router.get("/pipeline-state")
def pipeline_state(
    repository: str,
    pr_number: int,
    spec_id: str,
) -> dict[str, object]:
    settings = get_settings()
    if repository not in settings.coding_repositories:
        raise HTTPException(status_code=400, detail={"code": "unauthorized_repository"})
    service = DevelopmentPipelineStateService(_repository_service())
    try:
        return service.inspect(
            repository=repository,
            pr_number=pr_number,
            spec_id=spec_id,
        )
    except PipelineStateInputError as exc:
        raise HTTPException(status_code=400, detail={"code": str(exc)}) from exc


@router.post("/actions/inspect")
def coding_inspect(payload: CodingInspectRequest) -> dict[str, object]:
    settings = get_settings()
    if payload.repository not in settings.coding_repositories:
        return {"state": "refused", "reason": "missing_evidence"}
    service = CodingActionsService(_repository_service())
    try:
        return service.inspect(payload)
    except CodingActionError as exc:
        return {"state": "refused", "reason": exc.reason}


@router.post("/actions/context-preview")
def coding_context_preview(payload: CodingInspectRequest) -> dict[str, object]:
    settings = get_settings()
    if payload.repository not in settings.coding_repositories:
        return {"state": "refused", "reason": "missing_evidence"}
    truth = _repository_service()
    actions = CodingActionsService(truth)
    try:
        inspected = actions.inspect(payload)
    except CodingActionError as exc:
        return {"state": "refused", "reason": exc.reason}
    refs = [
        JarvisExactRef(
            workspace_id=payload.workspace_id,
            owner="coding",
            kind="repository-file",
            id=path,
            version=payload.base_sha,
        )
        for path in payload.target_paths
    ]
    try:
        preview = build_jarvis_context_preview(
            JarvisContextRequest(
                workspace_id=payload.workspace_id,
                route=JarvisRouteDescriptor(
                    route_id="coding-repository",
                    canonical_path="/coding/repository",
                ),
                added_context_refs=refs,
            ),
            registry=_coding_context_registry(
                truth,
                payload.repository,
                expected_sha=payload.base_sha,
                allowed_paths=payload.target_paths,
            ),
        )
    except JarvisContextError:
        return {"state": "refused", "reason": "missing_evidence"}
    requested_refs = [ref.model_dump(exclude_none=True, mode="json") for ref in refs]
    included_refs = [source.get("ref") for source in preview.context_sources_manifest]
    if not preview.dispatchable or included_refs != requested_refs:
        return {"state": "refused", "reason": "missing_evidence"}
    return {
        "state": "current",
        "repository": inspected["repository"],
        "base_sha": inspected["base_sha"],
        "target_paths": inspected["target_paths"],
        "added_context_refs": requested_refs,
        "context_digest": preview.context_digest,
        "context_sources_manifest": preview.context_sources_manifest,
    }


@router.post("/actions/suggest-modification")
def coding_suggest_modification(payload: CodingSuggestModificationRequest) -> dict[str, object]:
    settings = get_settings()
    if payload.repository not in settings.coding_repositories:
        return {"state": "refused", "reason": "missing_evidence"}
    truth = _repository_service()
    service = CodingActionsService(
        truth,
        context_registry=_coding_context_registry(
            truth,
            payload.repository,
            expected_sha=payload.base_sha,
            allowed_paths=payload.target_paths,
        ),
    )
    return service.suggest_modification(payload)
