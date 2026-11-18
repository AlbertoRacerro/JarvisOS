"""Spec 310: tests for scripts/fetch_file.py."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FETCH_FILE_SCRIPT = REPO_ROOT / "scripts" / "fetch_file.py"
SAMPLE_PAYLOAD = b"Hello, JarvisOS! This is a test file for fetch_file.py." * 100
SAMPLE_SHA256 = hashlib.sha256(SAMPLE_PAYLOAD).hexdigest()


class DownloadTestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # Suppress server access log

    def do_GET(self) -> None:
        if self.path.startswith("/ok") or self.path.startswith("/query"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(SAMPLE_PAYLOAD)))
            self.end_headers()
            self.wfile.write(SAMPLE_PAYLOAD)
        elif self.path.startswith("/early_close"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(SAMPLE_PAYLOAD) + 5000))
            self.end_headers()
            self.wfile.write(SAMPLE_PAYLOAD[:50])
            self.close_connection = True
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def http_server() -> tuple[str, int]:
    server = HTTPServer(("127.0.0.1", 0), DownloadTestHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield host, port
    server.shutdown()
    server.server_close()


def run_fetch(
    *args: str,
    control_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "RELAY_CONTINUITY_DIR": str(control_dir),
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(FETCH_FILE_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def test_success_transfer_and_record(http_server: tuple[str, int], tmp_path: Path) -> None:
    host, port = http_server
    control_dir = tmp_path / "control"
    dest = tmp_path / "downloads" / "output.bin"
    url = f"http://{host}:{port}/ok"

    proc = run_fetch(
        url,
        str(dest),
        "--sha256",
        SAMPLE_SHA256,
        "--label",
        "Test Output File",
        control_dir=control_dir,
    )

    assert proc.returncode == 0, f"fetch failed: {proc.stderr}"
    assert dest.is_file()
    assert dest.read_bytes() == SAMPLE_PAYLOAD
    assert not dest.with_name(f"{dest.name}.part").exists()

    d_dir = control_dir / "downloads"
    records = list(d_dir.glob("*.json"))
    assert len(records) == 1

    rec = json.loads(records[0].read_text(encoding="utf-8"))
    assert rec["state"] == "succeeded"
    assert rec["bytes_done"] == len(SAMPLE_PAYLOAD)
    assert rec["bytes_total"] == len(SAMPLE_PAYLOAD)
    assert rec["sha256"] == SAMPLE_SHA256
    assert rec["label"] == "Test Output File"
    assert rec["host"] == host
    assert rec["file_name"] == "output.bin"
    assert rec["reason"] is None
    assert rec["ended_at"] is not None
    assert isinstance(rec["pid"], int)


def test_checksum_mismatch(http_server: tuple[str, int], tmp_path: Path) -> None:
    host, port = http_server
    control_dir = tmp_path / "control"
    dest = tmp_path / "downloads" / "mismatch.bin"
    wrong_sha = "0" * 64
    url = f"http://{host}:{port}/ok"

    proc = run_fetch(
        url,
        str(dest),
        "--sha256",
        wrong_sha,
        control_dir=control_dir,
    )

    assert proc.returncode != 0
    assert not dest.exists()
    assert not dest.with_name(f"{dest.name}.part").exists()

    d_dir = control_dir / "downloads"
    records = list(d_dir.glob("*.json"))
    assert len(records) == 1

    rec = json.loads(records[0].read_text(encoding="utf-8"))
    assert rec["state"] == "failed"
    assert "checksum mismatch" in (rec.get("reason") or "")


def test_existing_dest_refused_before_request(http_server: tuple[str, int], tmp_path: Path) -> None:
    host, port = http_server
    control_dir = tmp_path / "control"
    dest = tmp_path / "downloads" / "existing.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("pre-existing content", encoding="utf-8")

    url = f"http://{host}:{port}/ok"
    proc = run_fetch(
        url,
        str(dest),
        control_dir=control_dir,
    )

    assert proc.returncode != 0
    assert "destination already exists" in proc.stderr
    assert dest.read_text(encoding="utf-8") == "pre-existing content"
    assert not dest.with_name(f"{dest.name}.part").exists()

    d_dir = control_dir / "downloads"
    # No download record should have been created since existing DEST was refused before any request
    records = list(d_dir.glob("*.json")) if d_dir.exists() else []
    assert len(records) == 0


def test_refused_non_loopback_http(tmp_path: Path) -> None:
    control_dir = tmp_path / "control"
    dest = tmp_path / "downloads" / "refused.bin"
    url = "http://example.com/file.bin"

    proc = run_fetch(
        url,
        str(dest),
        control_dir=control_dir,
    )

    assert proc.returncode != 0
    assert "refused URL" in proc.stderr
    assert not dest.exists()


def test_server_closing_early(http_server: tuple[str, int], tmp_path: Path) -> None:
    host, port = http_server
    control_dir = tmp_path / "control"
    dest = tmp_path / "downloads" / "early.bin"
    url = f"http://{host}:{port}/early_close"

    proc = run_fetch(
        url,
        str(dest),
        control_dir=control_dir,
    )

    assert proc.returncode != 0
    assert not dest.exists()
    assert not dest.with_name(f"{dest.name}.part").exists()

    d_dir = control_dir / "downloads"
    records = list(d_dir.glob("*.json"))
    assert len(records) == 1

    rec = json.loads(records[0].read_text(encoding="utf-8"))
    assert rec["state"] == "failed"
    assert rec["reason"] is not None


def test_record_has_no_url_path_query_or_destination_directory(
    http_server: tuple[str, int], tmp_path: Path
) -> None:
    host, port = http_server
    control_dir = tmp_path / "control"
    dest_dir = tmp_path / "very" / "deep" / "secret_folder"
    dest = dest_dir / "target_file.bin"
    secret_token = "secret_token_12345"
    url = f"http://{host}:{port}/query/nested/path/to/resource.bin?token={secret_token}&source=upstream"

    proc = run_fetch(
        url,
        str(dest),
        control_dir=control_dir,
    )

    assert proc.returncode == 0, f"fetch failed: {proc.stderr}"
    assert dest.is_file()

    d_dir = control_dir / "downloads"
    records = list(d_dir.glob("*.json"))
    assert len(records) == 1

    record_raw = records[0].read_text(encoding="utf-8")
    assert "/nested/path/to/resource.bin" not in record_raw
    assert secret_token not in record_raw
    assert "source=upstream" not in record_raw
    assert str(dest_dir) not in record_raw
    assert "secret_folder" not in record_raw
