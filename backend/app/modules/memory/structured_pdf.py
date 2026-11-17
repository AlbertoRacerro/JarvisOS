"""Optional, offline derived view of a registered literature PDF."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.database import open_sqlite_connection
from app.modules.memory.literature_service import LiteratureError, resolve_literature_content
from app.modules.memory.pdf_text import PdfTextError, cached_pdf_pages

EXTRACTOR = "docling-2.150.1-layout-table-v1"
MAX_INPUT = 20 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_PAGES = 200
MAX_BLOCKS = 2000
MAX_CELLS = 4000
TIMEOUT_SECONDS = 100
_lock = threading.Lock()
_tasks: dict[str, dict] = {}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="literature-pdf")


def _source(workspace_id: str, source_id: str) -> tuple[Path, str]:
    content = resolve_literature_content(workspace_id, source_id)
    if content.media_type != "application/pdf":
        raise LiteratureError("literature_pdf_required", "A registered PDF is required.", status_code=422)
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT a.stored_path FROM literature_sources s JOIN artifacts a ON a.id=s.artifact_id AND a.workspace_id=s.workspace_id WHERE s.id=? AND s.workspace_id=?", (source_id, workspace_id)).fetchone()
    path = content.path
    raw_path = Path(str(row["stored_path"])) if row is not None else path
    if raw_path.is_symlink() or any(parent.is_symlink() for parent in raw_path.parents):
        raise LiteratureError("literature_pdf_unsafe", "PDF backing is unsafe.", status_code=422)
    if path.stat().st_size > MAX_INPUT:
        raise LiteratureError("literature_pdf_too_large", "PDF exceeds the extraction limit.", status_code=413)
    return path, _source_digest(path)


def _source_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _cache(path: Path) -> Path:
    return path.with_suffix(".structured.json")


def _key(path: Path, digest: str) -> str:
    return f"{path}:{digest}:{EXTRACTOR}"


def _valid_block(block: object) -> int | None:
    """Return the number of table cells in a valid block, or None when it is malformed or oversized."""
    if not isinstance(block, dict) or block.get("kind") not in {"text", "table"}:
        return None
    text = block.get("text", "")
    if not isinstance(text, str) or len(text) > 4000:
        return None
    if block["kind"] == "text":
        return 0
    rows = block.get("rows")
    if not isinstance(rows, list):
        return None
    cells = 0
    for row in rows:
        if not isinstance(row, list) or not all(isinstance(cell, str) and len(cell) <= 500 for cell in row):
            return None
        cells += len(row)
    return cells


def _bounded(document: object) -> dict | None:
    if not isinstance(document, dict) or set(document) != {"pages", "unlocated"}:
        return None
    pages, unlocated = document["pages"], document["unlocated"]
    if not isinstance(pages, list) or not isinstance(unlocated, list) or len(pages) > MAX_PAGES:
        return None
    blocks = list(unlocated)
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("page"), int) or not 1 <= page["page"] <= MAX_PAGES:
            return None
        if not isinstance(page.get("blocks"), list):
            return None
        blocks.extend(page["blocks"])
    if len(blocks) > MAX_BLOCKS:
        return None
    cells = 0
    for block in blocks:
        count = _valid_block(block)
        if count is None:
            return None
        cells += count
    return document if cells <= MAX_CELLS else None


def _read(path: Path, digest: str) -> dict | None:
    cache = _cache(path)
    try:
        descriptor = os.open(cache, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_OUTPUT + 1)
        if len(raw) > MAX_OUTPUT:
            return None
        data = json.loads(raw)
        if data.get("source_sha256") == digest and data.get("extractor") == EXTRACTOR:
            return _bounded(data.get("document"))
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _view(state: str, code: str, reason: str, document: dict | None = None) -> dict:
    return {"state": state, "reason_code": code, "reason": reason,
            "pages": document["pages"] if document else [], "unlocated": document["unlocated"] if document else []}


class _ConversionError(Exception):
    pass


def _run_worker(path: Path, python: Path, artifacts: Path) -> dict:
    worker = Path(__file__).with_name("structured_pdf_worker.py")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": tempfile.gettempdir(),
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
           "DOCLING_ARTIFACTS_PATH": str(artifacts)}
    result = subprocess.run([str(python), str(worker), str(path), str(artifacts)], cwd=tempfile.gettempdir(),
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_SECONDS,
                            check=False)  # noqa: S603 - configured local interpreter and fixed worker; it limits itself.
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        raise _ConversionError("Local conversion failed or exceeded its output limit.")
    try:
        document = _bounded(json.loads(result.stdout))
    except ValueError as exc:
        raise _ConversionError("Local conversion returned unreadable output.") from exc
    if document is None:
        raise _ConversionError("Local conversion exceeded its content limits.")
    return document


def _write_cache(path: Path, digest: str, document: dict) -> None:
    data = json.dumps({"source_sha256": digest, "extractor": EXTRACTOR, "document": document}).encode()
    if len(data) > MAX_OUTPUT:
        raise _ConversionError("Local conversion exceeded its output limit.")
    cache = _cache(path)
    if cache.is_symlink():
        raise _ConversionError("Derived cache is unsafe.")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".structured-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if cache.is_symlink():
            raise _ConversionError("Derived cache is unsafe.")
        os.replace(name, cache)
    finally:
        Path(name).unlink(missing_ok=True)


def _convert(path: Path, digest: str, key: str, python: Path, artifacts: Path) -> None:
    try:
        document = _run_worker(path, python, artifacts)
        _write_cache(path, digest, document)
        state = None  # the cache is now the single source of the result
    except subprocess.TimeoutExpired:
        state = _view("failed", "timeout", "Local conversion timed out.")
    except Exception:  # noqa: BLE001 - any failure must end the task as failed, never leave it running
        state = _view("failed", "conversion_failed", "Local conversion failed.")
    with _lock:
        if state is None:
            _tasks.pop(key, None)
        else:
            _tasks[key] = state


def structured_view(workspace_id: str, source_id: str, *, start: bool = False) -> dict:
    path, digest = _source(workspace_id, source_id)
    try:
        pages = cached_pdf_pages(path)
    except PdfTextError:
        return _view("unavailable", "unreadable", "PDF text layer is unreadable.")
    if not pages:
        return _view("unavailable", "no_text_layer", "PDF has no readable text layer; scans are unsupported.")
    if len(pages) > MAX_PAGES:
        return _view("unavailable", "too_many_pages", "PDF exceeds the page limit.")
    if _cache(path).is_symlink():
        return _view("unavailable", "unsafe_cache", "Derived cache is unsafe.")
    cached = _read(path, digest)
    if cached is not None:
        return _view("available", "", "", cached)
    key = _key(path, digest)
    with _lock:
        state = _tasks.get(key)
        if state is not None and (not start or state["state"] == "running"):
            return state
        if not start:
            return _view("unavailable", "not_started", "Start extraction to build the local view.")
        python = Path(os.environ.get("JARVISOS_DOCLING_PYTHON", ""))
        artifacts = Path(os.environ.get("JARVISOS_DOCLING_ARTIFACTS", ""))
        if not python.is_file() or not artifacts.is_dir():
            return _view("unavailable", "bundle_missing", "Local Docling interpreter or model bundle is missing.")
        _tasks[key] = _view("running", "running", "Extracting locally.")
        _pool.submit(_convert, path, digest, key, python, artifacts)
        return _tasks[key]
