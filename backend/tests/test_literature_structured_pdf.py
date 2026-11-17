from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.modules.memory import structured_pdf
from tests.test_literature_knowledge import _workspace_ids, client  # noqa: F401
from tests.test_literature_pdf_pages import make_pdf
from tests.test_literature_source_file import _source, _upload

DOCUMENT = {"pages": [{"page": 2, "blocks": [{"kind": "table", "text": "Rates", "rows": [["rate", "9.9"]]}]}],
            "unlocated": [{"kind": "text", "text": "Floating 3.3"}]}


def _pdf(client: TestClient, workspace: str, texts: list[str] | None = None, data: bytes | None = None) -> tuple[str, str]:  # noqa: F811
    body = data if data is not None else make_pdf(texts or ["Intro", "Rate 9.9"])
    artifact = _upload(client, workspace, "paper.pdf", body).json()["artifact_id"]
    source = _source(client, workspace, artifact_id=artifact)
    return source["id"], f'/workspaces/{workspace}/literature/sources/{source["id"]}/structured'


def _local_bundle(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    python = tmp_path / "python"
    python.write_text("fake")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setenv("JARVISOS_DOCLING_PYTHON", str(python))
    monkeypatch.setenv("JARVISOS_DOCLING_ARTIFACTS", str(bundle))
    return python, bundle


def _fake_interpreter(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-python"
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _wait(client: TestClient, url: str, *, until: str = "running") -> dict:  # noqa: F811
    import time
    for _ in range(100):
        view = client.get(url).json()
        if view["state"] != until:
            return view
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def test_every_view_has_a_reason_code_and_distinct_states(client: TestClient, monkeypatch, tmp_path: Path) -> None:  # noqa: F811
    ws, other = _workspace_ids(client)
    source, url = _pdf(client, ws)
    not_started = client.get(url).json()
    assert (not_started["state"], not_started["reason_code"]) == ("unavailable", "not_started")
    assert not_started["pages"] == [] and not_started["unlocated"] == []
    monkeypatch.delenv("JARVISOS_DOCLING_PYTHON", raising=False)
    missing = client.post(url).json()
    assert (missing["state"], missing["reason_code"]) == ("unavailable", "bundle_missing")
    monkeypatch.setenv("JARVISOS_DOCLING_PYTHON", str(tmp_path / "missing"))
    assert client.post(url).json()["reason_code"] == "bundle_missing"
    assert client.get(f"/workspaces/{other}/literature/sources/{source}/structured").status_code == 404
    assert client.post(f"/workspaces/{other}/literature/sources/{source}/structured").status_code == 404
    _, url_scan = _pdf(client, ws, [""])
    assert client.get(url_scan).json()["reason_code"] == "no_text_layer"
    _, url_bad = _pdf(client, ws, data=b"%PDF-broken")
    assert client.get(url_bad).json()["reason_code"] == "unreadable"
    monkeypatch.setattr(structured_pdf, "MAX_PAGES", 1)
    assert client.get(url).json()["reason_code"] == "too_many_pages"
    monkeypatch.undo()
    for view in (not_started, missing):
        assert str(tmp_path) not in json.dumps(view)


def test_oversized_input_refused(client: TestClient, monkeypatch) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    _, url = _pdf(client, ws)
    monkeypatch.setattr(structured_pdf, "MAX_INPUT", 10)
    assert client.get(url).status_code == 413
    assert client.post(url).status_code == 413


def test_list_and_read_never_convert(client: TestClient, monkeypatch, tmp_path: Path) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    source, url = _pdf(client, ws)
    _local_bundle(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(structured_pdf, "_convert", lambda *args: calls.append(args))
    assert client.get(f"/workspaces/{ws}/literature/sources").status_code == 200
    assert client.get(f"/workspaces/{ws}/literature/sources/{source}").status_code == 200
    assert client.get(f"/workspaces/{ws}/literature/sources/{source}/content").status_code == 200
    assert client.get(url).json()["reason_code"] == "not_started"
    assert calls == []


def test_duplicate_cache_matching_cell_and_datum_authority(client: TestClient, monkeypatch, tmp_path: Path) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    source, url = _pdf(client, ws)
    _local_bundle(monkeypatch, tmp_path)
    entered, release, done = Event(), Event(), Event()
    calls = []

    def fake_convert(path, digest, key, interpreter, artifacts):
        calls.append(path)
        entered.set()
        release.wait(5)
        structured_pdf._write_cache(path, digest, DOCUMENT)
        with structured_pdf._lock:
            structured_pdf._tasks.pop(key, None)
        done.set()

    monkeypatch.setattr(structured_pdf, "_convert", fake_convert)
    running = client.post(url).json()
    assert (running["state"], running["reason_code"]) == ("running", "running")
    assert entered.wait(5)
    assert client.post(url).json()["state"] == "running"
    release.set()
    assert done.wait(5)
    assert len(calls) == 1
    view = client.get(url).json()
    assert view["state"] == "available" and view["reason_code"] == ""
    assert view["pages"][0]["blocks"][0]["rows"][0] == ["rate", "9.9"]
    assert view["unlocated"] == [{"kind": "text", "text": "Floating 3.3"}]
    assert client.post(url).json()["state"] == "available"
    assert len(calls) == 1
    entries = f"/workspaces/{ws}/literature/sources/{source}/entries"
    assert client.get(f"/workspaces/{ws}/literature/sources/{source}").json()["entries"] == []
    # The cell token 9.9 is on page 2 of the generated PDF: the spec-295 check finds it.
    saved = client.post(entries, json={"entry_kind": "datum", "value_text": "9.9", "value_number": 9.9, "unit": "1/s", "locator_kind": "page", "locator_start": 2, "locator_end": 2})
    assert saved.status_code == 201
    assert saved.json()["verdict"] == "usable"
    assert any(check["status"] == "pass" for check in saved.json()["checks"])
    # A number that is not on the cited page is not made usable by the extracted view.
    wrong = client.post(entries, json={"entry_kind": "datum", "value_text": "9.9", "value_number": 9.9, "unit": "1/s", "locator_kind": "page", "locator_start": 1, "locator_end": 1})
    assert wrong.json()["verdict"] == "not_usable"


def test_extractor_and_source_bytes_invalidate_cache(client: TestClient, monkeypatch) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    _, url = _pdf(client, ws)
    path = next(Path(str(p)) for p in _stored_pdfs())
    structured_pdf._write_cache(path, structured_pdf._source_digest(path), DOCUMENT)
    assert client.get(url).json()["state"] == "available"
    monkeypatch.setattr(structured_pdf, "EXTRACTOR", "docling-next")
    assert client.get(url).json()["reason_code"] == "not_started"
    monkeypatch.undo()
    assert client.get(url).json()["state"] == "available"
    cache = structured_pdf._cache(path)
    data = json.loads(cache.read_text())
    data["source_sha256"] = "0" * 64
    cache.write_text(json.dumps(data))
    assert client.get(url).json()["reason_code"] == "not_started"


def _stored_pdfs() -> list[Path]:
    from app.core.paths import build_paths
    return sorted((build_paths().artifacts_dir / "literature").rglob("*.pdf"))


def test_symlinked_cache_refused(client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    _, url = _pdf(client, ws)
    path = _stored_pdfs()[0]
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    structured_pdf._cache(path).symlink_to(target)
    view = client.get(url).json()
    assert (view["state"], view["reason_code"]) == ("unavailable", "unsafe_cache")
    assert client.post(url).json()["reason_code"] == "unsafe_cache"
    assert target.read_text() == "{}"


def test_bounded_rejects_oversize_and_malformed_output(monkeypatch) -> None:
    assert structured_pdf._bounded(DOCUMENT) == DOCUMENT
    assert structured_pdf._bounded({"pages": []}) is None
    assert structured_pdf._bounded({"pages": [], "unlocated": [], "extra": 1}) is None
    block = {"kind": "text", "text": "x"}
    assert structured_pdf._bounded({"pages": [{"page": 1, "blocks": [block] * 3}], "unlocated": []}) == {"pages": [{"page": 1, "blocks": [block] * 3}], "unlocated": []}
    monkeypatch.setattr(structured_pdf, "MAX_BLOCKS", 3)
    assert structured_pdf._bounded({"pages": [{"page": 1, "blocks": [block] * 2}], "unlocated": [block] * 2}) is None
    monkeypatch.undo()
    monkeypatch.setattr(structured_pdf, "MAX_CELLS", 1)
    assert structured_pdf._bounded(DOCUMENT) is None
    assert structured_pdf._bounded({"pages": [], "unlocated": [{"kind": "table", "text": "", "rows": [["a"]]}]}) is not None
    monkeypatch.undo()
    assert structured_pdf._bounded({"pages": [], "unlocated": [{"kind": "text", "text": "x" * 4001}]}) is None
    assert structured_pdf._bounded({"pages": [], "unlocated": [{"kind": "table", "text": "", "rows": [["x" * 501]]}]}) is None
    assert structured_pdf._bounded({"pages": [{"page": 0, "blocks": []}], "unlocated": []}) is None
    assert structured_pdf._bounded({"pages": [], "unlocated": [{"kind": "image"}]}) is None


def test_real_subprocess_path_with_fake_interpreter(client: TestClient, monkeypatch, tmp_path: Path) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    source, url = _pdf(client, ws)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setenv("JARVISOS_DOCLING_ARTIFACTS", str(bundle))
    monkeypatch.setenv("JARVISOS_DOCLING_PYTHON", str(_fake_interpreter(tmp_path, f"import sys\nsys.stdout.write({json.dumps(json.dumps(DOCUMENT))})")))
    assert client.post(url).json()["state"] == "running"
    view = _wait(client, url)
    assert view["state"] == "available"
    assert view["pages"][0]["page"] == 2 and view["unlocated"][0]["text"] == "Floating 3.3"
    assert structured_pdf._cache(_stored_pdfs()[0]).is_file()
    assert str(tmp_path) not in json.dumps(view)


@pytest.mark.parametrize(("body", "code"), [
    ("import sys\nsys.exit(3)", "conversion_failed"),
    ("print('not json')", "conversion_failed"),
    ('print(\'{"pages": "x", "unlocated": []}\')', "conversion_failed"),
    ("import time\ntime.sleep(30)", "timeout"),
])
def test_failed_conversions_are_distinct_and_retryable(client: TestClient, monkeypatch, tmp_path: Path, body: str, code: str) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    _, url = _pdf(client, ws)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setenv("JARVISOS_DOCLING_ARTIFACTS", str(bundle))
    monkeypatch.setenv("JARVISOS_DOCLING_PYTHON", str(_fake_interpreter(tmp_path, body)))
    monkeypatch.setattr(structured_pdf, "TIMEOUT_SECONDS", 1)
    assert client.post(url).json()["state"] == "running"
    view = _wait(client, url)
    assert (view["state"], view["reason_code"]) == ("failed", code)
    assert view["pages"] == [] and str(tmp_path) not in json.dumps(view)
    assert not structured_pdf._cache(_stored_pdfs()[0]).exists()
    assert client.post(url).json()["state"] == "running"  # a failed task may be retried
    _wait(client, url)


def test_worker_limits_are_applied_before_docling_import() -> None:
    source = (Path(structured_pdf.__file__).with_name("structured_pdf_worker.py")).read_text()
    main = source[source.index("def main"):]
    assert main.index("apply_limits()") < main.index("from docling")
    assert "preexec_fn" not in Path(structured_pdf.__file__).read_text()
    assert os.name == "posix"
