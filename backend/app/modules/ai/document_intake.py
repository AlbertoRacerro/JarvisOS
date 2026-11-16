"""Operator-owned, bounded local text folder projection for workspace retrieval."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import build_paths
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import SourceLocation, SourceRef
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.workspaces.service import get_workspace

OWNER = "workspace_documents"
MAX_FILES = 256
MAX_FILE_BYTES = 256_000
MAX_PASSAGE_CHARS = 8_000
MAX_PASSAGES = 2_048
MAX_RELATIVE_CHARS = 220
MAX_DIRECTORIES = 256
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class ScanResult:
    documents: tuple[IndexDocument, ...]
    skipped: tuple[dict[str, str], ...]


def _registration_file(workspace_id: str) -> Path:
    return build_paths().workspaces_dir / workspace_id / "documents" / "registration.json"


def _workspace(workspace_id: str) -> None:
    if get_workspace(workspace_id) is None:
        raise ValueError("workspace does not exist")


def registered_folder(workspace_id: str) -> Path | None:
    """Internal only: never serialize this path into retrieval output."""
    file = _registration_file(workspace_id)
    if not file.is_file() or file.is_symlink():
        return None
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
        path = Path(value["folder"])
        return path if path.is_absolute() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def register_folder(workspace_id: str, folder: Path) -> None:
    _workspace(workspace_id)
    if not folder.is_absolute() or folder.is_symlink() or not folder.is_dir():
        raise ValueError("folder must be an existing absolute non-symlink directory")
    path = folder.resolve(strict=True)
    destination = _registration_file(workspace_id)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps({"folder": str(path)}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


def remove_folder(workspace_id: str) -> None:
    _workspace(workspace_id)
    _registration_file(workspace_id).unlink(missing_ok=True)


def _read_file(root: Path, relative: Path) -> bytes | None:
    """Open every component without following links, including on a replacement race."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        descriptor = os.open("/", flags | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for part in root.parts[1:]:
            descriptor = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            descriptors.append(descriptor)
        for part in relative.parts[:-1]:
            descriptor = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(relative.name, flags | os.O_NONBLOCK, dir_fd=descriptor)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        content = os.read(descriptor, MAX_FILE_BYTES + 1)
        return content if len(content) <= MAX_FILE_BYTES else None
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def folder_fingerprint(workspace_id: str) -> str:
    """Bounded no-follow metadata hint; source bytes are read only on sync/resolution."""
    root = registered_folder(workspace_id)
    if root is None:
        return canonical_digest(None)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_DIRECTORY
    descriptors: list[int] = []
    entries: list[object] = []
    try:
        descriptor = os.open("/", flags)
        descriptors.append(descriptor)
        for part in root.parts[1:]:
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        root_fd = descriptor
        count = 0
        directories = 0
        errors: list[str] = []
        file_limit = False
        for directory, dirs, files, dir_fd in os.fwalk(
            ".", topdown=True, follow_symlinks=False, dir_fd=root_fd,
            onerror=lambda error: errors.append(type(error).__name__),
        ):
            directories += 1
            if directories > MAX_DIRECTORIES:
                entries.append(("directory_limit",))
                break
            dirs[:] = sorted(dirs)
            for name in sorted(files):
                if Path(name).suffix.lower() not in (".md", ".txt"):
                    continue
                count += 1
                if count > MAX_FILES:
                    file_limit = True
                    break
                relative = (Path(directory) / name).as_posix()
                try:
                    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    entries.append((relative, info.st_mode, info.st_dev, info.st_ino,
                                    info.st_size, info.st_mtime_ns, info.st_ctime_ns))
                except OSError:
                    entries.append((relative, "unreadable"))
            if file_limit:
                break
        entries.append(("counts", count, directories, errors))
        return canonical_digest((str(root), entries))
    except OSError:
        return canonical_digest((str(root), "missing_or_symlink_root"))
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _passages(text: str, markdown: bool) -> list[tuple[str, str, int, tuple[int, int]]]:
    """Return heading, passage, first line, and heading/chunk ordinals."""
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    heading = "Document"
    lines: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _HEADING.match(line) if markdown else None
        if match:
            if lines:
                sections.append((heading, lines))
            heading, lines = match.group(1).strip()[:180], []
        else:
            lines.append((number, line))
    if lines:
        sections.append((heading, lines))
    output: list[tuple[str, str, int, tuple[int, int]]] = []
    occurrences: dict[str, int] = {}
    for title, section in sections:
        occurrence = occurrences.get(title, 0)
        occurrences[title] = occurrence + 1
        blocks: list[tuple[int, str]] = []
        start = section[0][0]
        current: list[str] = []
        for line_no, line in section:
            if not line.strip():
                if current:
                    blocks.append((start, "\n".join(current).strip()))
                    current = []
            else:
                if not current:
                    start = line_no
                current.append(line)
        if current:
            blocks.append((start, "\n".join(current).strip()))
        passage = ""
        first = 0
        ordinal = 0
        for line_no, block in blocks:
            if len(block) > MAX_PASSAGE_CHARS:
                continue
            candidate = f"{passage}\n\n{block}" if passage else block
            if len(candidate) > MAX_PASSAGE_CHARS:
                output.append((title, passage, first, (occurrence, ordinal)))
                ordinal += 1
                passage = block
                first = line_no
            else:
                passage = candidate
                if not first:
                    first = line_no
        if passage:
            output.append((title, passage, first, (occurrence, ordinal)))
    return output


