from app.modules.secrets.storage import resolve_secret_ref


def test_named_systemd_credential_and_environment_precedence(tmp_path, monkeypatch):
    name = "JARVIS_TEST_CREDENTIAL"
    (tmp_path / name).write_text("synthetic-only", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.delenv(name, raising=False)

    from_systemd = resolve_secret_ref(f"env:{name}")
    assert from_systemd.value == "synthetic-only"
    assert from_systemd.source == "secure_persisted"

    monkeypatch.setenv(name, "environment-synthetic")
    from_environment = resolve_secret_ref(f"env:{name}")
    assert from_environment.value == "environment-synthetic"
    assert from_environment.source == "env"


def test_systemd_credential_rejects_symlink_and_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    target = tmp_path / "target"
    target.write_text("synthetic-only", encoding="utf-8")
    (tmp_path / "JARVIS_LINK").symlink_to(target)
    (tmp_path / "JARVIS_BIG").write_bytes(b"x" * 4097)

    assert not resolve_secret_ref("env:JARVIS_LINK").key_present
    assert not resolve_secret_ref("env:JARVIS_BIG").key_present
