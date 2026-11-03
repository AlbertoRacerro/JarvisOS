from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from math import isfinite
from typing import Any

from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import (
    normalize_magnitude,
    parse_unit_token,
    resolve_unit,
    unit_registry,
)
from app.modules.runner.input_contracts import normalize_studio_magnitude, parse_stored_input_contract
from app.modules.runner.safety import RunnerSafetyError

LINKED_PARAMETER_UNUSABLE_CODE = "runner_linked_parameter_unusable"
LINKED_PARAMETER_UNUSABLE_MESSAGE = "A linked Parameter is no longer usable for this queued run."


@dataclass(frozen=True)
class LinkedParameterUsability:
    usable: bool
    parameter: dict[str, object] | None
    reason: str | None


def contract_requires_canonical_linked_parameters(
    contract_payload: str | None,
    contract_sha256: str | None,
) -> bool:
    """Return whether stored contract authority makes source_parameter_id a canonical FK.

    Historical no-contract and schema-v1 runner payloads permit provenance-like source
    tokens. 058c freshness enforcement therefore applies only to authoritative schema-v2/v3
    contracts, never by inspecting payload shape or guessing identifier semantics. Partial or
    malformed stored-contract evidence remains fail-closed through the canonical parser.
    """

    if contract_payload is None and contract_sha256 is None:
        return False
    contract, _ = parse_stored_input_contract(contract_payload, contract_sha256)
    return contract.schema_version in {2, 3}


def inspect_linked_parameter_usability(
    connection: sqlite3.Connection,
    workspace_id: str,
    parameter_id: str,
) -> LinkedParameterUsability:
    """Return fail-closed 058c/098 usability for one canonical linked Parameter.

    This consumes existing Parameter lifecycle plus the canonical 051 freshness
    overlay. It deliberately does not follow replacement links or create a new
    freshness authority.
    """

    row = connection.execute(
        """
        SELECT id, workspace_id, value, unit, status, lifecycle_state, updated_at
        FROM parameters
        WHERE id = ?
        """,
        (parameter_id,),
    ).fetchone()
    if row is None:
        return LinkedParameterUsability(False, None, "missing")

    parameter = dict(row)
    if str(parameter.get("workspace_id")) != workspace_id:
        return LinkedParameterUsability(False, None, "inaccessible")
    if str(parameter.get("status")) == "superseded":
        return LinkedParameterUsability(False, None, "superseded")
    if str(parameter.get("lifecycle_state")) != "active":
        return LinkedParameterUsability(False, None, "noncurrent_lifecycle")

    value = _finite_number(parameter.get("value"))
    unit = parameter.get("unit")
    updated_at = parameter.get("updated_at")
    if value is None:
        return LinkedParameterUsability(False, None, "invalid_value")
    if not isinstance(unit, str) or not unit.strip():
        return LinkedParameterUsability(False, None, "invalid_unit")
    if not isinstance(updated_at, str) or not updated_at.strip():
        return LinkedParameterUsability(False, None, "invalid_revision")

    record_ref = f"parameter:{parameter_id}"
    stale = connection.execute(
        """
        SELECT 1
        FROM freshness_marks
        WHERE workspace_id = ? AND record_ref = ?
        LIMIT 1
        """,
        (workspace_id, record_ref),
    ).fetchone()
    if stale is not None:
        return LinkedParameterUsability(False, None, "stale")

    return LinkedParameterUsability(True, parameter, None)


def load_usable_linked_parameter(
    connection: sqlite3.Connection,
    workspace_id: str,
    parameter_id: str,
) -> dict[str, object] | None:
    result = inspect_linked_parameter_usability(connection, workspace_id, parameter_id)
    return result.parameter if result.usable else None


