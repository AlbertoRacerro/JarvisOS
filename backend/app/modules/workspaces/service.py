from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.packs import (
    DEFAULT_WORKSPACE_DESCRIPTION,
    default_workspace_name,
    default_workspace_slug,
)
from app.core.repository import optional_row_to_model, row_to_model, rows_to_models
from app.modules.events.service import log_event, utc_now
from app.modules.workspaces.models import WorkspaceCreate, WorkspaceRead


def default_workspace_payload() -> WorkspaceCreate:
    """Identity of the default workspace: generic, or taken from the loaded pack."""
    return WorkspaceCreate(
        name=default_workspace_name(),
        slug=default_workspace_slug(),
        description=DEFAULT_WORKSPACE_DESCRIPTION,
        status="active",
    )


def create_workspace(payload: WorkspaceCreate, *, actor: str = "local-user") -> WorkspaceRead:
    now = utc_now()
    workspace_id = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO workspaces (
                id, name, slug, description, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                payload.name,
                payload.slug,
                payload.description,
                payload.status,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO design_project_versions "
            "(id, project_id, data_workspace_id, parent_version_id, label, active, created_at) "
            "VALUES (?, ?, ?, NULL, 'v1', 1, ?)",
            (workspace_id, workspace_id, workspace_id, now),
        )
        log_event(
            connection,
            event_type="WorkspaceCreated",
            actor=actor,
            target_type="Workspace",
            target_id=workspace_id,
            workspace_id=workspace_id,
            payload={"slug": payload.slug},
        )
        connection.commit()
        row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return row_to_model(row, WorkspaceRead)


def list_workspaces(*, include_archived: bool = False) -> list[WorkspaceRead]:
    hidden = ("design_version",) if include_archived else ("design_version", "archived")
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            f"SELECT * FROM workspaces WHERE status NOT IN ({', '.join('?' for _ in hidden)}) ORDER BY created_at ASC",
            hidden,
        ).fetchall()
    return rows_to_models(rows, WorkspaceRead)


class WorkspaceStatusRefused(Exception):
    """Archiving or restoring a workspace was refused; status mirrors the HTTP route."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def set_workspace_archived(workspace_id: str, archived: bool, *, actor: str = "local-user") -> WorkspaceRead:
    """Archive hides a project from the project list; its data stays and restore brings it back."""
    target, source = ("archived", "active") if archived else ("active", "archived")
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT status FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None or row["status"] not in (source, target):
            raise WorkspaceStatusRefused(404, "Workspace not found.")
        if row["status"] != target:
            if archived:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM workspaces WHERE status = 'active' AND id != ?", (workspace_id,)
                ).fetchone()[0]
                if remaining == 0:
                    raise WorkspaceStatusRefused(409, "The last active project cannot be archived.")
            connection.execute(
                "UPDATE workspaces SET status = ?, updated_at = ? WHERE id = ?", (target, utc_now(), workspace_id)
            )
            log_event(
                connection,
                event_type="WorkspaceArchived" if archived else "WorkspaceRestored",
                actor=actor,
                target_type="Workspace",
                target_id=workspace_id,
                workspace_id=workspace_id,
            )
            connection.commit()
        updated = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return row_to_model(updated, WorkspaceRead)


def rename_workspace(workspace_id: str, name: str, *, actor: str = "local-user") -> WorkspaceRead:
    """Only the display name changes; the id and slug that other records reference stay the same."""
    name = name.strip()
    if not name:
        raise WorkspaceStatusRefused(422, "A project name is required.")
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT name, status FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None or row["status"] == "design_version":
            raise WorkspaceStatusRefused(404, "Workspace not found.")
        if row["name"] != name:
            connection.execute(
                "UPDATE workspaces SET name = ?, updated_at = ? WHERE id = ?", (name, utc_now(), workspace_id)
            )
            log_event(
                connection,
                event_type="WorkspaceRenamed",
                actor=actor,
                target_type="Workspace",
                target_id=workspace_id,
                workspace_id=workspace_id,
                payload={"from": row["name"], "to": name},
            )
            connection.commit()
        updated = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return row_to_model(updated, WorkspaceRead)


def get_workspace(workspace_id: str) -> WorkspaceRead | None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return optional_row_to_model(row, WorkspaceRead)


def get_workspace_by_slug(slug: str) -> WorkspaceRead | None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    return optional_row_to_model(row, WorkspaceRead)


def seed_default_workspace() -> WorkspaceRead:
    """Create the default workspace on an empty database; never add a second one to existing data."""
    default = default_workspace_payload()
    existing = get_workspace_by_slug(default.slug) or _first_workspace()
    if existing:
        return existing

    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO workspaces (
                id, name, slug, description, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (default.slug, default.name, default.slug, default.description, default.status, now, now),
        )
        connection.execute(
            "INSERT INTO design_project_versions "
            "(id, project_id, data_workspace_id, parent_version_id, label, active, created_at) "
            "VALUES (?, ?, ?, NULL, 'v1', 1, ?)",
            (default.slug, default.slug, default.slug, now),
        )
        log_event(
            connection,
            event_type="WorkspaceCreated",
            actor="system",
            target_type="Workspace",
            target_id=default.slug,
            workspace_id=default.slug,
            payload={"seed": "default"},
        )
        connection.commit()

    seeded = get_workspace(default.slug)
    if seeded is None:
        raise RuntimeError("Default workspace seed failed.")
    return seeded


def _first_workspace() -> WorkspaceRead | None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM workspaces ORDER BY created_at, id LIMIT 1").fetchone()
    return optional_row_to_model(row, WorkspaceRead)


def get_default_workspace() -> WorkspaceRead:
    default = default_workspace_payload()
    existing = get_workspace_by_slug(default.slug)
    if existing:
        return existing
    return WorkspaceRead(
        id=default.slug,
        name=default.name,
        slug=default.slug,
        description=default.description,
        status="not_initialized",
        created_at="",
        updated_at="",
    )
