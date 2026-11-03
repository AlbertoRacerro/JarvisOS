"""Public API for atomic JarvisOS data-root recovery."""

from .common import (
    COMPLETION_MARKER,
    MANIFEST_NAME,
    DataRootError,
    SnapshotResult,
    _canonical_json_bytes,
    _string_mentions_source_root,
    rebase_absolute_path,
    sha256_file,
)
from .restore import restore_snapshot
from .snapshot import create_snapshot, verify_snapshot

__all__ = [
    "COMPLETION_MARKER",
    "MANIFEST_NAME",
    "DataRootError",
    "SnapshotResult",
    "_canonical_json_bytes",
    "_string_mentions_source_root",
    "create_snapshot",
    "rebase_absolute_path",
    "restore_snapshot",
    "sha256_file",
    "verify_snapshot",
]
