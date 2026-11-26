#!/usr/bin/env python3
"""Refuse paths that touch the private vault named by JARVISOS_VAULT_PATH (spec 377).

Standard library only. A path touches the vault when its real path is the vault, lies inside it, or contains it.
An unset variable guards nothing; a set one always guards, even when the directory is missing. Messages never
print the vault path.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

VAULT_PATH_ENV = "JARVISOS_VAULT_PATH"
REFUSED_EXIT = 3
REFUSED_MESSAGE = "vault_guard: refused: path touches the private vault"


def vault_root(env: Mapping[str, str] | None = None) -> Path | None:
    value = (os.environ if env is None else env).get(VAULT_PATH_ENV, "").strip()
    if not value:
        return None
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(value))))


def touches_vault(path: str | os.PathLike[str], vault: Path | None) -> bool:
    if vault is None:
        return False
    real = Path(os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(path)))))
    return real == vault or vault in real.parents or real in vault.parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vault_guard.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="exit 3 if any path touches the vault")
    check.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    vault = vault_root()
    if any(touches_vault(path, vault) for path in args.paths):
        print(REFUSED_MESSAGE, file=sys.stderr)
        return REFUSED_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
