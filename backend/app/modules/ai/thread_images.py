"""Read back the screenshots a Bubu turn retained, for explicit operator-approved reuse (Relay escalation)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.ai.thread_ui_context import _IMAGE_EXTENSION, _IMAGE_MAGIC, MAX_IMAGE_BYTES, MAX_IMAGES

_EXTENSION_MAGIC = {_IMAGE_EXTENSION[mime]: magic for mime, magic in _IMAGE_MAGIC.items()}


@dataclass(frozen=True)
class RetainedScreenshot:
    digest: str  # "sha256:<hex>" of the stored bytes
    extension: str  # "png" or "jpg"
    data: bytes


def retained_screenshots(workspace_id: str, thread_id: str, interaction_id: str) -> list[RetainedScreenshot]:
    """Screenshots retained for one interaction of this thread, deduplicated, in submission order.

    Only files that still sit under the workspace's ``bubu_images`` folder, are regular files, still match
    their recorded hash and still look like the image type they claim are returned; anything else is
    skipped silently so a missing file never blocks an escalation.
    """
    with open_sqlite_connection() as connection:
        extras = connection.execute(
            """SELECT extras.image_artifact_ids_json FROM ai_thread_turn_extras AS extras
               JOIN ai_thread_interactions AS interaction ON interaction.id = extras.interaction_id
               JOIN ai_threads AS thread ON thread.id = interaction.thread_id
               WHERE extras.interaction_id = ? AND interaction.thread_id = ? AND thread.workspace_id = ?""",
            (interaction_id, thread_id, workspace_id),
        ).fetchone()
        if extras is None or not extras["image_artifact_ids_json"]:
            return []
        try:
            artifact_ids = json.loads(extras["image_artifact_ids_json"])
        except ValueError:
            return []
        if not isinstance(artifact_ids, list):
            return []
        rows = []
        for artifact_id in artifact_ids:
            if not isinstance(artifact_id, str):
                continue
            row = connection.execute(
                "SELECT stored_path, sha256 FROM artifacts "
                "WHERE id = ? AND workspace_id = ? AND artifact_type = 'bubu_screenshot'",
                (artifact_id, workspace_id),
            ).fetchone()
            if row is not None:
                rows.append(row)
    root = build_paths().workspaces_dir / workspace_id / "bubu_images"
    found: dict[str, RetainedScreenshot] = {}
    for row in rows:
        shot = _read_screenshot(Path(str(row["stored_path"])), root, row["sha256"])
        if shot is not None and shot.digest not in found:
            found[shot.digest] = shot
    return list(found.values())[:MAX_IMAGES]


def _read_screenshot(path: Path, root: Path, recorded_sha256: str | None) -> RetainedScreenshot | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if not path.resolve().is_relative_to(root.resolve()):
            return None
        extension = path.suffix.lstrip(".").lower()
        magic = _EXTENSION_MAGIC.get(extension)
        if magic is None or path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > MAX_IMAGE_BYTES or not data.startswith(magic):
        return None
    digest = hashlib.sha256(data).hexdigest()
    if recorded_sha256 and recorded_sha256 != digest:
        return None
    return RetainedScreenshot(digest=f"sha256:{digest}", extension=extension, data=data)
