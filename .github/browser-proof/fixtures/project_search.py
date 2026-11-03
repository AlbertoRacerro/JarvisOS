from __future__ import annotations

import argparse
from hashlib import sha256

from app.core.database import initialize_database, open_sqlite_connection
from app.core.paths import build_paths
from app.modules.modeling.models import RequirementCreate
from app.modules.modeling.service import create_requirement

NOW = "2026-09-11T00:00:00+00:00"
WORKSPACE_ID = "proof-workspace"
MODEL_SPEC_ID = "proof-search-model"
MODEL_VERSION_ID = "proof-search-version"
SOURCE_ID = "proof-search-source"
ENTRY_ID = "proof-search-entry"
ARTIFACT_ID = "proof-search-artifact"


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
        connection.execute("DELETE FROM model_versions WHERE workspace_id = ?", (WORKSPACE_ID,))
        connection.execute("DELETE FROM model_specs WHERE workspace_id = ?", (WORKSPACE_ID,))
        connection.execute("DELETE FROM requirements WHERE workspace_id = ?", (WORKSPACE_ID,))
        connection.execute("DELETE FROM artifacts WHERE id = ?", (ARTIFACT_ID,))
        connection.commit()


def seed_records() -> None:
    create_requirement(
        WORKSPACE_ID,
        RequirementCreate(
            statement="Reactor search requirement",
            rationale="Browser proof Project Basis result",
            status="active",
        ),
    )
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
                MODEL_SPEC_ID, WORKSPACE_ID, "Reactor search model",
                "Can the reactor search preserve exact model identity?",
                "Browser proof search scope", "active", "validated",
                "Proof assumption", "Proof input", "Proof output", None, 1, NOW, NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO model_versions
                (id, workspace_id, model_spec_id, version_label, implementation_kind, status,
                 input_contract_payload, input_contract_sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (MODEL_VERSION_ID, WORKSPACE_ID, MODEL_SPEC_ID, "Search version exact", "batch_growth_v0", "active", "{}", "proof-search-digest", NOW),
        )

        payload = b"Reactor search literature\nReactor search claim for browser proof.\n"
        path = build_paths().artifacts_dir / "literature" / "proof-search-literature.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        connection.execute(
            """
            INSERT INTO artifacts
                (id, workspace_id, filename, stored_path, artifact_type, mime_type,
                 sha256, source_ref, status, created_at, notes)
            VALUES (?, ?, ?, ?, 'literature_source', 'text/plain', ?, ?, 'registered', ?, ?)
            """,
            (ARTIFACT_ID, WORKSPACE_ID, "proof-search-literature.txt", str(path), sha256(payload).hexdigest(), "browser-proof:project-search", NOW, "trusted spec-115 browser fixture"),
        )
        connection.execute(
            """
            INSERT INTO literature_sources
                (id, workspace_id, title, source_kind, state, artifact_id, citation,
                 publisher, published_year, request_key, created_at, updated_at)
            VALUES (?, ?, ?, 'paper', 'accepted', ?, ?, ?, 2026, ?, ?, ?)
            """,
            (SOURCE_ID, WORKSPACE_ID, "Reactor search literature", ARTIFACT_ID, "Proof et al. (2026), Reactor search", "Proof Publisher", "proof-search-source", NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO literature_entries
                (id, workspace_id, source_id, entry_kind, statement, value_text, value_number,
                 unit, status, locator_kind, locator_start, locator_end, context_text,
                 request_key, created_at, updated_at)
            VALUES (?, ?, ?, 'claim', ?, NULL, NULL, NULL, 'accepted', 'line', 2, 2, ?, ?, ?, ?)
            """,
            (ENTRY_ID, WORKSPACE_ID, SOURCE_ID, "Reactor search claim for browser proof.", "Trusted Project Search browser fixture context.", "proof-search-entry", NOW, NOW),
        )
        connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("workspace", "records"))
    args = parser.parse_args()
    if args.phase == "workspace":
        seed_workspace()
    else:
        seed_records()


if __name__ == "__main__":
    main()
