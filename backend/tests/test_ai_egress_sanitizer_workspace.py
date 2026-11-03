from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.egress_sanitizer import create_prompt_derivative
from app.modules.ai.egress_service import sha256_text
from app.modules.events.service import utc_now

CONFIG_DIGEST = sha256_text("sanitizer-config-v1")


def test_identical_prompt_derivatives_are_isolated_by_workspace():
    initialize_database()
    now = utc_now()
    with open_sqlite_connection() as connection:
        for workspace_id in ("workspace-a", "workspace-b"):
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, name, slug, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, 'active', ?, ?)
                """,
                (workspace_id, workspace_id, workspace_id, now, now),
            )
        connection.commit()

    first = create_prompt_derivative(
        raw_prompt="private project question",
        derivative_content="Generic engineering question.",
        final_level="S1",
        transformations=["Removed project identity"],
        sanitizer_kind="deterministic",
        sanitizer_version="prompt-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        workspace_id="workspace-a",
    )
    second = create_prompt_derivative(
        raw_prompt="private project question",
        derivative_content="Generic engineering question.",
        final_level="S1",
        transformations=["Removed project identity"],
        sanitizer_kind="deterministic",
        sanitizer_version="prompt-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        workspace_id="workspace-b",
    )

    assert first.derivative_id != second.derivative_id
    assert first.workspace_id == "workspace-a"
    assert second.workspace_id == "workspace-b"
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            """
            SELECT workspace_id, COUNT(*) AS count
            FROM egress_prompt_derivatives
            GROUP BY workspace_id ORDER BY workspace_id
            """
        ).fetchall()
    assert [(row["workspace_id"], row["count"]) for row in rows] == [
        ("workspace-a", 1),
        ("workspace-b", 1),
    ]
