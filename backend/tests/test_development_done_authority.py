import pytest
from pydantic import ValidationError

from app.modules.development.models import RoadmapItemCreate, RoadmapItemUpdate


def test_client_cannot_claim_done_when_satisfaction() -> None:
    with pytest.raises(ValidationError):
        RoadmapItemCreate(
            workspace_id="workspace",
            title="Item",
            item_type="Task",
            done_when="Evidence accepted",
            done_when_satisfied=True,
            created_by="operator",
        )

    with pytest.raises(ValidationError):
        RoadmapItemUpdate(
            workspace_id="workspace",
            expected_revision=1,
            status="Done",
            done_when_satisfied=True,
            actor="operator",
        )
