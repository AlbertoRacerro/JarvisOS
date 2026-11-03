from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectSearchKind = Literal[
    "requirement",
    "parameter",
    "assumption",
    "decision",
    "model",
    "literature_source",
    "literature_entry",
]
ProjectSearchOwner = Literal["modeling", "model-dossier", "literature"]
ProjectSearchMatchTier = Literal["exact", "prefix", "contains", "normalized", "tokens"]


class StrictProjectSearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSearchResult(StrictProjectSearchModel):
    kind: ProjectSearchKind
    owner: ProjectSearchOwner
    stable_ref: str = Field(min_length=1, max_length=500)
    workspace_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=8000)
    summary: str | None = Field(default=None, max_length=12000)
    lifecycle_or_status: str | None = Field(default=None, max_length=256)
    version_or_revision: str | None = Field(default=None, max_length=500)
    provenance_refs: list[str]
    source_refs: list[str]
    route: Literal["/memory/project-basis", "/memory/models", "/memory/literature"]
    route_params: dict[str, str]
    match_fields: list[str]
    match_tier: ProjectSearchMatchTier


class ProjectSearchResponse(StrictProjectSearchModel):
    query: str
    items: list[ProjectSearchResult]
    total_returned: int
    truncated: bool