def extract_passages(text: str, *, markdown: bool) -> list[tuple[str, str, int, tuple[int, int]]]:
    return _passages(text, markdown)


def _file_documents(root: Path, relative: Path, workspace_id: str) -> ScanResult:
    label = relative.as_posix()
    if len(label) > MAX_RELATIVE_CHARS:
        return ScanResult((), ({"path": label[:MAX_RELATIVE_CHARS], "reason": "path_too_long"},))
    path = root / relative
    try:
        info = path.lstat()
    except OSError:
        return ScanResult((), ({"path": label, "reason": "unreadable"},))
    if not stat.S_ISREG(info.st_mode):
        return ScanResult((), ({"path": label, "reason": "symlink_or_special"},))
    if info.st_size > MAX_FILE_BYTES:
        return ScanResult((), ({"path": label, "reason": "file_too_large"},))
    raw = _read_file(root, relative)
    if raw is None:
        return ScanResult((), ({"path": label, "reason": "unreadable_or_changed"},))
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ScanResult((), ({"path": label, "reason": "invalid_utf8"},))
    if "\x00" in content:
        return ScanResult((), ({"path": label, "reason": "binary"},))
    skipped: list[dict[str, str]] = []
    if any(len(block.strip()) > MAX_PASSAGE_CHARS for block in re.split(r"\n\s*\n", content)):
        skipped.append({"path": label, "reason": "passage_too_large"})
    passages = extract_passages(content, markdown=relative.suffix.lower() == ".md")
    if not passages and content.strip():
        skipped.append({"path": label, "reason": "passage_too_large"})
    documents = []
    for heading, passage, start, (occurrence, chunk) in passages:
        digest = canonical_digest((heading, passage))
        heading_key = canonical_digest(heading)
        ref = SourceRef(authority_owner=OWNER, object_type="passage",
                        object_id=f"{label}::h{heading_key}::d{occurrence}::c{chunk}",
                        workspace_id=workspace_id, revision=digest, content_digest=digest,
                        location=SourceLocation(start_line=start, fragment=f"{label}#{heading}"[:256]))
        documents.append(IndexDocument(source_ref=ref, title=f"{label} — {heading}"[:512], text=passage))
    return ScanResult(tuple(documents), tuple(skipped))