def require_linked_parameters_usable(
    connection: sqlite3.Connection,
    workspace_id: str,
    normalized_input_payload: str | dict[str, Any],
    *,
    studio_pint: bool = False,
) -> None:
    """Fail closed when a canonical linked source no longer matches its snapshot.

    098 adds an immutable ``source_parameter_updated_at`` token to new schema-v2/v3
    normalized snapshots. New linked snapshots must carry that token and create/final
    claim validation requires exact revision plus physical value identity in the
    normalized contract unit. Historical schema-v2/v3 payloads that predate 098 may
    lack the token; they remain readable historical evidence but fail closed if someone
    attempts to execute them. Schema-v1 and no-contract authority remains unchanged
    because callers invoke this guard only for canonical schema-v2/v3 contracts.
    """

    for item in linked_parameter_items(normalized_input_payload):
        parameter_id = str(item["source_parameter_id"])
        result = inspect_linked_parameter_usability(connection, workspace_id, parameter_id)
        if not result.usable or result.parameter is None:
            _raise_linked_parameter_unusable()

        expected_updated_at = item.get("source_parameter_updated_at")
        expected_value = _finite_number(item.get("value"))
        expected_unit = item.get("unit")
        current_value = _finite_number(result.parameter.get("value"))
        current_unit = result.parameter.get("unit")
        if (
            not isinstance(expected_updated_at, str)
            or not expected_updated_at.strip()
            or result.parameter.get("updated_at") != expected_updated_at
            or expected_value is None
            or current_value is None
            or not isinstance(expected_unit, str)
            or not expected_unit.strip()
            or not isinstance(current_unit, str)
            or not current_unit.strip()
        ):
            _raise_linked_parameter_unusable()

        try:
            try:
                target = resolve_unit(expected_unit)
                physical_dimension = target.physical_dimension
                semantic_basis = target.semantic_basis
            except ProcessKernelError:
                if not studio_pint:
                    raise
                physical_dimension = str(unit_registry().Unit(parse_unit_token(str(expected_unit))).dimensionality)
                semantic_basis = None
            normalizer = normalize_studio_magnitude if studio_pint else normalize_magnitude
            normalized_current_value = normalizer(
                current_value,
                source_unit=current_unit,
                target_unit=expected_unit,
                physical_dimension=physical_dimension,
                semantic_basis=semantic_basis,
            )
        except (ProcessKernelError, ValueError, TypeError):
            _raise_linked_parameter_unusable()
        if normalized_current_value != expected_value:
            _raise_linked_parameter_unusable()


def linked_parameter_items(normalized_input_payload: str | dict[str, Any]) -> tuple[dict[str, Any], ...]:
    payload = _normalized_payload(normalized_input_payload)
    items: list[dict[str, Any]] = []
    for item in payload.values():
        if not isinstance(item, dict):
            continue
        source_parameter_id = item.get("source_parameter_id")
        if source_parameter_id is None:
            continue
        if not isinstance(source_parameter_id, str) or not source_parameter_id.strip():
            raise RunnerSafetyError("runner_input_invalid", "Stored linked Parameter identity is invalid.")
        source_parameter_updated_at = item.get("source_parameter_updated_at")
        if source_parameter_updated_at is not None and (
            not isinstance(source_parameter_updated_at, str) or not source_parameter_updated_at.strip()
        ):
            raise RunnerSafetyError("runner_input_invalid", "Stored linked Parameter revision is invalid.")
        items.append(item)
    return tuple(items)


def linked_parameter_ids(normalized_input_payload: str | dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted({str(item["source_parameter_id"]) for item in linked_parameter_items(normalized_input_payload)})
    )


def _normalized_payload(normalized_input_payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(normalized_input_payload, str):
        try:
            payload = json.loads(normalized_input_payload)
        except json.JSONDecodeError as exc:
            raise RunnerSafetyError("runner_input_invalid", "Stored runner input payload is invalid JSON.") from exc
    else:
        payload = normalized_input_payload

    if not isinstance(payload, dict):
        raise RunnerSafetyError("runner_input_invalid", "Stored runner input payload must be an object.")
    return payload


def _raise_linked_parameter_unusable() -> None:
    raise RunnerSafetyError(
        LINKED_PARAMETER_UNUSABLE_CODE,
        LINKED_PARAMETER_UNUSABLE_MESSAGE,
    )


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None
