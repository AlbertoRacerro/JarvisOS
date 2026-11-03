from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

EffectiveSource = Literal["environment", "secure_persisted", "none"]
PersistedState = Literal["absent", "usable", "corrupted", "unavailable"]
StorageMode = Literal[
    "environment",
    "secure_persisted",
    "none",
    "corrupted",
    "unavailable",
]


class ScalewaySecretStatus(BaseModel):
    key_present: bool
    source: str
    effective_source: EffectiveSource = "none"
    persisted_state: PersistedState = "absent"
    masked_preview: str | None = None
    last_updated_at: str | None = None
    storage_mode: StorageMode = "none"
    reason_code: str | None = None
