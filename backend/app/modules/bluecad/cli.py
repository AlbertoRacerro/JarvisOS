"""Command-line entry point for BLUECAD builds."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.modules.bluecad.service import build_geometry_spec_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.app.modules.bluecad")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build a GeometrySpec JSON into BLUECAD artifacts")
    build_parser.add_argument("spec_json", type=Path)
    build_parser.add_argument("--out", required=True, type=Path)
    build_parser.add_argument("--timeout-s", default=30.0, type=float)
    args = parser.parse_args(argv)
    if args.command == "build":
        result = build_geometry_spec_file(args.spec_json, args.out, timeout_s=args.timeout_s)
        print(result.report_path)
        if result.errors:
            return 2
        return 0 if result.verdict == "pass" else 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
