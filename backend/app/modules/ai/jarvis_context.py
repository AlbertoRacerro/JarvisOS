from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from app.modules.ai.context_builder import DEFAULT_CONTEXT_BUDGET_CHARS, canonical_digest
from app.modules.ai.jarvis_context_models import (
    JarvisActionClass,
    JarvisCapabilityDescriptor,
    JarvisContextPreview,
    JarvisContextRefOutcome,
    JarvisContextRequest,
    JarvisExactRef,
    JarvisResolvedRef,
)

MAX_ADAPTER_PREVIEW_EVIDENCE_CHARS = DEFAULT_CONTEXT_BUDGET_CHARS


class JarvisContextError(ValueError):
    pass


class JarvisContextConflictError(JarvisContextError):
    pass


class JarvisContextAdapter(Protocol):
    def resolve(self, ref: JarvisExactRef) -> JarvisResolvedRef: ...


@dataclass(frozen=True)
class _AdapterKey:
    owner: str
    kind: str


class JarvisContextAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[_AdapterKey, JarvisContextAdapter] = {}

    def register(
        self,
        *,
        owner: str,
        kind: str,
        adapter: JarvisContextAdapter,
        action_classes: tuple[JarvisActionClass, ...] = ("READ", "CONTEXT"),
    ) -> None:
        if "COMMIT" in action_classes or "EXECUTE" in action_classes:
            raise JarvisContextError("common Jarvis registry cannot own COMMIT or EXECUTE")
        key = _AdapterKey(owner=owner, kind=kind)
        if key in self._adapters:
            raise JarvisContextConflictError("Jarvis context adapter is already registered")
        self._adapters[key] = adapter

    def resolve(self, ref: JarvisExactRef) -> JarvisResolvedRef:
        adapter = self._adapters.get(_AdapterKey(owner=ref.owner, kind=ref.kind))
        if adapter is None:
            return JarvisResolvedRef(
                ref=ref,
                state="unknown",
                reason="no production context adapter is registered for this exact ref owner/kind",
            )
        resolved = adapter.resolve(ref)
        if resolved.ref != ref:
            raise JarvisContextConflictError("context adapter changed the requested exact ref identity")
        if "COMMIT" in resolved.action_classes or "EXECUTE" in resolved.action_classes:
            raise JarvisContextError("context adapter exposed forbidden common COMMIT/EXECUTE authority")
        try:
            serialized_evidence = json.dumps(
                {"content": resolved.content, "provenance": resolved.provenance},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError) as exc:
            raise JarvisContextError(
                "context adapter returned non-serializable preview evidence"
            ) from exc
        if len(serialized_evidence) > MAX_ADAPTER_PREVIEW_EVIDENCE_CHARS:
            raise JarvisContextError("context adapter returned oversized preview evidence")
        return resolved


class JarvisCapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[tuple[str, str], JarvisCapabilityDescriptor] = {}

    def register(self, capability: JarvisCapabilityDescriptor) -> None:
        if capability.action_class in {"COMMIT", "EXECUTE"}:
            raise JarvisContextError("common Jarvis capability registry cannot own COMMIT or EXECUTE")
        key = (capability.route_id, capability.capability_id)
        if key in self._capabilities:
            raise JarvisContextConflictError("Jarvis capability id is already registered for this route")
        self._capabilities[key] = capability

    def for_route(self, route_id: str) -> list[JarvisCapabilityDescriptor]:
        return [
            capability
            for capability in self._capabilities.values()
            if capability.route_id == route_id
        ]


# Deliberately empty in production under spec 111. Later domain slices register
# their own read/context adapters and route-scoped capabilities without moving
# domain write or execution authority here.
PRODUCTION_ADAPTER_REGISTRY = JarvisContextAdapterRegistry()
PRODUCTION_CAPABILITY_REGISTRY = JarvisCapabilityRegistry()


def _logical_key(ref: JarvisExactRef) -> tuple[str, str, str, str]:
    return ref.workspace_id, ref.owner, ref.kind, ref.id


def _canonical_ref(ref: JarvisExactRef) -> dict[str, object]:
    return ref.model_dump(exclude_none=True, mode="json")


