from __future__ import annotations

import argparse
from hashlib import sha256

from app.core.database import initialize_database, open_sqlite_connection
from app.core.paths import build_paths

NOW = "2026-09-11T00:00:00+00:00"
WORKSPACE_ID = "proof-workspace"
SOURCE_A = "proof-literature-source-a"
SOURCE_B = "proof-literature-source-b"
ARTIFACT_A = "proof-literature-artifact-a"
ARTIFACT_B = "proof-literature-artifact-b"
ENTRY_A = "proof-literature-entry-a"
ENTRY_B = "proof-literature-entry-b"


def seed_workspace() -> None:
    initialize_database()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO workspaces
                (id, name, slug, description, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (WORKSPACE_ID, "Browser Proof Workspace", "browser-proof", "isolated browser proof data", "active", NOW, NOW),
        )
        connection.execute("DELETE FROM literature_entries WHERE workspace_id = ?", (WORKSPACE_ID,))
        connection.execute("DELETE FROM literature_sources WHERE workspace_id = ?", (WORKSPACE_ID,))
        connection.execute("DELETE FROM artifacts WHERE id IN (?, ?)", (ARTIFACT_A, ARTIFACT_B))
        connection.commit()


def _insert_artifact(connection, *, artifact_id: str, filename: str, payload: bytes) -> None:
    path = build_paths().artifacts_dir / "literature" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    connection.execute(
        """
        INSERT INTO artifacts
            (id, workspace_id, filename, stored_path, artifact_type, mime_type,
             sha256, source_ref, status, created_at, notes)
        VALUES (?, ?, ?, ?, 'literature_source', 'text/plain', ?, ?, 'registered', ?, ?)
        """,
        (
            artifact_id,
            WORKSPACE_ID,
            filename,
            str(path),
            sha256(payload).hexdigest(),
            "browser-proof:literature",
            NOW,
            "trusted spec-114 browser fixture",
        ),
    )


def seed_sources() -> None:
    payload_a = b"Reactor hydrodynamics\nHeat transfer coefficient increases with superficial velocity.\nMethods\n"
    payload_b = b"Operating point\nSuperficial liquid velocity: 0.42 m/s\nResults\n"
    with open_sqlite_connection() as connection:
        _insert_artifact(connection, artifact_id=ARTIFACT_A, filename="proof-literature-a.txt", payload=payload_a)
        _insert_artifact(connection, artifact_id=ARTIFACT_B, filename="proof-literature-b.txt", payload=payload_b)
        connection.execute(
            """
            INSERT INTO literature_sources
                (id, workspace_id, title, source_kind, state, artifact_id, citation,
                 publisher, published_year, request_key, created_at, updated_at)
            VALUES (?, ?, ?, 'paper', 'accepted', ?, ?, ?, 2026, ?, ?, ?)
            """,
            (
                SOURCE_A,
                WORKSPACE_ID,
                "Browser proof literature A",
                ARTIFACT_A,
                "Proof et al. (2026), Reactor hydrodynamics",
                "Proof Publisher",
                "proof-source-a",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO literature_sources
                (id, workspace_id, title, source_kind, state, artifact_id, citation,
                 publisher, published_year, request_key, created_at, updated_at)
            VALUES (?, ?, ?, 'report', 'review', ?, ?, ?, 2026, ?, ?, ?)
            """,
            (
                SOURCE_B,
                WORKSPACE_ID,
                "Browser proof literature B",
                ARTIFACT_B,
                "Proof Lab (2026), Operating point",
                "Proof Lab",
                "proof-source-b",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO literature_entries
                (id, workspace_id, source_id, entry_kind, statement, value_text, value_number,
                 unit, status, locator_kind, locator_start, locator_end, context_text,
                 request_key, created_at, updated_at)
            VALUES (?, ?, ?, 'claim', ?, NULL, NULL, NULL, 'accepted', 'line', 2, 2, ?, ?, ?, ?)
            """,
            (
                ENTRY_A,
                WORKSPACE_ID,
                SOURCE_A,
                "Heat transfer coefficient increases with superficial velocity.",
                "Trusted browser fixture claim context.",
                "proof-entry-a",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO literature_entries
                (id, workspace_id, source_id, entry_kind, statement, value_text, value_number,
                 unit, status, locator_kind, locator_start, locator_end, context_text,
                 request_key, created_at, updated_at)
            VALUES (?, ?, ?, 'datum', NULL, NULL, 0.42, 'm/s', 'review', 'line', 2, 2, ?, ?, ?, ?)
            """,
            (
                ENTRY_B,
                WORKSPACE_ID,
                SOURCE_B,
                "Trusted browser fixture datum context.",
                "proof-entry-b",
                NOW,
                NOW,
            ),
        )
        connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("workspace", "sources"))
    args = parser.parse_args()
    if args.phase == "workspace":
        seed_workspace()
    else:
        seed_sources()


if __name__ == "__main__":
    main()
