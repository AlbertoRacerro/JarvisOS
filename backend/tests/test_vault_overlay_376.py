"""Offline tests for the optional private vault overlay (spec 376)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import search_guard as guard  # noqa: E402

from app.core import packs, vault  # noqa: E402
from app.core.vault import VAULT_PATH_ENV, VaultError, read_vault  # noqa: E402
from app.modules.ai.search_guard_summary import REASON_MESSAGES_IT  # noqa: E402

FAKE_PACK = ROOT / "backend/tests/fixtures/packs/zephyr"
FAKE_VAULT = ROOT / "backend/tests/fixtures/vaults/zephyr"
SECRET = "quince-secret-text"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(VAULT_PATH_ENV, raising=False)
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(FAKE_PACK))
    packs._load.cache_clear()
    vault._load.cache_clear()
    yield
    packs._load.cache_clear()
    vault._load.cache_clear()


def _vault(tmp_path: Path, body: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "jarvis_vault.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_absent_vault_is_empty_and_guard_unchanged() -> None:
    assert vault.load_vault() is None
    assert vault.vault_status() == "absent"
    assert vault.vault_protected_dimensions() == ()
    assert vault.local_only_items() == ()
    assert vault.protected_propositions() == ()
    assert guard.evaluate_query("apple orchard 75 mm spacing")[0] is True
    assert guard.evaluate_query("apple orchard 50 mm spacing")[1] == "protected_dimension"


def test_fake_vault_accessors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VAULT_PATH_ENV, str(FAKE_VAULT))
    assert vault.vault_status() == "loaded"
    (dim,) = vault.vault_protected_dimensions()
    assert (dim.value, dim.unit, dim.tolerance_rel) == (75.0, "mm", 0.02)
    assert [item_id for item_id, _ in vault.local_only_items()] == ["L1"]
    assert [item_id for item_id, _ in vault.protected_propositions()] == ["P1"]


def test_cache_is_isolated_per_env_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _vault(tmp_path / "other", '[[local_only]]\nid = "X9"\ntext = "other"\n')
    monkeypatch.setenv(VAULT_PATH_ENV, str(FAKE_VAULT))
    assert vault.local_only_items()[0][0] == "L1"
    monkeypatch.setenv(VAULT_PATH_ENV, str(other))
    assert vault.local_only_items()[0][0] == "X9"
    monkeypatch.setenv(VAULT_PATH_ENV, "  ")
    assert vault.vault_status() == "absent"


@pytest.mark.parametrize("query, allowed", [
    ("apple orchard 75 mm spacing", False),     # vault dimension with a pack context term
    ("apple orchard 76.4 mm spacing", False),   # inside 2 %
    ("apple orchard 80 mm spacing", True),      # outside tolerance
    ("apple orchard 60-90 mm spacing", True),   # generic range spanning the value
    ("weather 75 mm rain", True),               # no pack context term
])
def test_guard_enforces_vault_dimensions(monkeypatch: pytest.MonkeyPatch, query: str, allowed: bool) -> None:
    monkeypatch.setenv(VAULT_PATH_ENV, str(FAKE_VAULT))
    ok, code, _ = guard.evaluate_query(query)
    assert ok is allowed
    if not allowed:
        assert code == "protected_dimension"


BAD_VAULTS = {
    "unknown_key": f'notes = "{SECRET}"\n',
    "duplicate_id": f'[[local_only]]\nid = "L1"\ntext = "{SECRET}"\n[[local_only]]\nid = "L1"\ntext = "{SECRET}"\n',
    "bad_id": f'[[local_only]]\nid = "-{SECRET}"\ntext = "x"\n',
    "empty_text": '[[protected_propositions]]\nid = "P1"\ntext = "  "\n',
    "long_text": f'[[local_only]]\nid = "L1"\ntext = "{SECRET}{"x" * 2000}"\n',
    "extra_item_key": f'[[local_only]]\nid = "L1"\ntext = "a"\nnote = "{SECRET}"\n',
    "bad_unit": '[[protected_dimensions]]\nvalue = 12.5\nunit = "quincefoot"\n',
    "non_positive": '[[protected_dimensions]]\nvalue = -12.5\nunit = "mm"\n',
    "bad_tolerance": '[[protected_dimensions]]\nvalue = 12.5\nunit = "mm"\ntolerance_rel = 1.5\n',
    "not_a_list": f'local_only = "{SECRET}"\n',
    "bad_toml": f'local_only = [ "{SECRET}"\n',
}


@pytest.mark.parametrize("name", sorted(BAD_VAULTS))
def test_malformed_vault_raises_without_leaking(tmp_path: Path, name: str) -> None:
    root = _vault(tmp_path / name, BAD_VAULTS[name])
    with pytest.raises(VaultError) as info:
        read_vault(root)
    message = str(info.value)
    for leaked in (SECRET, "quincefoot", "12.5", str(tmp_path)):
        assert leaked not in message
    assert info.value.__cause__ is None


def test_set_directory_without_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(VaultError) as info:
        read_vault(tmp_path)
    assert str(tmp_path) not in str(info.value)


def test_broken_vault_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    monkeypatch.setenv(VAULT_PATH_ENV, str(_vault(tmp_path, 'bogus = 1\n')))
    assert guard.evaluate_query("weather today") == (False, "vault_load_error", None)
    assert "vault_load_error" in REASON_MESSAGES_IT
    with pytest.raises(VaultError):
        create_app()
    monkeypatch.setenv(VAULT_PATH_ENV, str(tmp_path / "missing"))
    assert guard.evaluate_query("weather today") == (False, "vault_load_error", None)


@pytest.mark.parametrize("configured", [False, True])
def test_system_status_reports_only_vault_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                configured: bool) -> None:
    from app.main import create_app

    if configured:
        monkeypatch.setenv(VAULT_PATH_ENV, str(FAKE_VAULT))
    with TestClient(create_app()) as client:
        body = client.post("/system/initialize").json()
    assert body["vault"] == ("loaded" if configured else "absent")
    text = json.dumps(body)
    for leaked in ("quince", "orchard", "75.0", "jarvis_vault", str(FAKE_VAULT)):
        assert leaked not in text
