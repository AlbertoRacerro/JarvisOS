"""145 frozen retrieval contracts: hits, context bundles and the IndexStore protocol.

Index data is derived and rebuildable. A hit is a pointer plus scores, never
truth: only refs re-read through ``IndexStore.resolve_authoritative`` and found
``current`` may enter a ``ContextBundle``. The deterministic workspace pack in
``context_builder`` (``WorkspaceContextPack``) remains the non-retrieval
context-block assembler.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, Protocol

from pydantic import Field, model_validator

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import (
    ContentDigest,
    ContractId,
    FrozenContract,
    JarvisResolutionState,
    SourceRef,
    UtcDatetime,
)

RETRIEVAL_CONTRACTS_VERSION: Final = "retrieval_contracts.v1"
RetrievalContractsVersion = Literal["retrieval_contracts.v1"]

MAX_EXCERPT_CHARS = 2_000
MAX_DOCUMENT_CHARS = 200_000
MAX_BUNDLE_ITEMS = 64
MAX_MANIFEST_ENTRIES = 256
MAX_EXPANSION_LEVEL = 8

ManifestOutcome = Literal["included", "dropped_budget", "stale", "unavailable", "unknown"]


class RetrievalHit(FrozenContract):
    """Derived-index candidate. Scores are index-relative and only comparable within one search."""

    schema_version: RetrievalContractsVersion = RETRIEVAL_CONTRACTS_VERSION
    source_ref: SourceRef
    lexical_score: float | None = Field(default=None, allow_inf_nan=False)
    vector_score: float | None = Field(default=None, allow_inf_nan=False)
    graph_score: float | None = Field(default=None, allow_inf_nan=False)
    fused_score: float = Field(allow_inf_nan=False)
    excerpt: str | None = Field(default=None, max_length=MAX_EXCERPT_CHARS)
    index_revision: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_component_score(self) -> RetrievalHit:
        if self.lexical_score is None and self.vector_score is None and self.graph_score is None:
            raise ValueError("retrieval hit requires at least one lexical/vector/graph score")
        return self


class IndexDocument(FrozenContract):
    source_ref: SourceRef
    text: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)
    title: str | None = Field(default=None, max_length=512)


class AuthoritativeResolution(FrozenContract):
    """Result of re-reading a ref from its canonical owner.

    ``current`` means the owner still holds exactly the ref's revision/digest;
    a moved revision or digest mismatch is ``stale`` and exposes no content.
    """

    ref: SourceRef
    state: JarvisResolutionState
    current_revision: str | None = Field(default=None, min_length=1, max_length=256)
    current_content_digest: ContentDigest | None = None
    content: str | None = Field(default=None, max_length=MAX_DOCUMENT_CHARS)

    @model_validator(mode="after")
    def enforce_exact_current(self) -> AuthoritativeResolution:
        if self.state != "current":
            if self.content is not None:
                raise ValueError("non-current resolutions cannot expose content")
            return self
        if self.current_content_digest is None:
            raise ValueError("current resolutions require current_content_digest")
        if self.ref.revision is not None and self.current_revision != self.ref.revision:
            raise ValueError("current resolution revision must equal the ref revision")
        if self.ref.content_digest is not None and self.current_content_digest != self.ref.content_digest:
            raise ValueError("current resolution digest must equal the ref content_digest")
        return self


class ContextBundleItem(FrozenContract):
    """One authoritatively re-read ref, in bundle order."""

    source_ref: SourceRef
    content_digest: ContentDigest
    token_estimate: int = Field(ge=0)
    expansion_level: int = Field(ge=0, le=MAX_EXPANSION_LEVEL)

    @model_validator(mode="after")
    def digest_matches_pinned_ref(self) -> ContextBundleItem:
        if self.source_ref.content_digest is not None and self.source_ref.content_digest != self.content_digest:
            raise ValueError("item content_digest must equal the ref's pinned content_digest")
        return self


class ContextManifestEntry(FrozenContract):
    source_ref: SourceRef
    outcome: ManifestOutcome
    reason: str | None = Field(default=None, max_length=256)


def context_bundle_digest(items: Sequence[ContextBundleItem]) -> str:
    """Digest of the ordered resolved content identity; independent of timestamps and manifest."""
    return canonical_digest([item.model_dump(mode="json") for item in items])


class ContextBundle(FrozenContract):
    """Ordered resolved refs plus the evidence manifest of every ref considered.

    ``expansion_level`` is the deepest progressive-expansion level used; items
    never exceed it. ``bundle_digest`` is ``context_bundle_digest(items)`` and is
    what an ``InferenceEnvelope.context_bundle_digest`` pins.
    """

    schema_version: RetrievalContractsVersion = RETRIEVAL_CONTRACTS_VERSION
    bundle_id: ContractId
    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    items: tuple[ContextBundleItem, ...] = Field(default=(), max_length=MAX_BUNDLE_ITEMS)
    evidence_manifest: tuple[ContextManifestEntry, ...] = Field(default=(), max_length=MAX_MANIFEST_ENTRIES)
    token_estimate: int = Field(ge=0)
    token_budget: int = Field(ge=0)
    expansion_level: int = Field(ge=0, le=MAX_EXPANSION_LEVEL)
    index_revision: str | None = Field(default=None, min_length=1, max_length=128)
    bundle_digest: ContentDigest
    created_at: UtcDatetime

    @model_validator(mode="after")
    def validate_bundle(self) -> ContextBundle:
        if self.token_estimate != sum(item.token_estimate for item in self.items):
            raise ValueError("token_estimate must equal the sum of item token estimates")
        if self.token_estimate > self.token_budget:
            raise ValueError("token_estimate exceeds token_budget")
        if any(item.expansion_level > self.expansion_level for item in self.items):
            raise ValueError("item expansion_level exceeds bundle expansion_level")
        included = [entry.source_ref for entry in self.evidence_manifest if entry.outcome == "included"]
        if included != [item.source_ref for item in self.items]:
            raise ValueError("manifest 'included' entries must list exactly the items in order")
        if self.bundle_digest != context_bundle_digest(self.items):
            raise ValueError("bundle_digest does not match items")
        return self


class IndexStore(Protocol):
    """Derived, rebuildable retrieval index (implemented by R-148).

    Every search returns candidates only; callers must pass refs through
    ``resolve_authoritative`` before they enter a ContextBundle.
    """

    def rebuild(self) -> str:
        """Rebuild from canonical owners; returns the new index revision."""
        ...

    def upsert(self, documents: Sequence[IndexDocument]) -> None: ...

    def delete(self, refs: Sequence[SourceRef]) -> None: ...

    def search_lexical(self, query: str, *, limit: int, workspace_id: str | None = None) -> list[RetrievalHit]: ...

    def search_vector(self, query: str, *, limit: int, workspace_id: str | None = None) -> list[RetrievalHit]: ...

    def expand_graph(self, refs: Sequence[SourceRef], *, depth: int, limit: int) -> list[RetrievalHit]: ...

    def resolve_authoritative(self, ref: SourceRef) -> AuthoritativeResolution: ...
