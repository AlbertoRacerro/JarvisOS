from __future__ import annotations


def _init(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "parameter-context-lifecycle"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage

    initialize_storage(seed_default=True)


def _set_lifecycle(parameter_id: str, lifecycle_state: str) -> None:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE parameters SET lifecycle_state = ? WHERE id = ?",
            (lifecycle_state, parameter_id),
        )
        connection.commit()


def test_default_workspace_context_excludes_noncurrent_parameters(monkeypatch, tmp_path) -> None:
    _init(monkeypatch, tmp_path)
    from app.modules.ai.context_builder import build_workspace_context_bundle
    from app.modules.modeling.models import ParameterCreate
    from app.modules.modeling.service import create_parameter

    current = create_parameter(
        "main",
        ParameterCreate(
            name="current_pressure",
            value="34",
            unit="bar",
            value_status="accepted",
            source_ref="operator",
            status="accepted",
        ),
    )
    archived = create_parameter(
        "main",
        ParameterCreate(
            name="archived_pressure",
            value="30",
            unit="bar",
            value_status="accepted",
            source_ref="legacy",
            status="accepted",
        ),
    )
    _set_lifecycle(archived.id, "archived")

    bundle = build_workspace_context_bundle("main")
    parameter_ids = {block["id"] for block in bundle.blocks if block.get("type") == "parameter"}

    assert current.id in parameter_ids
    assert archived.id not in parameter_ids


def test_explicit_context_ids_do_not_bypass_parameter_lifecycle(monkeypatch, tmp_path) -> None:
    _init(monkeypatch, tmp_path)
    from app.modules.ai.context_builder import ContextSelectionSpec, build_workspace_context_bundle
    from app.modules.modeling.models import ParameterCreate
    from app.modules.modeling.service import create_parameter

    current = create_parameter(
        "main",
        ParameterCreate(
            name="current_temperature",
            value="650",
            unit="degC",
            value_status="accepted",
            source_ref="operator",
            status="accepted",
        ),
    )
    deleted = create_parameter(
        "main",
        ParameterCreate(
            name="deleted_temperature",
            value="600",
            unit="degC",
            value_status="accepted",
            source_ref="legacy",
            status="accepted",
        ),
    )
    _set_lifecycle(deleted.id, "deleted")

    bundle = build_workspace_context_bundle(
        "main",
        selection=ContextSelectionSpec(
            kinds=["parameter"],
            ids=[current.id, deleted.id],
            max_items_per_kind=10,
        ),
    )

    assert [block["id"] for block in bundle.blocks] == [current.id]
