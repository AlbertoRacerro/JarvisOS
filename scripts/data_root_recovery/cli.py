"""Command-line interface for JarvisOS data-root recovery."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from .common import DataRootError, _resolved_path
from .restore import restore_snapshot
from .snapshot import create_snapshot, verify_snapshot


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    snapshot = subparsers.add_parser(
        "snapshot", help="create an atomic verified snapshot"
    )
    snapshot.add_argument("--source-root", type=Path)
    snapshot.add_argument("--destination", type=Path, required=True)
    snapshot.add_argument("--database-filename")
    snapshot.add_argument("--snapshot-id")
    snapshot.add_argument("--keep-last", type=int)
    verify = subparsers.add_parser("verify", help="verify a complete snapshot")
    verify.add_argument("snapshot_dir", type=Path)
    restore = subparsers.add_parser("restore", help="restore a verified snapshot")
    restore.add_argument("snapshot_dir", type=Path)
    restore.add_argument("--target-root", type=Path, required=True)
    restore.add_argument("--allow-nonempty-target", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.operation == "snapshot":
            result = create_snapshot(
                source_root=args.source_root,
                destination=args.destination,
                database_filename=args.database_filename,
                snapshot_id=args.snapshot_id,
                keep_last=args.keep_last,
            )
            output: object = result.as_json()
        elif args.operation == "verify":
            output = {
                "snapshot_dir": str(_resolved_path(args.snapshot_dir)),
                "manifest": verify_snapshot(args.snapshot_dir),
            }
        else:
            output = {
                "target_root": str(
                    restore_snapshot(
                        snapshot_dir=args.snapshot_dir,
                        target_root=args.target_root,
                        allow_nonempty_target=args.allow_nonempty_target,
                    )
                )
            }
        print(json.dumps(output, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    except (DataRootError, OSError, sqlite3.Error) as exc:
        print(f"jarvisos-data-root: FAIL: {exc}", file=sys.stderr)
        return 1
