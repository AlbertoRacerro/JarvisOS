from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.database import open_sqlite_connection
from app.modules.ai import sensitivity
from app.modules.ai.jarvis_context import (
    PRODUCTION_ADAPTER_REGISTRY,
    PRODUCTION_CAPABILITY_REGISTRY,
    JarvisContextConflictError,
    JarvisContextError,
    build_jarvis_context_preview,
    require_dispatchable_preview,
)
from app.modules.ai.jarvis_context_models import (
    JarvisCapabilityDescriptor,
    JarvisContextRequest,
    JarvisExactRef,
    JarvisResolvedRef,
    JarvisRouteDescriptor,
)
from app.modules.ai.models import AITaskRunRequest, AITaskRunResponse
from app.modules.ai.routing.bridge import run_auto_task
from app.modules.memory.literature_service import LiteratureError, get_literature_source
from app.modules.modeling.model_dossier import get_model_dossier
from app.modules.modeling.project_search_owner import get_context_record_exact

KnowledgeRouteId = Literal["memory-project-basis", "memory-models", "memory-literature"]
KnowledgeOwner = Literal["modeling", "model-dossier", "literature"]
KnowledgeTargetDomain = Literal["project_basis", "models", "literature"]
KnowledgeRefusalReason = Literal[
    "missing_evidence",
    "identity_conflict",
    "stale_context",
    "unsupported_ref",
    "provider_unavailable",
    "proposal_invalid",
    "proposal_too_large",
    "sensitive_context",
]

_ROUTE_PATHS: dict[KnowledgeRouteId, str] = {
    "memory-project-basis": "/memory/project-basis",
    "memory-models": "/memory/models",
    "memory-literature": "/memory/literature",
}
_ROUTE_OWNER: dict[KnowledgeRouteId, KnowledgeOwner] = {
    "memory-project-basis": "modeling",
    "memory-models": "model-dossier",
    "memory-literature": "literature",
}
_ROUTE_DOMAIN: dict[KnowledgeRouteId, KnowledgeTargetDomain] = {
    "memory-project-basis": "project_basis",
    "memory-models": "models",
    "memory-literature": "literature",
}
_ROUTE_NEXT_ACTION: dict[KnowledgeRouteId, str] = {
    "memory-project-basis": "Review the proposal, then use the Project Basis owner workflow if an authoritative change is accepted.",
    "memory-models": "Review the proposal against the read-only Model Dossier; any authoritative model change belongs to its owning workflow.",
    "memory-literature": "Review the proposal, then use the Literature owner workflow for any accepted extraction or promotion.",
}
_PROJECT_KINDS = frozenset({"requirement", "parameter", "assumption", "decision"})
MAX_INTENT_CHARS = 4_000
MAX_PROPOSAL_BYTES = 128 * 1024

_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?:api[_ -]?key|password|passwd|secret|token|access[_ -]?token|refresh[_ -]?token|"
    r"aws[_ -]?secret[_ -]?access[_ -]?key|authorization)\b"
    r"\s*[\"']?\s*(?::|=)\s*[\"']?[^\s,\"'}]{8,}"
)
_SECRET_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"xox[a-z]-[A-Za-z0-9-]{8,}", re.I),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}", re.I),
    re.compile(r"\bsk_live_[A-Za-z0-9_-]{8,}\b", re.I),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{8,}\b", re.I),
    re.compile(r"\bnpm_[A-Za-z0-9_-]{8,}\b", re.I),
    re.compile(r"\bAIza[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:[+]srv)?|redis)://[^ :/@]+:[^ /@]{8,}@", re.I),
    re.compile(r"https?://[^ :/@]+:[^ /@]+@", re.I),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bauthorization\s*:\s*basic\s+[A-Za-z0-9+/]{4,}={0,2}"),
)


class KnowledgeActionError(ValueError):
    def __init__(self, reason: KnowledgeRefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class StableKnowledgeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: KnowledgeOwner
    stable_ref: str = Field(min_length=1, max_length=512)


class KnowledgeContextPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=128)
    route_id: KnowledgeRouteId
    refs: list[StableKnowledgeRef] = Field(min_length=1, max_length=50)


class KnowledgeProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=128)
    route_id: KnowledgeRouteId
    intent: str = Field(min_length=1, max_length=MAX_INTENT_CHARS)
    exact_refs: list[JarvisExactRef] = Field(min_length=1, max_length=50)
    expected_context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    semantic: bool = False


class KnowledgeGeneratedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4_000)
    proposed_items: list[str] = Field(default_factory=list, max_length=16)
    questions: list[str] = Field(default_factory=list, max_length=16)
    research_steps: list[str] = Field(default_factory=list, max_length=16)
    assumptions: list[str] = Field(default_factory=list, max_length=16)
    warnings: list[str] = Field(default_factory=list, max_length=16)
    authoritative_next_action: str | None = Field(default=None, max_length=1_000)

    @field_validator("proposed_items", "questions", "research_steps", "assumptions", "warnings")
    @classmethod
    def bounded_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("proposal list items must be non-empty and bounded")
        return values


class _ProjectBasisAdapter:
    def resolve(self, ref: JarvisExactRef) -> JarvisResolvedRef:
        provenance: dict[str, object] = {
            "source": "112:project-knowledge-core",
            "owner": "modeling",
            "kind": ref.kind,
            "record_id": ref.id,
        }
        if ref.owner != "modeling" or ref.kind not in _PROJECT_KINDS:
            return JarvisResolvedRef(ref=ref, state="unknown", reason="unsupported Project Basis exact ref", provenance=provenance)
        record = get_context_record_exact(ref.workspace_id, ref.kind, ref.id)
        if record is None:
            return JarvisResolvedRef(ref=ref, state="unavailable", reason="Project Basis record is unavailable", provenance=provenance)
        current_revision = str(record.updated_at)
        provenance["revision"] = current_revision
        if ref.revision != current_revision:
            return JarvisResolvedRef(ref=ref, state="stale", reason="Project Basis record revision moved", provenance=provenance)
        if ref.kind == "requirement" and getattr(record, "status", None) == "retired":
            return JarvisResolvedRef(ref=ref, state="stale", reason="Project Basis requirement is retired", provenance=provenance)
        if ref.kind == "assumption" and getattr(record, "status", None) == "superseded":
            return JarvisResolvedRef(ref=ref, state="stale", reason="Project Basis assumption is superseded", provenance=provenance)
        if ref.kind == "parameter" and getattr(record, "lifecycle_state", None) != "active":
            return JarvisResolvedRef(ref=ref, state="stale", reason="Project Basis parameter is no longer active", provenance=provenance)
        if ref.kind == "decision" and getattr(record, "basis_lifecycle_state", None) != "active":
            return JarvisResolvedRef(ref=ref, state="stale", reason="Project Basis decision is no longer active", provenance=provenance)
        return JarvisResolvedRef(
            ref=ref,
            state="current",
            content=record.model_dump(mode="json"),
            provenance=provenance,
            action_classes=["READ", "CONTEXT"],
        )


def _model_dossier_content(dossier: Any) -> dict[str, object]:
    identity = dossier.identity
    return {
        "identity": identity.model_dump(mode="json"),
        "title": dossier.title,
        "engineering_question": dossier.engineering_question,
        "scope": dossier.scope,
        "maturity_status": dossier.maturity_status,
        "assumptions_summary": dossier.assumptions_summary,
        "inputs_summary": dossier.inputs_summary,
        "outputs_summary": dossier.outputs_summary,
    }


def _model_dossier_content_digest(dossier: object) -> str:
    encoded = json.dumps(
        _model_dossier_content(dossier), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class _ModelDossierAdapter:
    def resolve(self, ref: JarvisExactRef) -> JarvisResolvedRef:
        provenance: dict[str, object] = {
            "source": "113:model-dossier",
            "owner": "model-dossier",
            "model_version_id": ref.id,
        }
        if ref.owner != "model-dossier" or ref.kind != "model-version":
            return JarvisResolvedRef(ref=ref, state="unknown", reason="unsupported Model Dossier exact ref", provenance=provenance)
        dossier = get_model_dossier(ref.workspace_id, ref.id)
        if dossier is None:
            return JarvisResolvedRef(ref=ref, state="unavailable", reason="model version is unavailable", provenance=provenance)
        identity = dossier.identity
        immutable_ref = f"model_version:{identity.model_version_id}"
        provenance.update(
            {
                "immutable_ref": immutable_ref,
                "model_spec_id": identity.model_spec_id,
                "version_label": identity.version_label,
                "input_contract_digest": identity.input_contract_digest,
            }
        )
        if ref.immutable_ref != immutable_ref:
            return JarvisResolvedRef(ref=ref, state="stale", reason="model version identity moved", provenance=provenance)
        if ref.version != identity.version_label:
            return JarvisResolvedRef(ref=ref, state="stale", reason="model version label moved", provenance=provenance)
        content = _model_dossier_content(dossier)
        content_digest = _model_dossier_content_digest(dossier)
        provenance["content_digest"] = content_digest
        if ref.content_digest != content_digest:
            return JarvisResolvedRef(ref=ref, state="stale", reason="model dossier content moved", provenance=provenance)
        return JarvisResolvedRef(
            ref=ref,
            state="current",
            content=content,
            provenance=provenance,
            action_classes=["READ", "CONTEXT"],
        )


def _literature_source_for_entry(workspace_id: str, entry_id: str) -> str | None:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT source_id FROM literature_entries WHERE id = ? AND workspace_id = ?",
            (entry_id, workspace_id),
        ).fetchone()
        return None if row is None else str(row["source_id"])


