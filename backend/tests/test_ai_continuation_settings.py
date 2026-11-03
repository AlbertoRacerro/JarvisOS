from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.ai.models import AISettingsUpdate


def _initialize(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "continuation-settings"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import initialize_database

    initialize_database()


def test_continuation_setting_default_range_and_policy_version(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    from app.modules.ai.settings import ensure_ai_settings

    settings = ensure_ai_settings()

    assert settings.max_direct_continuations == 8
    assert settings.max_direct_continuations_min == 0
    assert settings.max_direct_continuations_max == 16
    assert settings.direct_continuation_policy_version == "token-flow-v0"


@pytest.mark.parametrize("value", [0, 1, 8, 16])
def test_continuation_setting_accepts_strict_bounded_integers(monkeypatch, tmp_path, value: int) -> None:
    _initialize(monkeypatch, tmp_path)
    from app.modules.ai.settings import update_ai_settings

    updated = update_ai_settings(AISettingsUpdate(max_direct_continuations=value))

    assert updated.max_direct_continuations == value


@pytest.mark.parametrize("value", [-1, 17, True, False, 1.5, "8"])
def test_continuation_setting_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError):
        AISettingsUpdate.model_validate({"max_direct_continuations": value})


def test_ai_settings_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AISettingsUpdate.model_validate({"max_direct_continuations": 8, "continuation_override": 16})


def test_flow_snapshots_setting_and_existing_snapshot_is_immutable(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    from app.modules.ai.settings import update_ai_settings
    from app.modules.ai.token_flow_service import create_flow, get_flow

    update_ai_settings(AISettingsUpdate(max_direct_continuations=3))
    first = create_flow(task_kind="synthesis", requested_route_class="local:fake")

    update_ai_settings(AISettingsUpdate(max_direct_continuations=1))
    second = create_flow(task_kind="synthesis", requested_route_class="local:fake")

    assert first["max_direct_continuations_snapshot"] == 3
    assert get_flow(str(first["id"]))["max_direct_continuations_snapshot"] == 3
    assert second["max_direct_continuations_snapshot"] == 1
