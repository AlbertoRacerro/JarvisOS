from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.process_stack.editor import _directory, _write_revision

WORKSPACE_ID = "proof-dwsim-editor"
CASE_ID = "00000000-0149-4000-8000-000000000001"
NOW = "2026-09-25T00:00:00+00:00"


def seed_workspace() -> None:
    initialize_database()
    with open_sqlite_connection() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO workspaces
            (id, name, slug, description, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (WORKSPACE_ID, "DWSIM Browser Proof", "proof-dwsim-editor", "Unavailable-state browser proof", "active", NOW, NOW),
        )
        connection.commit()


def seed_case() -> None:
    # The empty native file is only a lookup key so the real editor endpoint can
    # reach its configured-runtime check. It contains no process graph or results.
    source = Path(tempfile.gettempdir()) / f"{CASE_ID}.dwxml"
    source.write_text("<?xml version=\"1.0\" encoding=\"utf-8\"?><Flowsheet />\n", encoding="utf-8")
    directory = _directory(WORKSPACE_ID, CASE_ID)
    _write_revision(
        directory,
        source,
        command="browser_proof_lookup_only",
        parent=None,
        readback={"purpose": "reach configured DWSIM availability check; no graph fixture"},
        version="unavailable-state-fixture",
        mcp_sha="unavailable-state-fixture",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("workspace", "case"))
    phase = parser.parse_args().phase
    if phase == "workspace":
        seed_workspace()
    else:
        seed_case()


if __name__ == "__main__":
    main()
