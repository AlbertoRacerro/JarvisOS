from __future__ import annotations

import sqlite3

from app.core.database import open_sqlite_connection
from app.modules.project_knowledge.models import RevisionStateCommand, WorkingRevisionRead
from app.modules.project_knowledge.service import ProjectKnowledgeError, get_revision


def _require_working_revision(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    revision_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ?",
        (revision_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ProjectKnowledgeError("revision_not_found", "Working revision was not found in workspace.")
    if row["state"] != "working":
        raise ProjectKnowledgeError("revision_not_working", "Revision is no longer an active working revision.")
    return row


def change_revision_state(revision_id: str, payload: RevisionStateCommand) -> WorkingRevisionRead:
    """Apply an explicit bounded terminal transition without rewriting revision history."""
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            source = _require_working_revision(
                connection,
                workspace_id=payload.workspace_id,
                revision_id=revision_id,
            )
            if payload.action == "discard":
                if payload.superseded_by_revision_id is not None:
                    raise ProjectKnowledgeError(
                        "discard_successor_forbidden",
                        "Discard does not accept a successor revision.",
                    )
                cursor = connection.execute(
                    """
                    UPDATE project_knowledge_revisions
                    SET state = 'discarded'
                    WHERE id = ? AND workspace_id = ? AND state = 'working'
                    """,
                    (revision_id, payload.workspace_id),
                )
            else:
                successor_id = payload.superseded_by_revision_id
                if not successor_id or successor_id == revision_id:
                    raise ProjectKnowledgeError(
                        "supersede_successor_invalid",
                        "Supersede requires a distinct accepted successor revision.",
                    )
                successor = _require_working_revision(
                    connection,
                    workspace_id=payload.workspace_id,
                    revision_id=successor_id,
                )
                if successor["parent_kind"] != "working" or successor["parent_revision_id"] != source["id"]:
                    raise ProjectKnowledgeError(
                        "supersede_successor_not_direct",
                        "Supersede requires a direct accepted working successor.",
                    )
                cursor = connection.execute(
                    """
                    UPDATE project_knowledge_revisions
                    SET state = 'superseded', superseded_by_revision_id = ?
                    WHERE id = ? AND workspace_id = ? AND state = 'working'
                    """,
                    (successor_id, revision_id, payload.workspace_id),
                )
            if cursor.rowcount != 1:
                raise ProjectKnowledgeError("revision_stale", "Working revision changed before commit.")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return get_revision(payload.workspace_id, revision_id)