def _dedupe_refs(refs: list[JarvisExactRef]) -> list[JarvisExactRef]:
    by_key: dict[tuple[str, str, str, str], JarvisExactRef] = {}
    ordered: list[JarvisExactRef] = []
    for ref in refs:
        key = _logical_key(ref)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = ref
            ordered.append(ref)
            continue
        if existing != ref:
            raise JarvisContextConflictError(
                "same logical context ref was supplied with conflicting exact identity"
            )
    return ordered


def _block_for(resolved: JarvisResolvedRef) -> dict[str, object]:
    content = json.dumps(
        {
            "ref": _canonical_ref(resolved.ref),
            "content": resolved.content,
            "provenance": resolved.provenance,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        "source": f"jarvis:{resolved.ref.owner}:{resolved.ref.kind}",
        "type": "jarvis_exact_ref",
        "id": resolved.ref.id,
        "content": content,
    }


def _block_chars(block: dict[str, object]) -> int:
    return len(json.dumps(block, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def build_jarvis_context_preview(
    request: JarvisContextRequest,
    *,
    registry: JarvisContextAdapterRegistry = PRODUCTION_ADAPTER_REGISTRY,
) -> JarvisContextPreview:
    _dedupe_refs([*request.selected_refs, *request.added_context_refs])
    selected_refs = _dedupe_refs(request.selected_refs)
    added_refs = _dedupe_refs(request.added_context_refs)
    for ref in [*selected_refs, *added_refs]:
        if ref.workspace_id != request.workspace_id:
            raise JarvisContextConflictError("context ref workspace does not match request workspace")

    outcomes: list[JarvisContextRefOutcome] = []
    blocks: list[dict[str, object]] = []
    source_manifest: list[dict[str, object]] = []
    char_count = 0
    dispatchable = True

    for ref in added_refs:
        resolved = registry.resolve(ref)
        if resolved.state != "current":
            dispatchable = False
            outcomes.append(
                JarvisContextRefOutcome(
                    ref=ref,
                    state=resolved.state,
                    reason=resolved.reason,
                    provenance=resolved.provenance,
                )
            )
            continue

        block = _block_for(resolved)
        block_chars = _block_chars(block)
        if char_count + block_chars > request.budget_chars:
            outcomes.append(
                JarvisContextRefOutcome(
                    ref=ref,
                    state="current",
                    dropped_for_budget=True,
                    reason="whole context block dropped to preserve deterministic budget",
                    provenance=resolved.provenance,
                )
            )
            continue

        blocks.append(block)
        char_count += block_chars
        source = {"ref": _canonical_ref(ref), "provenance": resolved.provenance}
        source_manifest.append(source)
        outcomes.append(
            JarvisContextRefOutcome(
                ref=ref,
                state="current",
                included=True,
                provenance=resolved.provenance,
            )
        )

    digest_payload = {
        "workspace_id": request.workspace_id,
        "route": request.route.model_dump(mode="json"),
        "selected_refs": [_canonical_ref(ref) for ref in selected_refs],
        "added_context_refs": [_canonical_ref(ref) for ref in added_refs],
        "blocks": blocks,
        "context_sources_manifest": source_manifest,
        "ref_outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
        "budget_chars": request.budget_chars,
    }
    context_digest = canonical_digest(digest_payload)
    included_count = sum(outcome.included for outcome in outcomes)
    dropped_count = sum(outcome.dropped_for_budget for outcome in outcomes)
    return JarvisContextPreview(
        request=request.model_copy(update={"selected_refs": selected_refs, "added_context_refs": added_refs}),
        blocks=blocks,
        context_digest=context_digest,
        context_sources_manifest=source_manifest,
        ref_outcomes=outcomes,
        dispatchable=dispatchable,
        included_count=included_count,
        dropped_count=dropped_count,
        char_count=char_count,
        estimated_token_count=char_count // 4,
        budget_chars=request.budget_chars,
    )


def require_dispatchable_preview(
    request: JarvisContextRequest,
    expected_digest: str,
    *,
    registry: JarvisContextAdapterRegistry = PRODUCTION_ADAPTER_REGISTRY,
) -> JarvisContextPreview:
    preview = build_jarvis_context_preview(request, registry=registry)
    if not preview.dispatchable:
        raise JarvisContextConflictError("Jarvis context contains stale, unavailable, or unknown exact refs")
    if preview.context_digest != expected_digest:
        raise JarvisContextConflictError("Jarvis context changed since inspected preview")
    return preview