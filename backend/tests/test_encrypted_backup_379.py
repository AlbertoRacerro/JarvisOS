"""Spec 379: encrypted backup runner, host timer install and the system backup status (offline, fake restic)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.system import backup_status
from app.core.config import get_settings
from app.main import create_app

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


backup = _load("backup")

FAKE_RESTIC = r'''#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
log = Path(os.environ["FAKE_RESTIC_LOG"])
argv = sys.argv[1:]
with log.open("a") as handle:
    handle.write(json.dumps({"argv": argv, "password_env": sorted(k for k in os.environ if k.startswith("RESTIC_PASSWORD"))}) + "\n")
sub = argv[4]
if sub == "backup":
    if os.environ.get("FAKE_RESTIC_FAIL") == "backup":
        sys.exit(1)
    print(json.dumps({"message_type": "status", "percent_done": 0.5}))
    print(json.dumps({"message_type": "summary", "snapshot_id": "abc123", "data_added": 4096}))
elif sub == "cat":
    sys.exit(10 if os.environ.get("FAKE_RESTIC_NO_REPO") else 0)
elif sub == "ls":
    files = json.loads(Path(os.environ["FAKE_RESTIC_FILES"]).read_text())
    print(json.dumps({"struct_type": "snapshot", "time": os.environ["FAKE_RESTIC_TIME"], "id": "abc123"}))
    for path in files:
        print(json.dumps({"struct_type": "node", "type": "file", "path": path, "size": Path(path).stat().st_size}))
elif sub == "restore":
    target = Path(argv[argv.index("--target") + 1])
    for path in json.loads(Path(os.environ["FAKE_RESTIC_FILES"]).read_text()):
        restored = target / path.lstrip("/")
        restored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, restored)
        if os.environ.get("FAKE_RESTIC_CORRUPT") == path:
            data = restored.read_bytes()
            restored.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    Path(os.environ["FAKE_RESTIC_LOG"]).with_suffix(".restored").write_text(str(target))
'''


@pytest.fixture()
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    restic = bin_dir / "restic"
    restic.write_text(FAKE_RESTIC, encoding="utf-8")
    restic.chmod(0o755)
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    files = []
    for index in range(60):
        path = data / ("sub" if index % 2 else "") / f"f{index}.txt"
        path.write_text(f"content {index}\n", encoding="utf-8")
        files.append(str(path))
    (tmp_path / "files.json").write_text(json.dumps(files), encoding="utf-8")
    keys = tmp_path / "keys"
    keys.mkdir()
    config = tmp_path / "backup.toml"
    config.write_text(
        f'repositories = ["{tmp_path}/repo-a", "sftp:example:/repo-b"]\npaths = ["{data}"]\n'
        f'exclude = ["*.tmp"]\npassword_command = "pass show jarvisos/restic"\n', encoding="utf-8")
    status = tmp_path / "state" / "status.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(tmp_path / "restic.log"))
    monkeypatch.setenv("FAKE_RESTIC_FILES", str(tmp_path / "files.json"))
    monkeypatch.setenv("FAKE_RESTIC_TIME", (datetime.now(UTC) + timedelta(minutes=1)).isoformat())
    monkeypatch.setenv(backup.STATUS_FILE_ENV, str(status))
    for name in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "JARVISOS_VAULT_PATH"):
        monkeypatch.delenv(name, raising=False)
    return {"tmp": tmp_path, "config": config, "status": status, "data": data, "files": files, "keys": keys}


def _calls(fake) -> list[dict]:
    log = fake["tmp"] / "restic.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_run_backs_up_forgets_and_checks_each_repository_with_a_password_command(fake) -> None:
    assert backup.main(["run"], config_path=fake["config"]) == 0
    calls = _calls(fake)
    subs = [(c["argv"][1], c["argv"][4]) for c in calls]
    repo_a = f"{fake['tmp']}/repo-a"
    assert subs == [(repo_a, "backup"), (repo_a, "forget"), (repo_a, "check"),
                    ("sftp:example:/repo-b", "backup"), ("sftp:example:/repo-b", "forget"),
                    ("sftp:example:/repo-b", "check")]
    for call in calls:
        assert call["argv"][:4] == ["--repo", call["argv"][1], "--password-command", "pass show jarvisos/restic"]
        assert call["password_env"] == []
    assert calls[0]["argv"][5:] == ["--json", "--exclude", "*.tmp", str(fake["data"])]
    assert calls[1]["argv"][5:] == ["--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "12", "--prune"]
    raw = fake["status"].read_text()
    status = json.loads(raw)
    assert status["last_success_at"] and status["repositories"]["0"]["snapshot_id"] == "abc123"
    assert status["repositories"]["1"]["bytes_added"] == 4096
    for secret_or_path in (str(fake["tmp"]), "sftp:", "pass show", "jarvisos/restic"):
        assert secret_or_path not in raw


def test_failed_backup_records_only_a_reason_code(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RESTIC_FAIL", "backup")
    assert backup.main(["run"], config_path=fake["config"]) == 1
    status = json.loads(fake["status"].read_text())
    assert status["repositories"]["0"]["last_failure_reason"] == "backup_failed"
    assert status["last_failure_reason"] == "repository_failed" and "last_success_at" not in status


@pytest.mark.parametrize("case", ["env_password", "env_password_file", "literal_key", "command_in_path",
                                  "command_in_vault", "command_in_checkout", "relative_command_in_path"])
def test_forbidden_key_sources_refuse_before_any_restic_call(fake, monkeypatch: pytest.MonkeyPatch,
                                                             case: str) -> None:
    text = fake["config"].read_text()
    if case == "env_password":
        monkeypatch.setenv("RESTIC_PASSWORD", "not-a-real-key")
    elif case == "env_password_file":
        monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(fake["keys"] / "k"))
    elif case == "literal_key":
        fake["config"].write_text(text + 'password = "not-a-real-key"\n', encoding="utf-8")
    elif case == "command_in_path":
        fake["config"].write_text(text.replace('"pass show jarvisos/restic"', f'"cat {fake["data"]}/key"'))
    elif case == "command_in_vault":
        vault = fake["tmp"] / "vault"
        vault.mkdir()
        monkeypatch.setenv("JARVISOS_VAULT_PATH", str(vault))
        fake["config"].write_text(text.replace('"pass show jarvisos/restic"', f'"cat {vault}/key"'))
    elif case == "relative_command_in_path":
        (fake["data"] / "key").write_text("not-a-real-key\n", encoding="utf-8")
        monkeypatch.chdir(fake["tmp"])
        fake["config"].write_text(text.replace('"pass show jarvisos/restic"', '"cat data/key"'))
    else:
        checkout = fake["tmp"] / "checkout"
        (checkout / ".git").mkdir(parents=True)
        fake["config"].write_text(text.replace('"pass show jarvisos/restic"', f'"cat {checkout}/secrets/key"'))
    assert backup.main(["run"], config_path=fake["config"]) == 2
    assert _calls(fake) == []
    status = json.loads(fake["status"].read_text())
    assert status["last_failure_reason"] in {"password_in_environment", "literal_password_in_config",
                                             "password_command_inside_backed_up_data"}
    assert "not-a-real-key" not in fake["status"].read_text()


def test_password_command_outside_the_data_is_accepted(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    # The temporary directory may itself sit in a git work tree on a developer machine; the checkout rule has its
    # own case above.
    monkeypatch.setattr(backup, "_in_git_checkout", lambda _path: False)
    text = fake["config"].read_text()
    fake["config"].write_text(text.replace('"pass show jarvisos/restic"', f'"cat {fake["keys"]}/key"'))
    assert backup.main(["run"], config_path=fake["config"]) == 0


@pytest.mark.parametrize("corrupt", [False, True])
def test_restore_test_compares_hashes_and_removes_its_directory(fake, monkeypatch: pytest.MonkeyPatch,
                                                                corrupt: bool) -> None:
    if corrupt:
        monkeypatch.setenv("FAKE_RESTIC_CORRUPT", fake["files"][7])
        monkeypatch.setattr(backup, "SAMPLE_MIN", len(fake["files"]))
    assert backup.main(["restore-test"], config_path=fake["config"]) == (1 if corrupt else 0)
    subs = [c["argv"][4:] for c in _calls(fake)]
    assert subs[0] == ["check", "--read-data-subset", "5%"]
    assert subs[1] == ["ls", "--json", "latest"] and subs[2][:2] == ["restore", "latest"]
    restored_into = Path((fake["tmp"] / "restic.restored").read_text())
    assert not restored_into.exists()
    status = json.loads(fake["status"].read_text())
    assert status["last_restore_test_ok"] is (not corrupt)
    assert status["last_restore_test_reason"] == ("restore_hash_mismatch" if corrupt else None)
    assert "last_failure_at" not in status


def test_restore_test_fails_on_a_size_mismatch(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RESTIC_CORRUPT", "")
    real_restic = backup.restic

    def shrinking(repository, config, *args, **kwargs):
        proc = real_restic(repository, config, *args, **kwargs)
        if args[0] == "restore":
            target = Path(args[args.index("--target") + 1])
            (target / fake["files"][0].lstrip("/")).write_text("x")
        return proc

    monkeypatch.setattr(backup, "restic", shrinking)
    assert backup.main(["restore-test"], config_path=fake["config"]) == 1
    assert json.loads(fake["status"].read_text())["last_restore_test_reason"] == "restore_size_mismatch"


def test_init_creates_only_missing_repositories(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    assert backup.main(["init"], config_path=fake["config"]) == 0
    assert [c["argv"][4] for c in _calls(fake)] == ["cat", "cat"]
    monkeypatch.setenv("FAKE_RESTIC_NO_REPO", "1")
    assert backup.main(["init"], config_path=fake["config"]) == 0
    assert [c["argv"][4] for c in _calls(fake)][2:] == ["cat", "init", "cat", "init"]


def _status_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None) -> None:
    path = tmp_path / "status.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("JARVISOS_BACKUP_STATUS_FILE", str(path))


def test_system_backup_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    _status_file(tmp_path, monkeypatch, None)
    assert backup_status(now)["state"] == "never"
    _status_file(tmp_path, monkeypatch, "{not json")
    assert backup_status(now)["state"] == "failed"
    _status_file(tmp_path, monkeypatch, json.dumps({"repositories": {}}))
    assert backup_status(now)["state"] == "never"
    ok = {"last_success_at": "2026-11-26T03:00:00Z", "last_restore_test_at": "2026-11-01T04:00:00Z",
          "last_restore_test_ok": True, "repositories": {"0": {"snapshot_id": "abc"}}}
    _status_file(tmp_path, monkeypatch, json.dumps(ok))
    assert backup_status(now) == {"state": "ok", "last_success_at": "2026-11-26T03:00:00+00:00",
                                  "last_restore_test_at": "2026-11-01T04:00:00+00:00", "last_restore_test_ok": True}
    _status_file(tmp_path, monkeypatch, json.dumps({**ok, "last_failure_at": "2026-11-26T04:00:00Z"}))
    assert backup_status(now)["state"] == "failed"
    _status_file(tmp_path, monkeypatch, json.dumps({**ok, "last_success_at": "2026-11-24T03:00:00Z"}))
    assert backup_status(now)["state"] == "stale"


def test_system_info_reports_backup_without_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _status_file(tmp_path, monkeypatch, json.dumps({"last_success_at": datetime.now(UTC).isoformat(),
                                                    "repositories": {"0": {"snapshot_id": "abc123"}}}))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        info = client.get("/system/info").json()
    assert info["backup"]["state"] == "ok"
    assert set(info["backup"]) == {"state", "last_success_at", "last_restore_test_at", "last_restore_test_ok"}
    assert "abc123" not in json.dumps(info["backup"])



def test_snapshot_times_are_read_as_utc_with_nanoseconds_and_offsets() -> None:
    utc = datetime(2026, 11, 26, 2, 0, 0, 123456, tzinfo=UTC).timestamp()
    assert backup._parse_time("2026-11-26T03:00:00.123456789+01:00") == utc
    assert backup._parse_time("2026-11-26T02:00:00.123456Z") == utc
    assert backup._parse_time("2026-11-26T02:00:00.123456") == utc
    with pytest.raises(ValueError):
        backup._parse_time("yesterday")
