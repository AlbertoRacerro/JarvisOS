"""SHA-256 and path-confinement checks for FEM verification fixtures."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.modules.bluecad.fem_verification_common import FemVerificationError

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def verify_fixture_index(index_path: str | Path) -> dict[str, Any]:
    """Verify path confinement and SHA-256 bindings for checked-in fixtures."""

    path = Path(index_path)
    root = path.resolve().parent
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FemVerificationError(
            "FIXTURE_INDEX_INVALID", {"path": str(path)}
        ) from exc
    entries = payload.get("fixtures")
    if not isinstance(entries, list) or not entries:
        raise FemVerificationError("FIXTURE_INDEX_EMPTY", {})
    verified: list[dict[str, Any]] = []
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise FemVerificationError("FIXTURE_ENTRY_INVALID", {})
        name = str(entry.get("name", ""))
        if not name or name in names:
            raise FemVerificationError("FIXTURE_NAME_INVALID", {"name": name})
        names.add(name)
        files = entry.get("files")
        if not isinstance(files, dict) or set(files) != {"step", "manifest"}:
            raise FemVerificationError("FIXTURE_FILES_INVALID", {"name": name})
        checked_files: dict[str, dict[str, Any]] = {}
        for role in ("step", "manifest"):
            file_entry = files[role]
            if not isinstance(file_entry, dict):
                raise FemVerificationError(
                    "FIXTURE_FILE_ENTRY_INVALID", {"name": name, "role": role}
                )
            relative = Path(str(file_entry.get("path", "")))
            digest = str(file_entry.get("sha256", ""))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise FemVerificationError(
                    "FIXTURE_PATH_UNSAFE",
                    {"name": name, "role": role, "path": str(relative)},
                )
            if not _HEX64_RE.fullmatch(digest):
                raise FemVerificationError(
                    "FIXTURE_DIGEST_INVALID", {"name": name, "role": role}
                )
            resolved = (root / relative).resolve()
            if root != resolved and root not in resolved.parents:
                raise FemVerificationError(
                    "FIXTURE_PATH_ESCAPE", {"name": name, "role": role}
                )
            if not resolved.is_file():
                raise FemVerificationError(
                    "FIXTURE_FILE_MISSING",
                    {"name": name, "role": role, "path": str(relative)},
                )
            actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if actual != digest:
                raise FemVerificationError(
                    "FIXTURE_DIGEST_MISMATCH",
                    {
                        "name": name,
                        "role": role,
                        "expected": digest,
                        "actual": actual,
                    },
                )
            checked_files[role] = {
                "path": relative.as_posix(),
                "sha256": actual,
                "bytes": resolved.stat().st_size,
            }
        verified.append({"name": name, "files": checked_files})
    return {
        "schema_version": payload.get("schema_version"),
        "generator_version": payload.get("generator_version"),
        "fixtures": verified,
    }