class _LiteratureAdapter:
    def resolve(self, ref: JarvisExactRef) -> JarvisResolvedRef:
        provenance: dict[str, object] = {
            "source": "114:literature-knowledge",
            "owner": "literature",
            "kind": ref.kind,
            "record_id": ref.id,
        }
        if ref.owner != "literature" or ref.kind not in {"source", "entry"}:
            return JarvisResolvedRef(ref=ref, state="unknown", reason="unsupported Literature exact ref", provenance=provenance)
        source_id = ref.id if ref.kind == "source" else _literature_source_for_entry(ref.workspace_id, ref.id)
        if source_id is None:
            return JarvisResolvedRef(ref=ref, state="unavailable", reason="Literature record is unavailable", provenance=provenance)
        try:
            source = get_literature_source(ref.workspace_id, source_id)
        except LiteratureError:
            return JarvisResolvedRef(ref=ref, state="unavailable", reason="Literature source is unavailable", provenance=provenance)
        backing = getattr(source, "backing", None)
        if backing is not None and getattr(backing, "availability", None) != "available":
            provenance["backing_availability"] = getattr(backing, "availability", None)
            return JarvisResolvedRef(ref=ref, state="unavailable", reason="Literature source backing is unavailable", provenance=provenance)
        if ref.kind == "source":
            stable_ref = source.source_ref
            revision = source.updated_at
            content: dict[str, object] = {
                "id": source.id,
                "title": source.title,
                "source_kind": source.source_kind,
                "state": source.state,
                "citation": source.citation,
                "publisher": source.publisher,
                "published_year": source.published_year,
                "source_ref": source.source_ref,
            }
        else:
            entry = next((item for item in source.entries if item.id == ref.id), None)
            if entry is None:
                return JarvisResolvedRef(ref=ref, state="unavailable", reason="Literature entry is unavailable", provenance=provenance)
            stable_ref = entry.provenance_ref
            revision = entry.updated_at
            content = {
                "id": entry.id,
                "source_id": entry.source_id,
                "entry_kind": entry.entry_kind,
                "statement": entry.statement,
                "value_text": entry.value_text,
                "value_number": entry.value_number,
                "unit": entry.unit,
                "status": entry.status,
                "locator_kind": entry.locator_kind,
                "locator_start": entry.locator_start,
                "locator_end": entry.locator_end,
                "context_text": entry.context_text,
                "provenance_ref": entry.provenance_ref,
                "used_by": [item.model_dump(mode="json") for item in entry.used_by[:20]],
            }
        provenance.update({"immutable_ref": stable_ref, "revision": revision, "source_id": source_id})
        if ref.immutable_ref != stable_ref or ref.revision != revision:
            return JarvisResolvedRef(ref=ref, state="stale", reason="Literature provenance identity moved", provenance=provenance)
        return JarvisResolvedRef(
            ref=ref,
            state="current",
            content=content,
            provenance=provenance,
            action_classes=["READ", "CONTEXT"],
        )


def _register_production_contract() -> None:
    project_adapter = _ProjectBasisAdapter()
    for kind in sorted(_PROJECT_KINDS):
        PRODUCTION_ADAPTER_REGISTRY.register(owner="modeling", kind=kind, adapter=project_adapter)
    PRODUCTION_ADAPTER_REGISTRY.register(owner="model-dossier", kind="model-version", adapter=_ModelDossierAdapter())
    literature_adapter = _LiteratureAdapter()
    PRODUCTION_ADAPTER_REGISTRY.register(owner="literature", kind="source", adapter=literature_adapter)
    PRODUCTION_ADAPTER_REGISTRY.register(owner="literature", kind="entry", adapter=literature_adapter)
    for route_id in _ROUTE_PATHS:
        PRODUCTION_CAPABILITY_REGISTRY.register(
            JarvisCapabilityDescriptor(
                capability_id="knowledge.add-context",
                route_id=route_id,
                action_class="CONTEXT",
                label="Add exact current knowledge evidence to Jarvis context",
            )
        )
        PRODUCTION_CAPABILITY_REGISTRY.register(
            JarvisCapabilityDescriptor(
                capability_id="knowledge.propose",
                route_id=route_id,
                action_class="PROPOSE",
                label="Generate a bounded advisory knowledge proposal",
            )
        )


