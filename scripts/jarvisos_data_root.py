#!/usr/bin/env python3
"""Atomic snapshot, verification, restore and move for the JarvisOS data root."""

from data_root_recovery import (
    COMPLETION_MARKER,
    MANIFEST_NAME,
    DataRootError,
    SnapshotResult,
    create_snapshot,
    move_data_root,
    rebase_absolute_path,
    restore_snapshot,
    sha256_file,
    verify_move,
    verify_snapshot,
)
from data_root_recovery.cli import main
from data_root_recovery.common import (
    _canonical_json_bytes,
    _string_mentions_source_root,
)

__all__ = [
    "COMPLETION_MARKER",
    "MANIFEST_NAME",
    "DataRootError",
    "SnapshotResult",
    "_canonical_json_bytes",
    "_string_mentions_source_root",
    "create_snapshot",
    "main",
    "move_data_root",
    "rebase_absolute_path",
    "restore_snapshot",
    "sha256_file",
    "verify_move",
    "verify_snapshot",
]

if __name__ == "__main__":
    raise SystemExit(main())
