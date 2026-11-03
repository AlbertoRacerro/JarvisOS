from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.modules.ai.context_builder import DEFAULT_CONTEXT_BUDGET_CHARS

# Shared 145 contract primitives. Every frozen cross-workstream contract uses
# these instead of redefining digest/id/time conventions.
CONTENT_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
ContentDigest = Annotated[str, StringConstraints(pattern=CONTENT_DIGEST_PATTERN)]
ContractId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
OwnerToken = Annotated[
    str,
    StringConstraints(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$"),
]


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Naive datetimes are rejected; aware ones are normalized to UTC so equal
# instants serialize and digest identically.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

JarvisActionClass = Literal[
    "PRESENTATION",
    "READ",
    "CONTEXT",
    "PROPOSE",
    "COMMIT",
    "EXECUTE",
    "NAVIGATE",
]
JarvisResolutionState = Literal["current", "stale", "unavailable", "unknown"]

CANONICAL_ROUTE_PAIRS: dict[str, str] = {
    "design-process": "/design/process",
    "design-bluecad": "/design/bluecad",
    "memory-project-basis": "/memory/project-basis",
    "memory-models": "/memory/models",
    "memory-literature": "/memory/literature",
    "development-roadmap-timeline": "/development/roadmap/timeline",
    "development-roadmap-calendar": "/development/roadmap/calendar",
    "coding-repository": "/coding/repository",
    "coding-runtime": "/coding/runtime",
    "settings-appearance": "/settings/appearance",
    "settings-ai": "/settings/ai",
    "settings-system": "/settings/system",
    "runs": "/runs",
    "engineering-data": "/engineering-data",
    "review": "/review",
    "ai-threads": "/ai-threads",
}


class JarvisRouteDescriptor(BaseModel):
    route_id: str = Field(min_length=1, max_length=96)
    canonical_path: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_frozen_pair(self) -> JarvisRouteDescriptor:
        if CANONICAL_ROUTE_PAIRS.get(self.route_id) != self.canonical_path:
            raise ValueError("route_id and canonical_path must match one frozen canonical route pair")
        return self


class JarvisCapabilityDescriptor(BaseModel):
    capability_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    route_id: str = Field(min_length=1, max_length=96)
    action_class: JarvisActionClass
    label: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def require_canonical_route_id(self) -> JarvisCapabilityDescriptor:
        if self.route_id not in CANONICAL_ROUTE_PAIRS:
            raise ValueError("capability route_id must be one frozen canonical route id")
        return self


class JarvisExactRef(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
    kind: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
    id: str = Field(min_length=1, max_length=256)
    version: str | None = Field(default=None, min_length=1, max_length=256)
    revision: str | None = Field(default=None, min_length=1, max_length=256)
    immutable_ref: str | None = Field(default=None, min_length=1, max_length=512)
    content_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_exact_identity(self) -> JarvisExactRef:
        if not any((self.version, self.revision, self.immutable_ref, self.content_digest)):
            raise ValueError("exact context ref requires version, revision, immutable_ref, or content_digest")
        return self


class SourceLocation(FrozenContract):
    """Position inside an already-identified object; applied after authoritative reread."""

    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    fragment: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def require_ordered_location(self) -> SourceLocation:
        if self.start_line is None and self.end_line is None and self.fragment is None:
            raise ValueError("source location requires a line range or fragment")
        if self.end_line is not None and (self.start_line is None or self.end_line < self.start_line):
            raise ValueError("source location end_line requires start_line <= end_line")
        return self


class SourceRef(FrozenContract):
    """Exact reference to one object in its canonical SQL/Git/artifact owner.

    The single shared ref for retrieval hits, context bundles, evidence refs and
    engineering envelopes. A ref identifies; it never carries authority. Git and
    repository objects may omit workspace_id; ``revision`` is the owner's native
    revision (commit SHA, record version) and ``content_digest`` is
    ``canonical_digest`` of the object content when the owner provides one.
    """

    authority_owner: OwnerToken
    object_type: OwnerToken
    object_id: str = Field(min_length=1, max_length=1024)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    revision: str | None = Field(default=None, min_length=1, max_length=256)
    content_digest: ContentDigest | None = None
    location: SourceLocation | None = None

    @model_validator(mode="after")
    def require_exact_identity(self) -> SourceRef:
        if self.revision is None and self.content_digest is None:
            raise ValueError("source ref requires revision or content_digest")
        return self

    def to_jarvis_exact_ref(self) -> JarvisExactRef:
        """Object-level 111 exact ref for authoritative resolution; location is not part of identity."""
        if self.workspace_id is None:
            raise ValueError("only workspace-scoped source refs map to a Jarvis exact ref")
        return JarvisExactRef(
            workspace_id=self.workspace_id,
            owner=self.authority_owner,
            kind=self.object_type,
            id=self.object_id,
            revision=self.revision,
            content_digest=self.content_digest,
        )


class JarvisContextRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    route: JarvisRouteDescriptor
    selected_refs: list[JarvisExactRef] = Field(default_factory=list, max_length=50)
    added_context_refs: list[JarvisExactRef] = Field(default_factory=list, max_length=50)
    budget_chars: int = Field(
        default=DEFAULT_CONTEXT_BUDGET_CHARS,
        ge=0,
        le=DEFAULT_CONTEXT_BUDGET_CHARS,
    )


class JarvisResolvedRef(BaseModel):
    ref: JarvisExactRef
    state: JarvisResolutionState
    content: dict[str, object] | list[object] | str | int | float | bool | None = None
    provenance: dict[str, object] = Field(default_factory=dict)
    action_classes: list[JarvisActionClass] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def enforce_inert_content(self) -> JarvisResolvedRef:
        if self.state != "current" and self.content is not None:
            raise ValueError("non-current exact refs cannot expose context content")
        return self


class JarvisContextRefOutcome(BaseModel):
    ref: JarvisExactRef
    state: JarvisResolutionState
    included: bool = False
    dropped_for_budget: bool = False
    reason: str | None = None
    provenance: dict[str, object] = Field(default_factory=dict)


class JarvisContextPreview(BaseModel):
    request: JarvisContextRequest
    blocks: list[dict[str, object]] = Field(default_factory=list)
    context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context_sources_manifest: list[dict[str, object]] = Field(default_factory=list)
    ref_outcomes: list[JarvisContextRefOutcome] = Field(default_factory=list)
    dispatchable: bool
    included_count: int
    dropped_count: int
    char_count: int
    estimated_token_count: int
    budget_chars: int
