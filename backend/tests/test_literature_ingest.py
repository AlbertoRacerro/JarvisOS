from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.database import open_sqlite_connection
from app.modules.ai.contracts import AIRequest, AIResponse, AIUsage
from app.modules.memory import literature_ingest
from app.modules.memory.literature_ingest import QUANTITY_LABELS, Proposal, validate_proposal
from tests.test_literature_knowledge import _workspace_ids, client  # noqa: F401
from tests.test_literature_pdf_pages import make_pdf
from tests.test_literature_source_file import _source, _upload

PAGE_2 = (
    "Results\nThe solubility of CO2 = 0.8 g/L at 25 °C in fresh water.\n"
    "Table 2: run A 1.2 3.4 5.6\n"
)


def _proposal(**overrides: object) -> Proposal:
    data: dict[str, object] = {
        "quantity": "solubility of CO2", "value_text": "0.8", "unit": "g/L",
        "conditions": [{"name": "temperature", "value_text": "25", "unit": "°C"}],
        "page": 2, "quote": "The solubility of CO2 = 0.8 g/L at 25 °C in fresh water.",
    }
    data.update(overrides)
    return Proposal.model_validate(data)


def _check(quote: str, **overrides: object):
    return validate_proposal(_proposal(quote=quote, **overrides), ["", quote])


def test_value_in_the_accepted_pattern_is_source_checked_with_page_number_and_conditions() -> None:
    value = validate_proposal(_proposal(), ["Introduction", PAGE_2])
    assert value.status == "source_checked", value.reasons
    assert value.quantity == "solubility of CO2"
    assert value.value_number == 0.8 and value.unit == "g/L"
    assert value.conditions == [{"name": "temperature", "value": 25.0, "unit": "°C"}]


def test_table_row_value_is_flagged_table_meaning_unverified() -> None:
    value = validate_proposal(
        _proposal(quantity="solubility", value_text="3.4", conditions=[], quote="Table 2: run A 1.2 3.4 5.6"),
        ["", PAGE_2],
    )
    assert value.reasons == ["table_meaning_unverified"]


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"quote": "The solubility of CO2 = 0.9 g/L at 25 °C."}, "quote_not_on_page"),
    ({"page": 1}, "quote_not_on_page"),
    ({"value_text": "0.9"}, "number_not_in_quote"),
    ({"unit": "g/zorkmid"}, "unit_unknown"),
    ({"unit": "m"}, "unit_dimension_mismatch"),
])
def test_source_and_unit_failures_are_flagged_with_their_reason(overrides: dict[str, object], reason: str) -> None:
    value = validate_proposal(_proposal(**overrides), ["Introduction", PAGE_2])
    assert value.status == "flagged"
    assert reason in value.reasons


@pytest.mark.parametrize(("quote", "overrides", "reason"), [
    ("Here viscosity rose while the sample, measured at 20 °C, read 1.1 mPa*s.",
     {"quantity": "viscosity", "value_text": "1.1", "unit": "mPa*s", "conditions": []}, "label_value_unit_shape_missing"),
    ("The turbidity is 4 g/L at 25 °C.", {"quantity": "turbidity", "value_text": "4"}, "label_not_in_table"),
    ("The density is 0.8 1/h at 25 °C.", {"quantity": "density", "unit": "1/h"}, "unit_dimension_mismatch"),
    ("The solubility is 0.8 g/L or solubility is 0.7 g/L at 25 °C.", {"quantity": "solubility"},
     "multiple_numbers_after_separator"),
    ("The solubility is 0.6-0.8 g/L at 25 °C.", {"quantity": "solubility"}, "range_or_ratio"),
    ("The solubility is not 0.8 g/L at 25 °C.", {"quantity": "solubility"}, "negation"),
    ("The solubility is 0.8 g/L, measured near 25 °C.", {"quantity": "solubility"}, "condition_not_in_source"),
    ("The solubility is 0.8 g/L.", {"quantity": "solubility", "conditions": []}, "conditions_missing"),
])
def test_ambiguity_stays_flagged_with_a_distinct_reason(quote: str, overrides: dict[str, object], reason: str) -> None:
    value = _check(quote, **overrides)
    assert value.status == "flagged"
    assert value.reasons == [reason]


def test_label_without_required_conditions_passes_without_a_condition() -> None:
    value = _check("The inner diameter is 0.05 m.", quantity="inner diameter", value_text="0.05", unit="m",
                   conditions=[])
    assert value.status == "source_checked", value.reasons
    assert value.quantity == "diameter" and value.value_number == 0.05


