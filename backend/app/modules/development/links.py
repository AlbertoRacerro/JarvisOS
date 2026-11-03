from __future__ import annotations

import sqlite3
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.development.service import DevelopmentError
from app.modules.events.service import log_event, utc_now

_REF_TABLES = {
    "requirement": "requirements",
    "parameter": "parameters",
    "model_spec": "model_specs",
    "simulation_run": "simulation_runs",
    "decision": "decisions",
    "literature_entry": "literature_entries",
}


def _workspace_exists(connection: sqlite3.Connection, workspace_id: str) -> None:
    row = connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise DevelopmentError("workspace_not_found", "Workspace not found.")


def _roadmap_exists(connection: sqlite3.Connection, workspace_id: str, item_id: str) -> None:
    row = connection.execute(
        "SELECT 1 FROM roadmap_items WHERE workspace_id = ? AND id = ?",
        (workspace_id, item_id),
    ).fetchone()
    if row is None:
        raise DevelopmentError("roadmap_item_not_found", "Roadmap item not found.")


def _canonical_ref_exists(
    connection: sqlite3.Connection, workspace_id: str, ref_type: str, ref_id: str
) -> None:
    table = _REF_TABLES.get(ref_type)
    if table is None:
        raise DevelopmentError("roadmap_link_type_invalid", "Roadmap link type is not supported.")
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE workspace_id = ? AND id = ?",
        (workspace_id, ref_id),
    ).fetchone()
    if row is None:
        raise DevelopmentError(
            "roadmap_link_target_not_found",
            "Roadmap link target was not found in the requested workspace.",
        )


def add_object_link(
    workspace_id: str,
    item_id: str,
    ref_type: str,
    ref_id: str,
    actor: str,
) -> dict[str, str]:
    normalized_type = ref_type.strip().lower()
    link_id = str(uuid4())
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        _roadmap_exists(connection, workspace_id, item_id)
        _canonical_ref_exists(connection, workspace_id, normalized_type, ref_id)
        try:
            connection.execute(
                """
                INSERT INTO roadmap_object_links (
                    id, workspace_id, item_id, ref_type, ref_id, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (link_id, workspace_id, item_id, normalized_type, ref_id, actor, utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DevelopmentError("roadmap_link_exists", "Roadmap object link already exists.") from exc
        log_event(
            connection,
            event_type="development.roadmap.link_added",
            actor=actor,
            target_type="roadmap_item",
            target_id=item_id,
            workspace_id=workspace_id,
            payload={"ref_type": normalized_type, "ref_id": ref_id},
        )
        connection.commit()
    return {"id": link_id, "ref_type": normalized_type, "ref_id": ref_id}


def list_object_links(workspace_id: str, item_id: str) -> list[dict[str, str]]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        _roadmap_exists(connection, workspace_id, item_id)
        rows = connection.execute(
            """
            SELECT id, ref_type, ref_id, created_by, created_at
            FROM roadmap_object_links
            WHERE workspace_id = ? AND item_id = ?
            ORDER BY created_at, id
            """,
            (workspace_id, item_id),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "ref_type": str(row["ref_type"]),
                "ref_id": str(row["ref_id"]),
                "created_by": str(row["created_by"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]


def remove_object_link(workspace_id: str, item_id: str, link_id: str, actor: str) -> None:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        _roadmap_exists(connection, workspace_id, item_id)
        row = connection.execute(
            "SELECT ref_type, ref_id FROM roadmap_object_links WHERE workspace_id = ? AND item_id = ? AND id = ?",
            (workspace_id, item_id, link_id),
        ).fetchone()
        if row is None:
            raise DevelopmentError("roadmap_link_not_found", "Roadmap object link not found.")
        connection.execute(
            "DELETE FROM roadmap_object_links WHERE workspace_id = ? AND item_id = ? AND id = ?",
            (workspace_id, item_id, link_id),
        )
        log_event(
            connection,
            event_type="development.roadmap.link_removed",
            actor=actor,
            target_type="roadmap_item",
            target_id=item_id,
            workspace_id=workspace_id,
            payload={"ref_type": str(row["ref_type"]), "ref_id": str(row["ref_id"])},
        )
        connection.commit()
