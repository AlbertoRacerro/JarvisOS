from pydantic import BaseModel, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    description: str | None = None
    status: str = "active"


class WorkspaceRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceRead(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None = None
    status: str
    created_at: str
    updated_at: str