_register_production_contract()


def _route(route_id: KnowledgeRouteId) -> JarvisRouteDescriptor:
    return JarvisRouteDescriptor(route_id=route_id, canonical_path=_ROUTE_PATHS[route_id])


def _owner_allowed(route_id: KnowledgeRouteId, owner: KnowledgeOwner) -> bool:
    return _ROUTE_OWNER[route_id] == owner


def _dedupe_exact_refs(refs: list[JarvisExactRef]) -> list[JarvisExactRef]:
    normalized: list[JarvisExactRef] = []
    seen: set[str] = set()
    for ref in refs:
        key = ref.model_dump_json(exclude_none=True)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(ref)
    return normalized


def _secret_candidate_texts(value: object) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            texts.append(str(key))
            texts.extend(_secret_candidate_texts(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            texts.extend(_secret_candidate_texts(item))
    return texts


def _contains_secret_material(blocks: list[dict[str, object]]) -> bool:
    serialized = json.dumps(blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    candidates = [serialized, *_secret_candidate_texts(blocks)]
    return any(
        sensitivity.deterministic_floor(candidate) == "S4"
        or _SECRET_ASSIGNMENT_PATTERN.search(candidate)
        or any(pattern.search(candidate) for pattern in _SECRET_TOKEN_PATTERNS)
        for candidate in candidates
    )


def _contains_current_s4_project_basis_label(workspace_id: str, refs: list[JarvisExactRef]) -> bool:
    for ref in refs:
        if ref.owner != "modeling" or ref.kind not in _PROJECT_KINDS:
            continue
        try:
            label = sensitivity.get_current_sensitivity_label(
                workspace_id,
                f"{ref.kind}:{ref.id}",
            )
        except (sensitivity.SensitivityNotFoundError, sensitivity.SensitivityPolicyError):
            return True
        if label is not None and (not label.current or label.level == "S4"):
            return True
    return False


def exact_ref_from_stable(workspace_id: str, route_id: KnowledgeRouteId, item: StableKnowledgeRef) -> JarvisExactRef:
    if not _owner_allowed(route_id, item.owner):
        raise KnowledgeActionError("unsupported_ref", "stable ref owner does not belong to the active knowledge route")
    prefix, separator, record_id = item.stable_ref.partition(":")
    if not separator or not record_id:
        raise KnowledgeActionError("unsupported_ref", "stable ref is malformed")
    if item.owner == "modeling":
        if prefix not in _PROJECT_KINDS:
            raise KnowledgeActionError("unsupported_ref", "Project Basis stable ref kind is unsupported")
        record = get_context_record_exact(workspace_id, prefix, record_id)
        if record is None:
            raise KnowledgeActionError("missing_evidence", "Project Basis record is unavailable")
        return JarvisExactRef(
            workspace_id=workspace_id,
            owner="modeling",
            kind=prefix,
            id=record_id,
            revision=str(record.updated_at),
        )
    if item.owner == "model-dossier":
        if prefix != "model_version":
            raise KnowledgeActionError("unsupported_ref", "an exact model version is required")
        dossier = get_model_dossier(workspace_id, record_id)
        if dossier is None:
            raise KnowledgeActionError("missing_evidence", "model version is unavailable")
        version = dossier.identity.version_label
        return JarvisExactRef(
            workspace_id=workspace_id,
            owner="model-dossier",
            kind="model-version",
            id=record_id,
            version=version if isinstance(version, str) and version else None,
            immutable_ref=f"model_version:{record_id}",
            content_digest=_model_dossier_content_digest(dossier),
        )
    if prefix == "literature_source":
        try:
            source = get_literature_source(workspace_id, record_id)
        except LiteratureError as exc:
            raise KnowledgeActionError("missing_evidence", "Literature source is unavailable") from exc
        return JarvisExactRef(
            workspace_id=workspace_id,
            owner="literature",
            kind="source",
            id=record_id,
            revision=source.updated_at,
            immutable_ref=source.source_ref,
        )
    if prefix == "literature_entry":
        source_id = _literature_source_for_entry(workspace_id, record_id)
        if source_id is None:
            raise KnowledgeActionError("missing_evidence", "Literature entry is unavailable")
        try:
            source = get_literature_source(workspace_id, source_id)
        except LiteratureError as exc:
            raise KnowledgeActionError("missing_evidence", "Literature entry source is unavailable") from exc
        entry = next((candidate for candidate in source.entries if candidate.id == record_id), None)
        if entry is None:
            raise KnowledgeActionError("missing_evidence", "Literature entry is unavailable")
        return JarvisExactRef(
            workspace_id=workspace_id,
            owner="literature",
            kind="entry",
            id=record_id,
            revision=entry.updated_at,
            immutable_ref=entry.provenance_ref,
        )
    raise KnowledgeActionError("unsupported_ref", "Literature stable ref kind is unsupported")


def _validate_route_refs(workspace_id: str, route_id: KnowledgeRouteId, refs: list[JarvisExactRef]) -> None:
    expected_owner = _ROUTE_OWNER[route_id]
    for ref in refs:
        if ref.workspace_id != workspace_id or ref.owner != expected_owner:
            raise KnowledgeActionError("identity_conflict", "exact context ref does not match request workspace/route")


def build_knowledge_preview(payload: KnowledgeContextPreviewRequest) -> dict[str, object]:
    exact_refs = _dedupe_exact_refs(
        [exact_ref_from_stable(payload.workspace_id, payload.route_id, item) for item in payload.refs]
    )
    try:
        preview = build_jarvis_context_preview(
            JarvisContextRequest(
                workspace_id=payload.workspace_id,
                route=_route(payload.route_id),
                added_context_refs=exact_refs,
            )
        )
    except JarvisContextConflictError as exc:
        raise KnowledgeActionError("identity_conflict", "exact knowledge refs conflict") from exc
    except JarvisContextError as exc:
        raise KnowledgeActionError("missing_evidence", "exact knowledge evidence is unavailable") from exc
    if not preview.dispatchable:
        if any(outcome.state == "stale" for outcome in preview.ref_outcomes):
            raise KnowledgeActionError("stale_context", "one or more exact knowledge refs are stale")
        raise KnowledgeActionError("missing_evidence", "one or more exact knowledge refs are unavailable")
    if preview.included_count != len(exact_refs):
        raise KnowledgeActionError("missing_evidence", "one or more exact knowledge refs are not dispatchable")
    return {
        "state": "current",
        "workspace_id": payload.workspace_id,
        "route_id": payload.route_id,
        "exact_refs": [ref.model_dump(exclude_none=True, mode="json") for ref in exact_refs],
        "context_digest": preview.context_digest,
        "context_sources_manifest": preview.context_sources_manifest,
        "included_count": preview.included_count,
        "estimated_token_count": preview.estimated_token_count,
    }


def _parse_generated(raw_text: str) -> KnowledgeGeneratedProposal:
    if len(raw_text.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise KnowledgeActionError("proposal_too_large", "proposal exceeds payload limit")
    try:
        return KnowledgeGeneratedProposal.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise KnowledgeActionError("proposal_invalid", "model proposal did not match the closed schema") from exc


def _template_generated(payload: KnowledgeProposalRequest) -> KnowledgeGeneratedProposal:
    return KnowledgeGeneratedProposal(
        summary=f"Advisory proposal grounded in the inspected exact context: {payload.intent}",
        proposed_items=[payload.intent],
        questions=[],
        research_steps=[],
        assumptions=[],
        warnings=["This proposal is advisory only and has not changed domain truth."],
        authoritative_next_action=_ROUTE_NEXT_ACTION[payload.route_id],
    )


def _refusal(reason: KnowledgeRefusalReason) -> dict[str, object]:
    return {"state": "refused", "reason": reason}


AutoRunner = Callable[[AITaskRunRequest], AITaskRunResponse]


def validate_semantic_knowledge_context(exact_refs: list[JarvisExactRef], intent: str, blocks: list[dict]) -> None:
    """Shared existing Memory restrictions, including local conversation dispatch."""
    if any(ref.owner == "modeling" and ref.kind in _PROJECT_KINDS for ref in exact_refs):
        raise KnowledgeActionError(
            "sensitive_context",
            "Project Basis semantic generation is unavailable because sensitivity authority "
            "can change at the dispatch boundary; use the deterministic proposal path",
        )
    if _contains_secret_material([{"intent": intent}, *blocks]):
        raise KnowledgeActionError("sensitive_context", "secret-bearing evidence cannot enter semantic model context")


class KnowledgeActionsService:
    def __init__(self, *, auto_runner: AutoRunner = run_auto_task) -> None:
        self._auto_runner = auto_runner

    def propose(self, payload: KnowledgeProposalRequest) -> dict[str, object]:
        try:
            _validate_route_refs(payload.workspace_id, payload.route_id, payload.exact_refs)
            if _contains_secret_material([{"intent": payload.intent}]):
                raise KnowledgeActionError("sensitive_context", "secret-bearing operator intent cannot enter proposal generation")
            request = JarvisContextRequest(
                workspace_id=payload.workspace_id,
                route=_route(payload.route_id),
                added_context_refs=payload.exact_refs,
            )
            try:
                inspected = require_dispatchable_preview(request, payload.expected_context_digest)
            except JarvisContextConflictError as exc:
                raise KnowledgeActionError("stale_context", "knowledge context changed since inspection") from exc
            except JarvisContextError as exc:
                raise KnowledgeActionError("missing_evidence", "knowledge context cannot be resolved") from exc

            generated_by: dict[str, object]
            if payload.semantic:
                context_blocks = list(inspected.blocks)
                validate_semantic_knowledge_context(payload.exact_refs, payload.intent, context_blocks)
                prompt = (
                    "Return JSON only with keys summary, proposed_items, questions, research_steps, assumptions, "
                    "warnings, authoritative_next_action. Keep the answer advisory; never claim to commit, apply, "
                    "promote, execute, fetch URLs, or mutate domain truth. Operator intent: " + payload.intent
                )
                try:
                    outcome = self._auto_runner(
                        AITaskRunRequest(
                            prompt=prompt,
                            task_kind="synthesis",
                            route_class="auto",
                            context_blocks=context_blocks,
                            include_project_context=False,
                            workspace_id=payload.workspace_id,
                        )
                    )
                except Exception as exc:
                    raise KnowledgeActionError("provider_unavailable", "proposal generation was unavailable") from exc
                if outcome.status != "success" or outcome.response_text is None:
                    raise KnowledgeActionError("provider_unavailable", "proposal generation was unavailable")
                generated = _parse_generated(outcome.response_text)
                generated_by = {
                    "kind": "ai_task",
                    "ai_job_id": outcome.ledger_id,
                    "selected_route_class": outcome.selected_route_class,
                    "provider_id": outcome.provider_id,
                    "model_id": outcome.model_id,
                }
            else:
                generated = _template_generated(payload)
                generated_by = {"kind": "deterministic_template", "template_id": "knowledge-proposal-v1"}

            if _contains_secret_material([{"proposal": generated.model_dump(mode="json")} ]):
                raise KnowledgeActionError("sensitive_context", "secret-bearing proposal content cannot be returned")

            try:
                current = require_dispatchable_preview(request, payload.expected_context_digest)
            except JarvisContextConflictError as exc:
                raise KnowledgeActionError("stale_context", "knowledge context changed during proposal generation") from exc
            except JarvisContextError as exc:
                raise KnowledgeActionError("missing_evidence", "knowledge context became unavailable") from exc
            response: dict[str, object] = {
                "state": "proposed",
                "workspace_id": payload.workspace_id,
                "route_id": payload.route_id,
                "intent": payload.intent,
                "exact_context_refs": [ref.model_dump(exclude_none=True, mode="json") for ref in current.request.added_context_refs],
                "context_digest": current.context_digest,
                "context_sources_manifest": current.context_sources_manifest,
                "target_domain": _ROUTE_DOMAIN[payload.route_id],
                "summary": generated.summary,
                "proposed_items": generated.proposed_items,
                "questions": generated.questions,
                "research_steps": generated.research_steps,
                "assumptions": generated.assumptions,
                "warnings": generated.warnings,
                "authoritative_next_action": _ROUTE_NEXT_ACTION[payload.route_id],
                "generated_by": generated_by,
            }
            if len(json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_PROPOSAL_BYTES:
                raise KnowledgeActionError("proposal_too_large", "proposal exceeds payload limit")
            return response
        except KnowledgeActionError as exc:
            return _refusal(exc.reason)


def route_capabilities(route_id: KnowledgeRouteId) -> list[dict[str, object]]:
    return [capability.model_dump(mode="json") for capability in PRODUCTION_CAPABILITY_REGISTRY.for_route(route_id)]