def test_parenthesised_condition_and_pressure_clause_pass() -> None:
    value = _check("The density is 998 kg/m^3 (T = 293 K).", quantity="density", value_text="998", unit="kg/m^3",
                   conditions=[{"name": "T", "value_text": "293", "unit": "K"}])
    assert value.status == "source_checked", value.reasons
    assert value.conditions == [{"name": "temperature", "value": 293.0, "unit": "K"}]


def test_number_is_re_derived_from_the_page_not_the_model() -> None:
    value = _check("The solubility is 8e-1 g/L at 25 °C.", quantity="solubility", value_text="0.8")
    assert value.status == "source_checked", value.reasons
    assert value.value_number == pytest.approx(0.8)


def test_core_label_table_is_generic_and_every_reference_unit_parses() -> None:
    from app.modules.process_kernel.units import parse_unit_token

    for label in QUANTITY_LABELS:
        for unit in label.reference_units:
            parse_unit_token(unit)


def test_read_time_eligibility_requires_current_source_and_a_checked_datum() -> None:
    row = {"ingestion_status": "source_checked", "source_sha256": "a" * 64}
    assert literature_ingest.ingestion_eligibility(row, "a" * 64, "usable") == (True, False, [])
    assert literature_ingest.ingestion_eligibility(row, "b" * 64, "usable") == (False, True, ["stale_source"])
    assert literature_ingest.ingestion_eligibility(row, "a" * 64, "unchecked") == (False, False, ["datum_check_unchecked"])


class _ScriptedAdapter:
    provider_id = "fake"

    def __init__(self, values: list[dict[str, object]]) -> None:
        self.values = values
        self.requests: list[AIRequest] = []

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        body = json.dumps({"values": self.values})
        return AIResponse(provider_id="fake", model_id="fake-model", request_id=request.request_id,
                          text=body, content=body,
                          usage=AIUsage(provider_id="fake", model_id="fake-model", input_tokens=1, output_tokens=1),
                          finish_reason="stop", safety_status="allowed")


def _script(monkeypatch: pytest.MonkeyPatch, values: list[dict[str, object]]) -> _ScriptedAdapter:
    adapter = _ScriptedAdapter(values)
    monkeypatch.setattr(literature_ingest, "INGEST_ROUTE_CLASS", "local:fake")
    monkeypatch.setattr(literature_ingest, "_ADAPTERS", {"fake": adapter})
    return adapter