def scan_folder(workspace_id: str) -> ScanResult:
    root = registered_folder(workspace_id)
    if root is None:
        return ScanResult((), ())
    skipped: list[dict[str, str]] = []
    docs: list[IndexDocument] = []
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        return ScanResult((), ({"path": ".", "reason": "missing_or_symlink_root"},))
    candidates: list[Path] = []
    overflow_files = 0
    directory_count = 0
    try:
        for directory, dirs, files in os.walk(root, followlinks=False):
            directory_count += 1
            if directory_count > MAX_DIRECTORIES:
                skipped.append({"path": ".", "reason": "directory_limit"})
                break
            relative_dir = Path(directory).relative_to(root)
            kept = []
            for name in sorted(dirs):
                relative = relative_dir / name
                if (Path(directory) / name).is_symlink():
                    skipped.append({"path": relative.as_posix(), "reason": "symlink_directory"})
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(files):
                relative = relative_dir / name
                if relative.suffix.lower() in (".md", ".txt"):
                    if len(candidates) < MAX_FILES:
                        candidates.append(relative)
                    else:
                        overflow_files += 1
    except OSError:
        return ScanResult((), ({"path": ".", "reason": "unreadable_root"},))
    if overflow_files:
        skipped.append({"path": ".", "reason": f"file_limit:{overflow_files}"})
    for relative in sorted(candidates, key=lambda path: path.as_posix()):
        label = relative.as_posix()
        if len(docs) >= MAX_PASSAGES:
            skipped.append({"path": label, "reason": "passage_limit"})
            continue
        file_scan = _file_documents(root, relative, workspace_id)
        skipped.extend(file_scan.skipped)
        for doc in file_scan.documents:
            if len(docs) >= MAX_PASSAGES:
                skipped.append({"path": label, "reason": "passage_limit"})
                break
            docs.append(doc)
    return ScanResult(tuple(docs), tuple(skipped[:MAX_FILES + MAX_PASSAGES]))


def resolve_passage(ref: SourceRef) -> IndexDocument | None:
    if ref.authority_owner != OWNER or ref.object_type != "passage" or ref.workspace_id is None:
        return None
    # Parse the registered relative file identity; the index supplies no trusted path.
    match = re.fullmatch(r"(.+)::hsha256:[0-9a-f]{64}::d\d+::c\d+", ref.object_id)
    if match is None:
        return None
    relative = Path(match.group(1))
    if (relative.is_absolute() or relative.suffix.lower() not in (".md", ".txt")
            or any(part in (".", "..") for part in relative.parts)):
        return None
    root = registered_folder(ref.workspace_id)
    if root is None or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        return None
    return next((doc for doc in _file_documents(root, relative, ref.workspace_id).documents
                 if doc.source_ref.object_id == ref.object_id), None)


def refresh_folder(workspace_id: str) -> dict[str, object]:
    _workspace(workspace_id)
    from app.core.database import open_retrieval_index_connection
    from app.modules.ai.retrieval_index import SQLiteIndexStore
    from app.modules.ai.retrieval_owners import _manifest_key, owner_catch_up

    scan = scan_folder(workspace_id)
    store = SQLiteIndexStore()
    with open_retrieval_index_connection() as db:
        present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_manifest'").fetchone()
        previous = dict(db.execute(
            "SELECT key,content_digest FROM owner_manifest WHERE owner=? AND workspace_id=?",
            (OWNER, workspace_id),
        )) if present else {}
    owner_catch_up(store)
    current = {_manifest_key(doc.source_ref): doc.source_ref.content_digest for doc in scan.documents}
    return {"included": len(scan.documents), "skipped": len(scan.skipped),
            "changed": sum(previous.get(key) != digest for key, digest in current.items()),
            "deleted": len(previous.keys() - current.keys()),
            "reasons": list(scan.skipped)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Local workspace document folder operator command")
    parser.add_argument("action", choices=("register", "inspect", "refresh", "remove"))
    parser.add_argument("workspace_id")
    parser.add_argument("folder", nargs="?", type=Path)
    args = parser.parse_args()
    if args.action == "register":
        if args.folder is None:
            parser.error("register requires an absolute folder")
        register_folder(args.workspace_id, args.folder)
    elif args.folder is not None:
        parser.error("folder is only valid for register")
    if args.action == "remove":
        remove_folder(args.workspace_id)
    _workspace(args.workspace_id)
    result = {"workspace_id": args.workspace_id, "registered": registered_folder(args.workspace_id) is not None}
    if args.action == "inspect":
        scan = scan_folder(args.workspace_id)
        result.update({"included": len(scan.documents), "skipped": len(scan.skipped),
                       "reasons": list(scan.skipped)})
    else:
        result.update(refresh_folder(args.workspace_id))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
