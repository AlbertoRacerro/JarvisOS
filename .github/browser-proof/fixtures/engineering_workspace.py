from __future__ import annotations

import argparse

from app.core.database import initialize_database
from app.modules.workspaces.service import seed_default_workspace


def seed_workspace() -> None:
    initialize_database()
    seed_default_workspace()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("workspace",))
    parser.parse_args()
    seed_workspace()


if __name__ == "__main__":
    main()
