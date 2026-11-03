from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.modules.ai.record_capture import _validate_payload, parse_jarvis_records_block

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "jarvis_records_v0.schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _valid_record(kind: str) -> dict:
    if kind == "decision":
        return {"record_kind": "decision", "title": "Pick pump", "decision_text": "Use pump A."}
    if kind == "assumption":
        return {"record_kind": "assumption", "statement": "Flow is steady."}
    return {"record_kind": "parameter", "name": "Flow rate", "unit": "L/min", "value": "10"}


def _schema_accepts(payload: dict) -> bool:
    if importlib.util.find_spec("jsonschema") is not None:
        import jsonschema

        validator = jsonschema.Draft202012Validator(_schema())
        return not list(validator.iter_errors(payload))

    schema = _schema()
    assert schema["additionalProperties"] is False
    assert all(
        definition["additionalProperties"] is False
        for definition in schema["$defs"].values()
        if definition.get("type") == "object"
    )
    # Use the parser validator as the fallback seam only when no standard JSON Schema validator is installed.
    try:
        _validate_payload(payload)
    except ValueError:
        return False
    return True


def test_schema_file_defines_v0_envelope_and_rejects_unknowns() -> None:
    schema = _schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["records"]["maxItems"] == 10
    assert "workspace_id" not in json.dumps(schema)

    for kind in ("decision", "assumption", "parameter"):
        assert _schema_accepts({"record_version": "jarvis_records_v0", "records": [_valid_record(kind)]})

    assert not _schema_accepts({"record_version": "jarvis_records_v0", "records": [], "extra": True})
    assert not _schema_accepts({"record_version": "jarvis_records_v0", "records": [{**_valid_record("decision"), "extra": True}]})
    assert not _schema_accepts({"record_version": "jarvis_records_v0", "records": [{"record_kind": "requirement"}]})
    assert not _schema_accepts({"record_version": "jarvis_records_v0", "records": [_valid_record("assumption")] * 11})


def test_parser_no_block_is_noop() -> None:
    parsed = parse_jarvis_records_block("plain answer")
    assert parsed.records == []
    assert parsed.error is None


def test_parser_valid_block_returns_records() -> None:
    text = 'answer\n```jarvis-records\n{"record_version":"jarvis_records_v0","records":[{"record_kind":"assumption","statement":"Flow is steady."}]}\n```'
    parsed = parse_jarvis_records_block(text)
    assert parsed.error is None
    assert parsed.records == [{"record_kind": "assumption", "statement": "Flow is steady."}]


def test_parser_rejects_parameter_confidence_string_from_schema() -> None:
    text = (
        'answer\n```jarvis-records\n'
        + json.dumps(
            {
                "record_version": "jarvis_records_v0",
                "records": [{"record_kind": "parameter", "name": "Flow rate", "confidence": "high"}],
            }
        )
        + "\n```"
    )

    parsed = parse_jarvis_records_block(text)

    assert parsed.records == []
    assert parsed.error and parsed.error.startswith("records_schema_error")


def test_parser_malformed_json_and_schema_violation_are_errors_not_exceptions() -> None:
    malformed = parse_jarvis_records_block("```jarvis-records\n{not json}\n```")
    assert malformed.records == []
    assert malformed.error and malformed.error.startswith("records_json_error")

    invalid = parse_jarvis_records_block('```jarvis-records\n{"record_version":"jarvis_records_v0","records":[{"record_kind":"bad"}]}\n```')
    assert invalid.records == []
    assert invalid.error and invalid.error.startswith("records_schema_error")


def test_parser_truncates_eleven_records_and_notes_drop() -> None:
    records = [{"record_kind": "assumption", "statement": f"A{i}"} for i in range(11)]
    text = "```jarvis-records\n" + json.dumps({"record_version": "jarvis_records_v0", "records": records}) + "\n```"
    parsed = parse_jarvis_records_block(text)
    assert len(parsed.records) == 10
    assert parsed.records[-1]["statement"] == "A9"
    assert parsed.error == "records_truncated: 1 dropped"
