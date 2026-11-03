from pydantic import BaseModel


class EventRecord(BaseModel):
    id: str
    workspace_id: str | None = None
    event_type: str
    actor: str
    target_type: str
    target_id: str | None = None
    payload: str | None = None
    created_at: str
