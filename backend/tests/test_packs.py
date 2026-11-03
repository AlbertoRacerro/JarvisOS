"""Project packs: discovery, validation, and the points where a pack changes core behaviour."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.packs import (
    PACK_PATH_ENV,
    PackError,
    default_workspace_name,
    default_workspace_slug,
    load_packs,
    mentions_pack_term,
    read_pack,
    sensitivity_terms,
    studio_example_dirs,
    ui_labels,
)
from app.modules.ai.privacy import PrivacyPolicyEngine
from app.modules.ai.sensitivity import deterministic_floor
from app.modules.workspaces.service import get_default_workspace, seed_default_workspace

FULL_MANIFEST = """
id = "acme"

[workspace]
slug = "acme-lab"
name = "Acme Lab"

[sensitivity]
terms = ["Acme", "project  falcon"]

[ui_labels]
home = "Acme"

[studio]
examples_dir = "examples"
"""


def make_pack(root: Path, manifest: str = FULL_MANIFEST) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "examples").mkdir(exist_ok=True)
    (root / "jarvis_pack.toml").write_text(manifest, encoding="utf-8")
    return root


@pytest.fixture
def no_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PACK_PATH_ENV, raising=False)


def test_zero_packs_gives_generic_defaults(no_pack: None) -> None:
    assert load_packs() == ()
    assert default_workspace_slug() == "main"
    assert default_workspace_name() == "Workspace"
    assert sensitivity_terms() == ()
    assert ui_labels() == {}
    assert studio_example_dirs() == ()
    assert not mentions_pack_term("anything at all")


def test_pack_manifest_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_pack(tmp_path / "acme")
    monkeypatch.setenv(PACK_PATH_ENV, str(root))
    (pack,) = load_packs()
    assert pack.id == "acme"
    assert pack.workspace_slug == "acme-lab" and pack.workspace_name == "Acme Lab"
    assert pack.sensitivity_terms == ("Acme", "project  falcon")
    assert sensitivity_terms() == ("acme", "project falcon")
    assert ui_labels() == {"home": "Acme"}
    assert studio_example_dirs() == ((root / "examples").resolve(),)
    assert default_workspace_slug() == "acme-lab" and default_workspace_name() == "Acme Lab"
    assert mentions_pack_term("About PROJECT FALCON budget")


def test_several_packs_are_combined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_pack(tmp_path / "one", 'id = "one"\n[sensitivity]\nterms = ["alpha"]\n')
    second = make_pack(tmp_path / "two", 'id = "two"\n[workspace]\nslug = "two"\n[sensitivity]\nterms = ["beta", "alpha"]\n')
    monkeypatch.setenv(PACK_PATH_ENV, os.pathsep.join([str(first), str(second)]))
    assert [pack.id for pack in load_packs()] == ["one", "two"]
    assert sensitivity_terms() == ("alpha", "beta")
    assert default_workspace_slug() == "two"


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("", "id is required"),
        ('id = "Bad Id"', "does not match"),
        ('id = "ok"\nsurprise = 1', "unknown keys"),
        ('id = "ok"\n[workspace]\nslug = "Not A Slug"', "slug"),
        ('id = "ok"\n[sensitivity]\nterms = "alpha"', "list of non-empty strings"),
        ('id = "ok"\n[sensitivity]\nterms = [""]', "list of non-empty strings"),
        ('id = "ok"\n[ui_labels]\nhome = 3', "ui_labels values"),
        ('id = "ok"\n[studio]\nexamples_dir = "missing"', "inside the pack"),
        ('id = "ok"\n[studio]\nexamples_dir = ".."', "inside the pack"),
        ("id = ", "cannot be read"),
    ],
)
def test_invalid_manifest_is_a_clear_error(tmp_path: Path, manifest: str, message: str) -> None:
    root = make_pack(tmp_path / "bad", manifest)
    with pytest.raises(PackError, match=message):
        read_pack(root)


def test_missing_manifest_and_duplicate_ids_fail_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(PACK_PATH_ENV, str(empty))
    with pytest.raises(PackError, match="no jarvis_pack.toml"):
        load_packs()
    one = make_pack(tmp_path / "one", 'id = "same"')
    two = make_pack(tmp_path / "two", 'id = "same"')
    monkeypatch.setenv(PACK_PATH_ENV, os.pathsep.join([str(one), str(two)]))
    with pytest.raises(PackError, match="duplicate pack id"):
        load_packs()


def test_broken_pack_stops_application_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    monkeypatch.setenv(PACK_PATH_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(PackError):
        create_app()


def test_default_workspace_is_generic_without_a_pack(no_pack: None) -> None:
    initialize_storage(seed_default=True)
    workspace = get_default_workspace()
    assert (workspace.id, workspace.slug, workspace.name) == ("main", "main", "Workspace")


def test_default_workspace_comes_from_the_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PACK_PATH_ENV, str(make_pack(tmp_path / "acme")))
    initialize_storage(seed_default=True)
    workspace = get_default_workspace()
    assert (workspace.id, workspace.slug, workspace.name) == ("acme-lab", "acme-lab", "Acme Lab")


def test_seed_never_adds_a_second_workspace_to_existing_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.database import open_sqlite_connection

    monkeypatch.setenv(PACK_PATH_ENV, str(make_pack(tmp_path / "acme")))
    initialize_storage(seed_default=True)
    monkeypatch.delenv(PACK_PATH_ENV)  # an existing database is opened without its pack
    first = seed_default_workspace()
    assert first.id == "acme-lab"
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1


def test_sensitivity_terms_feed_the_egress_guard(no_pack: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "Falcon geometry decision for the next review"
    engine = PrivacyPolicyEngine()
    assert deterministic_floor(text) is None
    assert engine.classify("falcon notes") == "unknown"
    assert engine.decide_for_smoke_console("falcon notes").privacy_class != "confidential"

    root = make_pack(tmp_path / "falcon", 'id = "falcon"\n[sensitivity]\nterms = ["Falcon"]\n')
    monkeypatch.setenv(PACK_PATH_ENV, str(root))
    assert deterministic_floor(text) == "S3"
    assert deterministic_floor("Analizza la geometria del progetto Falcon") == "S3"
    assert engine.classify("falcon notes") == "confidential"
    assert engine.decide_for_smoke_console("falcon notes").privacy_class == "confidential"
    assert engine.decide_for_smoke_console("falcon sensitive plan").privacy_class == "sensitive_ip"


def test_fixture_pack_is_active_for_the_suite() -> None:
    assert [pack.id for pack in load_packs()] == ["zephyr"]
    assert deterministic_floor("Zephyr geometry decision") == "S3"


def test_system_status_reports_default_workspace_and_packs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    monkeypatch.setenv(PACK_PATH_ENV, str(make_pack(tmp_path / "acme")))
    with TestClient(create_app()) as client:
        body = client.post("/system/initialize").json()
    assert body["default_workspace"] == "acme-lab"
    assert body["packs"] == [{"id": "acme", "ui_labels": {"home": "Acme"}}]
