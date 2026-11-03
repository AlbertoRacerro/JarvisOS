"""Deterministic parser for opt-in JarvisOS proposed memory records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

MAX_RECORDS = 10
RECORD_VERSION = "jarvis_records_v0"
RECORD_CAPTURE_TASK_KINDS = frozenset({"decision_support"})

JARVIS_RECORDS_PROMPT_FRAGMENT = """Optional structured record capture: if your answer includes candidate engineering decisions, assumptions, or parameters that should be reviewed before becoming project memory, emit at most one fenced code block tagged `jarvis-records`. The block must contain JSON with `record_version` equal to `jarvis_records_v0` and a `records` array of at most 10 decision, assumption, or parameter objects. Do not include workspace_id; JarvisOS supplies workspace scope deterministically. Omit the block entirely when there are no record proposals."""

_BLOCK_RE = re.compile(r"```[ \t]*jarvis-records[ \t]*\r?\n(?P<body>.*?)\r?\n```", re.DOTALL)

SCHEMA_PATH = Path(__file__).resolve().parents[4] / "schemas" / "jarvis_records_v0.schema.json"


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _schema_def(name: str) -> dict[str, Any]:
    return _schema()["$defs"][name]


def _allowed_top_level_keys() -> set[str]:
    return set(_schema()["properties"])


def _schema_for_kind(kind: str) -> dict[str, Any] | None:
    definition = _schema()["$defs"].get(kind)
    if isinstance(definition, dict):
        return definition
    return None


@dataclass(frozen=True)
class RecordParseResult:
    records: list[dict[str, Any]]
    error: str | None = None


def _strip_workspace_fields(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    cleaned = {key: item for key, item in value.items() if key != "workspace_id"}
    records = cleaned.get("records")
    if isinstance(records, list):
        cleaned["records"] = [
            {key: item for key, item in record.items() if key != "workspace_id"} if isinstance(record, dict) else record
            for record in records
        ]
    return cleaned


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return (isinstance(value, int | float) and not isinstance(value, bool))
    if expected == "null":
        return value is None
    return False


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        _validate_schema_value(value, _schema_def(ref_name), path)
        return
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must be {schema['const']}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} has invalid enum value")
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _type_matches(value, expected_type):
        raise ValueError(f"{path} must be {expected_type}")
    if isinstance(expected_type, list) and not any(_type_matches(value, option) for option in expected_type):
        raise ValueError(f"{path} has invalid type")
    if isinstance(value, str) and schema.get("minLength") is not None and len(value) < schema["minLength"]:
        raise ValueError(f"{path} must not be blank")


def _validate_record_with_schema(record: Any, index: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError(f"record #{index} must be an object")
    kind = record.get("record_kind")
    if not isinstance(kind, str):
        raise ValueError(f"record #{index} has invalid record_kind")
    record_schema = _schema_for_kind(kind)
    if record_schema is None:
        raise ValueError(f"record #{index} has invalid record_kind")
    unknown = set(record) - set(record_schema["properties"])
    if unknown:
        raise ValueError(f"record #{index} has unknown keys: {sorted(unknown)}")
    missing = [key for key in record_schema.get("required", []) if key not in record]
    if missing:
        raise ValueError(f"record #{index} is missing required keys: {missing}")
    for key, value in record.items():
        _validate_schema_value(value, record_schema["properties"][key], f"record #{index} {key}")
    if kind == "decision" and (not record["title"].strip() or not record["decision_text"].strip()):
        raise ValueError(f"record #{index} decision requires title and decision_text")
    if kind == "assumption" and not record["statement"].strip():
        raise ValueError(f"record #{index} assumption requires statement")
    if kind == "parameter":
        if not record["name"].strip():
            raise ValueError(f"record #{index} parameter requires name")
        if "unit" in record and not record["unit"].strip():
            raise ValueError(f"record #{index} parameter unit cannot be blank")
    return dict(record)


def _validate_payload(payload: Any) -> list[dict[str, Any]]:
    schema = _schema()
    _validate_schema_value(payload, schema, "payload")
    unknown_top = set(payload) - _allowed_top_level_keys()
    if unknown_top:
        raise ValueError(f"unknown top-level keys: {sorted(unknown_top)}")
    missing_top = [key for key in schema.get("required", []) if key not in payload]
    if missing_top:
        raise ValueError(f"missing top-level keys: {missing_top}")
    _validate_schema_value(payload.get("record_version"), schema["properties"]["record_version"], "record_version")
    records = payload.get("records")
    _validate_schema_value(records, schema["properties"]["records"], "records")
    if len(records) > MAX_RECORDS:
        raise ValueError("records must contain at most 10 items")
    return [_validate_record_with_schema(record, index) for index, record in enumerate(records)]


def parse_jarvis_records_block(response_text: str) -> RecordParseResult:
    match = _BLOCK_RE.search(response_text)
    if match is None:
        return RecordParseResult(records=[])
    try:
        raw_payload = json.loads(match.group("body"))
    except json.JSONDecodeError as exc:
        return RecordParseResult(records=[], error=f"records_json_error: {exc.msg}")

    payload = _strip_workspace_fields(raw_payload)
    dropped = 0
    if isinstance(payload, dict) and isinstance(payload.get("records"), list) and len(payload["records"]) > MAX_RECORDS:
        dropped = len(payload["records"]) - MAX_RECORDS
        payload = {**payload, "records": payload["records"][:MAX_RECORDS]}
    try:
        records = _validate_payload(payload)
    except ValueError as exc:
        return RecordParseResult(records=[], error=f"records_schema_error: {exc}")
    error = f"records_truncated: {dropped} dropped" if dropped else None
    return RecordParseResult(records=records, error=error)
