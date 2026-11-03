import sqlite3

import pytest

from app.modules.runner.linked_parameters import (
    LINKED_PARAMETER_UNUSABLE_CODE,
    LINKED_PARAMETER_UNUSABLE_MESSAGE,
    contract_requires_canonical_linked_parameters,
    inspect_linked_parameter_usability,
    linked_parameter_ids,
    load_usable_linked_parameter,
    require_linked_parameters_usable,
)
from app.modules.runner.safety import RunnerSafetyError


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE parameters (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            value TEXT,
            unit TEXT NOT NULL,
            status TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE freshness_marks (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            record_ref TEXT NOT NULL
        );
        """
    )
    return connection


def _insert_parameter(
    connection: sqlite3.Connection,
    *,
    parameter_id: str = "parameter-1",
    workspace_id: str = "workspace-1",
    value: object = "12.5",
    unit: str = "m",
    status: str = "accepted",
    lifecycle_state: str = "active",
    updated_at: str = "2026-08-23T00:00:00+00:00",
) -> None:
    connection.execute(
        """
        INSERT INTO parameters (
            id, workspace_id, value, unit, status, lifecycle_state, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (parameter_id, workspace_id, value, unit, status, lifecycle_state, updated_at),
    )


def _snapshot(**overrides: object) -> dict[str, dict[str, object]]:
    item: dict[str, object] = {
        "value": 12.5,
        "unit": "m",
        "source_parameter_id": "parameter-1",
        "source_parameter_updated_at": "2026-08-23T00:00:00+00:00",
    }
    item.update(overrides)
    return {"tube_length": item}


def test_contract_authority_preserves_historical_no_contract_runner_path() -> None:
    assert contract_requires_canonical_linked_parameters(None, None) is False


@pytest.mark.parametrize(
    ("contract_payload", "contract_sha256"),
    [
        (None, "0" * 64),
        ("{}", None),
    ],
)
def test_contract_authority_fails_closed_for_partial_contract_evidence(
    contract_payload: str | None,
    contract_sha256: str | None,
) -> None:
    with pytest.raises(RunnerSafetyError):
        contract_requires_canonical_linked_parameters(contract_payload, contract_sha256)


def test_linked_parameter_usability_accepts_fresh_same_workspace_source() -> None:
    connection = _connection()
    _insert_parameter(connection)

    result = inspect_linked_parameter_usability(connection, "workspace-1", "parameter-1")

    assert result.usable is True
    assert result.reason is None
    assert result.parameter is not None
    assert result.parameter["id"] == "parameter-1"
    assert load_usable_linked_parameter(connection, "workspace-1", "parameter-1") is not None


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("superseded", "superseded"),
        ("inactive", "noncurrent_lifecycle"),
        ("stale", "stale"),
        ("invalid_value", "invalid_value"),
        ("inaccessible", "inaccessible"),
    ],
)
def test_linked_parameter_usability_fails_closed_for_canonical_unusable_states(
    mutation: str,
    reason: str,
) -> None:
    connection = _connection()
    if mutation == "inaccessible":
        _insert_parameter(connection, workspace_id="workspace-2")
    elif mutation == "invalid_value":
        _insert_parameter(connection, value="nan")
    elif mutation == "superseded":
        _insert_parameter(connection, status="superseded", lifecycle_state="superseded")
    elif mutation == "inactive":
        _insert_parameter(connection, lifecycle_state="inactive")
    else:
        _insert_parameter(connection)
        connection.execute(
            "INSERT INTO freshness_marks (id, workspace_id, record_ref) VALUES (?, ?, ?)",
            ("mark-a", "workspace-1", "parameter:parameter-1"),
        )

    result = inspect_linked_parameter_usability(connection, "workspace-1", "parameter-1")

    assert result.usable is False
    assert result.parameter is None
    assert result.reason == reason


def test_linked_parameter_usability_treats_missing_source_as_unusable() -> None:
    connection = _connection()

    result = inspect_linked_parameter_usability(connection, "workspace-1", "missing")

    assert result.usable is False
    assert result.parameter is None
    assert result.reason == "missing"


def test_linked_parameter_ids_are_deterministic_and_deduplicated() -> None:
    payload = {
        "tube_length": {"value": 12.0, "unit": "m", "source_parameter_id": "parameter-b"},
        "tube_inner_diameter": {"value": 80.0, "unit": "mm", "source_parameter_id": "parameter-a"},
        "tube_outer_diameter": {"value": 90.0, "unit": "mm", "source_parameter_id": "parameter-b"},
        "pump_efficiency": {"value": 0.8, "unit": "fraction"},
    }

    assert linked_parameter_ids(payload) == ("parameter-a", "parameter-b")


def test_require_linked_parameters_usable_accepts_exact_snapshot_identity() -> None:
    connection = _connection()
    _insert_parameter(connection)

    require_linked_parameters_usable(connection, "workspace-1", _snapshot())


def test_require_linked_parameters_usable_accepts_convertible_current_source_unit() -> None:
    connection = _connection()
    _insert_parameter(connection, value="12500", unit="mm")

    require_linked_parameters_usable(connection, "workspace-1", _snapshot())


@pytest.mark.parametrize(
    "payload",
    [
        {"tube_length": {"value": 12.5, "unit": "m", "source_parameter_id": "parameter-1"}},
        _snapshot(source_parameter_updated_at="2026-08-23T01:00:00+00:00"),
        _snapshot(value=13.0),
        _snapshot(unit="cm"),
    ],
)
def test_require_linked_parameters_usable_rejects_missing_or_drifted_snapshot_identity(
    payload: dict[str, dict[str, object]],
) -> None:
    connection = _connection()
    _insert_parameter(connection)

    with pytest.raises(RunnerSafetyError) as caught:
        require_linked_parameters_usable(connection, "workspace-1", payload)

    assert caught.value.code == LINKED_PARAMETER_UNUSABLE_CODE
    assert caught.value.message == LINKED_PARAMETER_UNUSABLE_MESSAGE


def test_require_linked_parameters_usable_raises_exact_stale_claim_contract() -> None:
    connection = _connection()
    _insert_parameter(connection, status="superseded", lifecycle_state="superseded")

    with pytest.raises(RunnerSafetyError) as caught:
        require_linked_parameters_usable(connection, "workspace-1", _snapshot())

    assert caught.value.code == LINKED_PARAMETER_UNUSABLE_CODE
    assert caught.value.message == LINKED_PARAMETER_UNUSABLE_MESSAGE
