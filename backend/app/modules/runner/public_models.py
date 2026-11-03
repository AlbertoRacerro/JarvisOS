from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PublicModelImplementationCreate(BaseModel):
    """Caller-visible implementation registration.

    Executable source and trust-shaped fields are intentionally absent.
    """

    model_config = ConfigDict(extra="forbid")

    model_spec_id: str = Field(min_length=1)
    version_label: str = "batch-growth-v0"
    implementation_kind: str = "batch_growth_v0"
    notes: str | None = None
    input_contract: dict[str, Any] | None = None