def test_pdf_upload_ingests_once_and_retains_flagged_values(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    workspace, _ = _workspace_ids(client)
    values = [_proposal(quote="The solubility of CO2 = 0.8 g/L at 25 K.",
                        conditions=[{"name": "temperature", "value_text": "25", "unit": "K"}]).model_dump(),
              _proposal(quantity="solubility", value_text="3.4", unit="g/L",
              conditions=[], quote="Table 2: run A 1.2 3.4 5.6").model_dump()]
    adapter = _script(monkeypatch, values)
    pdf = make_pdf(["Introduction", "The solubility of CO2 = 0.8 g/L at 25 K. Table 2: run A 1.2 3.4 5.6"])
    artifact = _upload(client, workspace, "paper.pdf", pdf).json()["artifact_id"]
    created = _source(client, workspace, artifact_id=artifact)
    source = client.get(f"/workspaces/{workspace}/literature/sources/{created['id']}").json()
    source_id = source["id"]
    batch = source["ingestion_batch"]
    assert batch["state"] == "done" and batch["notification_state"] == "pending"
    assert batch["source_checked_count"] == 1 and batch["flagged_count"] == 1
    entries = {entry["ingestion_status"]: entry for entry in source["entries"]}
    assert entries["source_checked"]["eligible_for_later_admission"] is True
    assert entries["source_checked"]["value_number"] == 0.8
    assert entries["flagged"]["eligible_for_later_admission"] is False
    assert any(check["reason"] == "table_meaning_unverified" for check in entries["flagged"]["ingestion_checks"])
    assert len(adapter.requests) == 1
    assert len(batch["ai_job_ids"]) == 1
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_jobs WHERE id = ?", (batch["ai_job_ids"][0],)).fetchone()[0] == 1
    replay = client.post(f"/workspaces/{workspace}/literature/sources/{source_id}/backing",
                         json={"artifact_id": artifact})
    assert replay.status_code == 409  # Existing backing cannot be re-attached.
    assert literature_ingest.ingest_source(workspace, source_id) == batch["id"]
    assert len(adapter.requests) == 1
    assert len(client.get(f"/workspaces/{workspace}/literature/sources/{source_id}").json()["entries"]) == 2
    acknowledged = client.post(f"/workspaces/{workspace}/literature/sources/{source_id}/ingestion/{batch['id']}/acknowledge")
    assert acknowledged.status_code == 200
    assert client.get(f"/workspaces/{workspace}/literature/sources/{source_id}").json()["ingestion_batch"]["notification_state"] == "read"


def test_duplicate_contradiction_and_replaced_bytes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    workspace, _ = _workspace_ids(client)
    first = "The inner diameter is 0.05 m."
    second = "The inner diameter is 0.06 m."
    values = [_proposal(quantity="inner diameter", value_text="0.05", unit="m", conditions=[], page=1,
                        quote=first).model_dump(),
              _proposal(quantity="inner diameter", value_text="0.05", unit="m", conditions=[], page=1,
                        quote=first).model_dump(),
              _proposal(quantity="inner diameter", value_text="0.06", unit="m", conditions=[], page=1,
                        quote=second).model_dump()]
    _script(monkeypatch, values)
    pdf = make_pdf([f"{first} {second}"])
    artifact = _upload(client, workspace, "a.pdf", pdf).json()["artifact_id"]
    created = _source(client, workspace, artifact_id=artifact)
    url = f"/workspaces/{workspace}/literature/sources/{created['id']}"
    source = client.get(url).json()
    assert source["ingestion_batch"]["duplicate_entry_ids"] == [source["entries"][0]["id"]]
    assert len(source["entries"]) == 2
    assert source["ingestion_batch"]["source_checked_count"] == 1
    assert source["ingestion_batch"]["flagged_count"] == 1
    assert any(check["reason"] == "contradiction" for check in source["entries"][1]["ingestion_checks"])
    replacement = _upload(client, workspace, "b.pdf", pdf + b"\n% changed container\n").json()["artifact_id"]
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE literature_sources SET artifact_id = ? WHERE id = ?", (replacement, created["id"]))
        connection.commit()
    changed = client.get(url).json()
    assert changed["entries"][0]["verdict"] == "usable"
    assert all(entry["stale_source"] and not entry["eligible_for_later_admission"] for entry in changed["entries"])
    assert changed["ingestion_batch"]["current"] is False
    typed = client.post(f"{url}/entries", json={"entry_kind": "datum", "value_text": "0.05", "unit": "m",
                                                "locator_kind": "page", "locator_start": 1, "locator_end": 1})
    assert typed.status_code in (200, 201), typed.text
    assert typed.json()["eligible_for_later_admission"] is False
    assert typed.json()["eligibility_reasons"] == ["not_ingested"]


def test_unexpected_error_fails_the_batch_and_an_interrupted_run_is_retryable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    workspace, _ = _workspace_ids(client)
    adapter = _script(monkeypatch, [_proposal(quote="The solubility of CO2 = 0.8 g/L at 25 K.",
                                              conditions=[{"name": "temperature", "value_text": "25", "unit": "K"}]
                                              ).model_dump()])

    def boom(*_args: object) -> object:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(literature_ingest, "_duplicate_or_contradiction", boom)
    pdf = make_pdf(["Introduction", "The solubility of CO2 = 0.8 g/L at 25 K."])
    artifact = _upload(client, workspace, "paper.pdf", pdf).json()["artifact_id"]
    created = _source(client, workspace, artifact_id=artifact)
    url = f"/workspaces/{workspace}/literature/sources/{created['id']}"
    failed = client.get(url).json()
    assert failed["ingestion_batch"]["state"] == "failed"
    assert failed["ingestion_batch"]["reason"] == "internal_error"
    assert failed["entries"] == []  # nothing half-written survives the rollback

    with open_sqlite_connection() as connection:
        connection.execute("UPDATE literature_ingestion_batches SET state = 'running', updated_at = ? WHERE id = ?",
                           ("2000-01-01T00:00:00+00:00", failed["ingestion_batch"]["id"]))
        connection.commit()
    interrupted = client.get(url).json()["ingestion_batch"]
    assert (interrupted["state"], interrupted["reason"]) == ("failed", "interrupted")
    monkeypatch.undo()
    _script(monkeypatch, adapter.values)
    assert client.post(f"{url}/ingestion/retry").status_code == 202
    done = client.get(url).json()
    assert done["ingestion_batch"]["state"] == "done" and done["ingestion_batch"]["source_checked_count"] == 1
