"""FreeCAD FCStd uploads and pinned visual-only BLUECAD references (spec 327)."""

from __future__ import annotations

import hashlib
import stat
import sys
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.bluecad import freecad_references


def _fcstd(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Document.xml", "<document/>")
    return path.read_bytes()


@pytest.fixture
def fake_freecad(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    executable = tmp_path / "fake-python"
    log = tmp_path / "argv.jsonl"
    executable.write_text(
        "#!" + sys.executable + """
import os, sys, time
log = os.environ["FREECAD_ARGV_LOG"]
with open(log, "a", encoding="utf-8") as out:
    out.write("\\n".join(sys.argv[1:]) + "\\n")
if os.environ.get("FREECAD_TIMEOUT") == "1":
    time.sleep(30)
mode = sys.argv[2]
out_dir = sys.argv[4]
if mode == "list":
    print('[{"name":"Box","label":"Boîte","type_id":"Part::Box"}]')
else:
    try:
        os.makedirs(out_dir, exist_ok=True)
        import build123d as bd
        bd.export_step(bd.Solid.make_box(10, 20, 30), os.path.join(out_dir, "model.step"))
    except Exception as exc:
        open(os.environ.get("FREECAD_ERROR_LOG","/tmp/freecad-error"), "w").write(repr(exc))
        sys.exit(7)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_PATH", str(executable))
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_SHA256", hashlib.sha256(executable.read_bytes()).hexdigest())
    monkeypatch.setenv("FREECAD_ARGV_LOG", str(log))
    monkeypatch.setenv("FREECAD_ERROR_LOG", str(tmp_path / "freecad-error"))
    log.write_text("", encoding="utf-8")
    return executable, log


def _candidate() -> str:
    candidate_id = str(uuid4())
    now = "2026-11-19T09:07:15Z"
    with open_sqlite_connection() as connection:
        connection.execute(
            """INSERT INTO bluecad_candidates
            (id,workspace_id,brief_text,brief_digest,status,origin,loop_config_json,created_at,updated_at)
            VALUES (?, 'main', 'Test part', 'digest', 'valid', 'template', '{}', ?, ?)""",
            (candidate_id, now, now),
        )
        connection.commit()
    return candidate_id


def test_upload_add_integrity_move_remove(
    tmp_path: Path,
    isolated_data_root: None,
    fake_freecad: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_storage(seed_default=True)
    candidate_id = _candidate()
    fcstd = tmp_path / "Piccolo.FCStd"
    body = _fcstd(fcstd)

    with TestClient(create_app()) as client:
        base = "/workspaces/main/bluecad"
        upload = client.post(
            f"{base}/freecad/files",
            content=body,
            headers={"X-Filename": "Piccolo.FCStd", "Content-Type": "application/octet-stream"},
        )
        assert upload.status_code == 201, upload.text
        uploaded = upload.json()
        assert uploaded["objects"][0]["name"] == "Box"
        assert uploaded["file_name"] == "Piccolo.FCStd"

        dedup = client.post(
            f"{base}/freecad/files",
            content=body,
            headers={"X-Filename": "Piccolo.FCStd"},
        )
        assert dedup.status_code == 201
        assert dedup.json()["file_sha256"] == uploaded["file_sha256"]
        source = build_paths().bluecad_freecad_source_file("main", uploaded["file_sha256"])
        assert source.is_file() and source.read_bytes() == body

        bad_name = client.post(f"{base}/freecad/files", content=body, headers={"X-Filename": "../bad.FCStd"})
        assert bad_name.status_code == 400
        assert bad_name.json()["detail"]["code"] == "FREECAD_FILE_INVALID"
        too_large = client.post(f"{base}/freecad/files", content=b"x" * (64 * 1024 * 1024 + 1),
                                headers={"X-Filename": "Large.FCStd"})
        assert too_large.status_code == 413
        assert too_large.json()["detail"]["code"] == "FREECAD_FILE_TOO_LARGE"
        invalid = client.post(f"{base}/freecad/files", content=b"not zip", headers={"X-Filename": "Bad.FCStd"})
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "FREECAD_FILE_INVALID"

        refused = client.post(
            f"{base}/candidates/{candidate_id}/freecad-references",
            json={"file_sha256": uploaded["file_sha256"], "object_name": "bad-name"},
        )
        assert refused.status_code == 400
        before = fake_freecad[1].read_text()
        assert "bad-name" not in before

        ref_base = f"{base}/candidates/{candidate_id}/freecad-references"
        added = client.post(ref_base, json={"file_sha256": uploaded["file_sha256"], "object_name": "Box"})
        assert added.status_code == 201, added.text + (tmp_path / "freecad-error").read_text() if (tmp_path / "freecad-error").exists() else added.text
        reference = added.json()
        assert reference["valid"] is True
        assert "/bluecad/freecad/" not in str(reference)

        duplicate = client.post(ref_base, json={"file_sha256": uploaded["file_sha256"], "object_name": "Box"})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "freecad_reference_duplicate"
        out_dir = build_paths().bluecad_freecad_export_dir("main", reference["fcstd_sha256"], hashlib.sha256(b"Box").hexdigest())
        assert hashlib.sha256((out_dir / "model.step").read_bytes()).hexdigest() == reference["step_sha256"]

        stale_move = client.put(
            f"{ref_base}/{reference['id']}",
            json={"expected_revision": 99, "translation_mm": [1, 2, 3], "rotation_deg": [0, 0, 0]},
        )
        assert stale_move.status_code == 409
        moved = client.put(
            f"{ref_base}/{reference['id']}",
            json={"expected_revision": 1, "translation_mm": [1, 2, 3], "rotation_deg": [0, 0, 0]},
        )
        assert moved.status_code == 200 and moved.json()["revision"] == 2, moved.text

        stale_remove = client.delete(f"{ref_base}/{reference['id']}?expected_revision=1")
        assert stale_remove.status_code == 409
        removed = client.delete(f"{ref_base}/{reference['id']}?expected_revision=2")
        assert removed.status_code == 204


def test_timeout_leaves_no_reference_and_tamper_serves_404(
    tmp_path: Path,
    isolated_data_root: None,
    fake_freecad: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_storage(seed_default=True)
    candidate_id = _candidate()
    body = _fcstd(tmp_path / "Box.FCStd")
    with TestClient(create_app()) as client:
        base = "/workspaces/main/bluecad"
        uploaded = client.post(f"{base}/freecad/files", content=body,
                               headers={"X-Filename": "Box.FCStd"}).json()
        monkeypatch.setenv("FREECAD_TIMEOUT", "1")
        monkeypatch.setattr(freecad_references, "EXPORT_TIMEOUT_SECONDS", 1)
        timed = client.post(
            f"{base}/candidates/{candidate_id}/freecad-references",
            json={"file_sha256": uploaded["file_sha256"], "object_name": "Box"},
        )
        assert timed.status_code == 504
        assert client.get(f"{base}/candidates/{candidate_id}/freecad-references").json() == []
        monkeypatch.delenv("FREECAD_TIMEOUT")
        monkeypatch.setattr(freecad_references, "EXPORT_TIMEOUT_SECONDS", 120)

        added = client.post(
            f"{base}/candidates/{candidate_id}/freecad-references",
            json={"file_sha256": uploaded["file_sha256"], "object_name": "Box"},
        )
        assert added.status_code == 201
        reference = added.json()
        glb_url = reference["glb_url"]
        assert client.get(glb_url).status_code == 200
        glb_bytes = client.get(glb_url).content
        out_dir = build_paths().bluecad_freecad_export_dir(
            "main", reference["fcstd_sha256"], hashlib.sha256(b"Box").hexdigest()
        )
        (out_dir / "model.glb").write_bytes(b"tampered")
        found = client.get(f"{base}/candidates/{candidate_id}/freecad-references").json()[0]
        assert found["valid"] is False and found["finding"]
        assert client.get(glb_url).status_code == 404
        (out_dir / "model.glb").write_bytes(glb_bytes)
        (out_dir / "model.step").write_bytes(b"tampered step")
        assert client.get(reference["step_url"]).status_code == 404


def test_second_candidate_reuses_pinned_export_and_name_survives_restart(
    tmp_path: Path,
    isolated_data_root: None,
    fake_freecad: tuple[Path, Path],
) -> None:
    initialize_storage(seed_default=True)
    first, second = _candidate(), _candidate()
    body = _fcstd(tmp_path / "Shared.FCStd")
    with TestClient(create_app()) as client:
        base = "/workspaces/main/bluecad"
        uploaded = client.post(f"{base}/freecad/files", content=body,
                               headers={"X-Filename": "Shared.FCStd"}).json()
    # A new app instance stands for a backend restart between upload and add.
    with TestClient(create_app()) as client:
        payload = {"file_sha256": uploaded["file_sha256"], "object_name": "Box"}
        one = client.post(f"{base}/candidates/{first}/freecad-references", json=payload)
        assert one.status_code == 201, one.text
        assert one.json()["document_file_name"] == "Shared.FCStd"
        assert one.json()["object_label"] == "Boîte"
        exports_before = fake_freecad[1].read_text().count("export")
        two = client.post(f"{base}/candidates/{second}/freecad-references", json=payload)
        assert two.status_code == 201, two.text
        assert fake_freecad[1].read_text().count("export") == exports_before
        assert two.json()["glb_sha256"] == one.json()["glb_sha256"]
        refs = client.get(f"{base}/candidates/{first}/freecad-references").json()
        assert refs[0]["valid"] is True
