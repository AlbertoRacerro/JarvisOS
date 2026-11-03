from __future__ import annotations

import argparse

from app.core.database import initialize_database, open_sqlite_connection

NOW = "2026-09-07T00:00:00+00:00"
WORKSPACE_ID = "proof-workspace"
MODEL_SPEC_ID = "proof-model-spec"
VERSION_A = "proof-version-a"
VERSION_B = "proof-version-b"


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
        connection.execute("DELETE FROM model_versions WHERE workspace_id = ?", (WORKSPACE_ID,))
        connection.execute("DELETE FROM model_specs WHERE workspace_id = ?", (WORKSPACE_ID,))
        connection.commit()


def seed_versions() -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO model_specs
                (id, workspace_id, title, engineering_question, scope, status, maturity_status,
                 assumptions_summary, inputs_summary, outputs_summary, raw_payload, schema_version,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                MODEL_SPEC_ID, WORKSPACE_ID, "Exact version browser proof",
                "Does the browser preserve exact model-version identity?",
                "Isolated proof-only dossier", "active", "validated",
                "Proof assumption summary", "Proof input summary", "Proof output summary",
                None, 1, NOW, NOW,
            ),
        )
        rows = [
            (VERSION_A, "Version A exact", "digest-a", "2026-09-07T00:00:01+00:00"),
            (VERSION_B, "Version B exact", "digest-b", "2026-09-07T00:00:02+00:00"),
        ]
        for version_id, label, digest, created_at in rows:
            connection.execute(
                """
                INSERT INTO model_versions
                    (id, workspace_id, model_spec_id, version_label, implementation_kind, status,
                     input_contract_payload, input_contract_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (version_id, WORKSPACE_ID, MODEL_SPEC_ID, label, "batch_growth_v0", "active", "{}", digest, created_at),
            )
        connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("workspace", "versions"))
    args = parser.parse_args()
    if args.phase == "workspace":
        seed_workspace()
    else:
        seed_versions()


if __name__ == "__main__":
    main()
