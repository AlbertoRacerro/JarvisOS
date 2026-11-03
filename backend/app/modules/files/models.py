from pydantic import BaseModel, Field


class ArtifactCreate(BaseModel):
    filename: str = Field(min_length=1)
    stored_path: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    mime_type: str | None = None
    sha256: str | None = None
    source_ref: str | None = None
    status: str = "registered"
    notes: str | None = None


class ArtifactRecord(ArtifactCreate):
    id: str
    workspace_id: str
    created_at: str
