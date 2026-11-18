"""Unit and integration tests for numeric QuantitySource metadata (spec 309)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import initialize_database
from app.main import app
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import (
    AddStream,
    AddUnit,
    DraftQuantity,
    QuantitySource,
    SetStreamCulture,
    SetStreamSpec,
    SetUnitParams,
)
from app.modules.workspace_actions.models import ActionOrigin, ActionRequest
from app.modules.workspace_actions.service import apply as apply_action
from app.modules.workspace_actions.service import submit


def test_quantity_source_valid_kinds_and_states() -> None:
    kinds = ["typed", "literature", "ingested", "result"]
    states = ["unchecked", "source_checked", "verified", "flagged", "stale", None]
    for kind in kinds:
        for state in states:
            src = QuantitySource(kind=kind, check_state=state)  # type: ignore[arg-type]
            assert src.kind == kind
            assert src.check_state == state

    src_full = QuantitySource(
        kind="literature",
        check_state="verified",
        ref_id="doi:10.1000/182",
        label="Smith et al. 2024",
    )
    assert src_full.ref_id == "doi:10.1000/182"
    assert src_full.label == "Smith et al. 2024"


def test_quantity_source_validation_failures() -> None:
    with pytest.raises(ValidationError):
        QuantitySource(kind="unsupported_kind")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        QuantitySource(kind="typed", check_state="invalid_state")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        QuantitySource(kind="typed", extra_field="forbidden")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        QuantitySource(kind="typed", ref_id="x" * 129)

    with pytest.raises(ValidationError):
        QuantitySource(kind="typed", label="x" * 161)


def test_draft_quantity_source_omitted_gives_identical_si_output() -> None:
    q_no_source = DraftQuantity(value=25.0, unit="degC")
    assert q_no_source.source is None
    si_out = draft._si(q_no_source, "temperature", "Temperature")
    assert si_out == {"si": 298.15, "value": 25.0, "unit": "degC"}
    assert "source" not in si_out


def test_draft_quantity_si_round_trip_preserves_source() -> None:
    source = QuantitySource(
        kind="literature",
        check_state="verified",
        ref_id="ref-42",
        label="Standard Table",
    )
    q = DraftQuantity(value=25.0, unit="degC", source=source)
    si_out = draft._si(q, "temperature", "Temperature")
    assert "source" in si_out
    assert si_out["source"] == {
        "kind": "literature",
        "check_state": "verified",
        "ref_id": "ref-42",
        "label": "Standard Table",
    }

    # Rebuilding DraftQuantity from stored dict restores source identical to original
    restored = DraftQuantity(
        value=si_out["value"],
        unit=si_out["unit"],
        source=si_out.get("source"),
    )
    assert restored.source is not None
    assert restored.source.model_dump(exclude_none=True) == source.model_dump(exclude_none=True)


def test_draft_ops_preserve_source() -> None:
    doc = draft.empty_document("Test Draft")
    # Unit with parameter having source
    source = QuantitySource(kind="ingested", check_state="source_checked")
    draft.apply_op(doc, AddUnit(op="add_unit", id="u1", type="Heater", tag="H1", x=0, y=0))
    draft.apply_op(
        doc,
        SetUnitParams(
            op="set_unit_params",
            unit="u1",
            values={"outlet_temperature": DraftQuantity(value=350.0, unit="K", source=source)},
        ),
    )
    stored_param = doc["objects"]["u1"]["params"]["outlet_temperature"]
    assert stored_param["source"] == {"kind": "ingested", "check_state": "source_checked"}

    # Stream spec with source
    stream_source = QuantitySource(kind="result", check_state="flagged", label="Simulation run 1")
    draft.apply_op(doc, AddStream(op="add_stream", id="s1", tag="Feed", x=10, y=10))
    draft.apply_op(
        doc,
        SetStreamSpec(
            op="set_stream_spec",
            stream="s1",
            temperature=DraftQuantity(value=20.0, unit="degC", source=stream_source),
        ),
    )
    stored_spec = doc["objects"]["s1"]["spec"]["temperature"]
    assert stored_spec["source"] == {
        "kind": "result",
        "check_state": "flagged",
        "label": "Simulation run 1",
    }


def test_workspace_action_culture_keeps_source_of_untouched_fields() -> None:
    initialize_database()
    with TestClient(app) as client:
        ws = client.post("/workspaces", json={"name": "Source test", "slug": f"src-test-{uuid4().hex[:8]}"}).json()
        wsid = ws["id"]
        created = draft.create_draft(wsid, "Culture source preservation")

        bio_source = QuantitySource(kind="literature", check_state="verified", label="Paper A")
        n_source = QuantitySource(kind="ingested", check_state="source_checked", label="Lab Report")
        sal_source = QuantitySource(kind="typed", check_state="unchecked")

        state = draft.patch(
            wsid,
            created["draft_id"],
            created["revision"],
            [
                AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=0),
                SetStreamCulture(
                    op="set_stream_culture",
                    stream="feed",
                    culture={
                        "biomass": DraftQuantity(value=1.0, unit="g/L", source=bio_source),
                        "nitrogen": DraftQuantity(value=2.0, unit="mg/L", source=n_source),
                        "salinity": DraftQuantity(value=35.0, unit="g/kg", source=sal_source),
                    },
                ),
            ],
        )

        feed_before = next(item for item in draft.projection(wsid, state["draft_id"])["objects"] if item["tag"] == "Feed")
        assert feed_before["spec"]["culture"]["nitrogen"]["source"] == {
            "kind": "ingested",
            "check_state": "source_checked",
            "label": "Lab Report",
        }
        assert feed_before["spec"]["culture"]["salinity"]["source"] == {
            "kind": "typed",
            "check_state": "unchecked",
        }

        # Operator/agent sets biomass to 2.5 g/L via workspace action
        origin = ActionOrigin(kind="local", thread_id="culture-test", interaction_id="309")
        request = ActionRequest.model_validate({
            "surface": "process",
            "base_revision": state["revision"],
            "draft_id": state["draft_id"],
            "actions": [
                {
                    "op": "set_value",
                    "target": "Feed",
                    "property": "biomass",
                    "value": {"value": 2.5, "unit": "g/L"},
                }
            ],
        })
        proposal = submit(wsid, request, origin)
        assert proposal.state == "proposed"
        applied = apply_action(wsid, proposal.action_id)
        assert applied.state == "applied"

        feed_after = next(item for item in draft.projection(wsid, state["draft_id"])["objects"] if item["tag"] == "Feed")
        culture_after = feed_after["spec"]["culture"]

        # Touched field biomass was changed without source metadata -> no source
        assert culture_after["biomass"]["value"] == 2.5
        assert "source" not in culture_after["biomass"]

        # Untouched fields nitrogen and salinity preserved their source metadata!
        assert culture_after["nitrogen"]["source"] == {
            "kind": "ingested",
            "check_state": "source_checked",
            "label": "Lab Report",
        }
        assert culture_after["salinity"]["source"] == {
            "kind": "typed",
            "check_state": "unchecked",
        }
