from __future__ import annotations

import json
import sqlite3
from datetime import date
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.errors import WORKSPACE_NOT_FOUND_CODE, WORKSPACE_NOT_FOUND_MESSAGE, AppError
from app.modules.development.models import (
    CalendarAllocationCreate,
    CalendarAllocationUpdate,
    RoadmapItemCreate,
    RoadmapItemUpdate,
)
from app.modules.development.time import DevelopmentTimeError, resolve_interval
from app.modules.events.service import log_event, utc_now


class DevelopmentError(AppError):
    pass


def _workspace_exists(connection: sqlite3.Connection, workspace_id: str) -> None:
    row = connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise DevelopmentError(WORKSPACE_NOT_FOUND_CODE, WORKSPACE_NOT_FOUND_MESSAGE)


def _roadmap_row(connection: sqlite3.Connection, workspace_id: str, item_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM roadmap_items WHERE workspace_id = ? AND id = ?",
        (workspace_id, item_id),
    ).fetchone()
    if row is None:
        raise DevelopmentError("roadmap_item_not_found", "Roadmap item not found.")
    return row


def _calendar_row(connection: sqlite3.Connection, workspace_id: str, allocation_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM calendar_allocations WHERE workspace_id = ? AND id = ?",
        (workspace_id, allocation_id),
    ).fetchone()
    if row is None:
        raise DevelopmentError("calendar_allocation_not_found", "Calendar allocation not found.")
    return row


def _validate_window(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise DevelopmentError("roadmap_window_invalid", "Roadmap window end must not precede its start.")


def _validate_done(status: str, done_when: str | None, done_when_satisfied: bool | None) -> None:
    if status == "Done" and done_when and done_when_satisfied is not True:
        raise DevelopmentError(
            "roadmap_done_gate_unsatisfied",
            "Roadmap item cannot transition to Done while its done-when criterion is unsatisfied or unknown.",
        )


def _roadmap_payload(row: sqlite3.Row) -> dict[str, object]:
    value = dict(row)
    value["tags"] = json.loads(str(value.pop("tags_json")))
    value["done_when_satisfied"] = None if value["done_when_satisfied"] is None else bool(value["done_when_satisfied"])
    return value


def _calendar_payload(row: sqlite3.Row) -> dict[str, object]:
    value = dict(row)
    value["tags"] = json.loads(str(value.pop("tags_json")))
    reminder = value.pop("reminder_json")
    value["reminder"] = None if reminder is None else json.loads(str(reminder))
    value["all_day"] = bool(value["all_day"])
    value["deadline"] = bool(value["deadline"])
    return value


def create_roadmap_item(payload: RoadmapItemCreate) -> dict[str, object]:
    _validate_window(payload.window_start_date, payload.window_end_date)
    _validate_done(payload.status, payload.done_when, payload.done_when_satisfied)
    now = utc_now()
    item_id = str(uuid4())
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, payload.workspace_id)
        connection.execute(
            """
            INSERT INTO roadmap_items (
                id, workspace_id, title, description, item_type, status, priority,
                window_start_date, window_end_date, domain, owner, effort_estimate,
                tags_json, notes, cannot_start_before, must_finish_before, done_when,
                done_when_satisfied, created_by, created_at, updated_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                item_id,
                payload.workspace_id,
                payload.title.strip(),
                payload.description,
                payload.item_type,
                payload.status,
                payload.priority,
                payload.window_start_date.isoformat() if payload.window_start_date else None,
                payload.window_end_date.isoformat() if payload.window_end_date else None,
                payload.domain,
                payload.owner,
                payload.effort_estimate,
                json.dumps(payload.tags),
                payload.notes,
                payload.cannot_start_before.isoformat() if payload.cannot_start_before else None,
                payload.must_finish_before.isoformat() if payload.must_finish_before else None,
                payload.done_when,
                None if payload.done_when_satisfied is None else int(payload.done_when_satisfied),
                payload.created_by,
                now,
                now,
            ),
        )
        log_event(
            connection,
            event_type="development.roadmap.created",
            actor=payload.created_by,
            target_type="roadmap_item",
            target_id=item_id,
            workspace_id=payload.workspace_id,
            payload={"revision": 1, "status": payload.status},
        )
        row = _roadmap_row(connection, payload.workspace_id, item_id)
        connection.commit()
        return _roadmap_payload(row)


def list_roadmap_items(workspace_id: str) -> list[dict[str, object]]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM roadmap_items WHERE workspace_id = ? ORDER BY updated_at DESC, id",
            (workspace_id,),
        ).fetchall()
        return [_roadmap_payload(row) for row in rows]


def get_roadmap_item(workspace_id: str, item_id: str) -> dict[str, object]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        return _roadmap_payload(_roadmap_row(connection, workspace_id, item_id))


def list_dependencies(workspace_id: str, item_id: str) -> list[dict[str, str]]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        _roadmap_row(connection, workspace_id, item_id)
        rows = connection.execute(
            """
            SELECT depends_on_item_id, created_by, created_at
            FROM roadmap_dependencies
            WHERE workspace_id = ? AND item_id = ?
            ORDER BY created_at, depends_on_item_id
            """,
            (workspace_id, item_id),
        ).fetchall()
        return [
            {
                "depends_on_item_id": str(row["depends_on_item_id"]),
                "created_by": str(row["created_by"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]


def update_roadmap_item(item_id: str, payload: RoadmapItemUpdate) -> dict[str, object]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, payload.workspace_id)
        current = _roadmap_row(connection, payload.workspace_id, item_id)
        if int(current["revision"]) != payload.expected_revision:
            raise DevelopmentError("roadmap_item_stale", "Roadmap item revision is stale.")

        changes = payload.model_dump(exclude_unset=True)
        changes.pop("workspace_id", None)
        changes.pop("expected_revision", None)
        actor = str(changes.pop("actor"))
        merged = dict(current)
        for key, value in changes.items():
            if key in {"window_start_date", "window_end_date", "cannot_start_before", "must_finish_before"}:
                merged[key] = value.isoformat() if value is not None else None
            elif key == "tags":
                merged["tags_json"] = json.dumps(value)
            elif key == "done_when_satisfied":
                merged[key] = None if value is None else int(bool(value))
            else:
                merged[key] = value
        _validate_window(
            date.fromisoformat(str(merged["window_start_date"])) if merged["window_start_date"] else None,
            date.fromisoformat(str(merged["window_end_date"])) if merged["window_end_date"] else None,
        )
        _validate_done(
            str(merged["status"]),
            None if merged["done_when"] is None else str(merged["done_when"]),
            None if merged["done_when_satisfied"] is None else bool(merged["done_when_satisfied"]),
        )
        now = utc_now()
        result = connection.execute(
            """
            UPDATE roadmap_items SET
                title = ?, description = ?, item_type = ?, status = ?, priority = ?,
                window_start_date = ?, window_end_date = ?, domain = ?, owner = ?, effort_estimate = ?,
                tags_json = ?, notes = ?, cannot_start_before = ?, must_finish_before = ?,
                done_when = ?, done_when_satisfied = ?, updated_at = ?, revision = revision + 1
            WHERE workspace_id = ? AND id = ? AND revision = ?
            """,
            (
                merged["title"], merged["description"], merged["item_type"], merged["status"], merged["priority"],
                merged["window_start_date"], merged["window_end_date"], merged["domain"], merged["owner"],
                merged["effort_estimate"], merged["tags_json"], merged["notes"], merged["cannot_start_before"],
                merged["must_finish_before"], merged["done_when"], merged["done_when_satisfied"], now,
                payload.workspace_id, item_id, payload.expected_revision,
            ),
        )
        if result.rowcount != 1:
            raise DevelopmentError("roadmap_item_stale", "Roadmap item revision is stale.")
        log_event(
            connection,
            event_type="development.roadmap.updated",
            actor=actor,
            target_type="roadmap_item",
            target_id=item_id,
            workspace_id=payload.workspace_id,
            payload={"revision": payload.expected_revision + 1, "fields": sorted(changes)},
        )
        row = _roadmap_row(connection, payload.workspace_id, item_id)
        connection.commit()
        return _roadmap_payload(row)


def add_dependency(workspace_id: str, item_id: str, depends_on_item_id: str, actor: str) -> None:
    if item_id == depends_on_item_id:
        raise DevelopmentError("roadmap_dependency_self", "Roadmap item cannot depend on itself.")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _workspace_exists(connection, workspace_id)
        _roadmap_row(connection, workspace_id, item_id)
        _roadmap_row(connection, workspace_id, depends_on_item_id)
        cycle = connection.execute(
            """
            WITH RECURSIVE reachable(id) AS (
                SELECT depends_on_item_id FROM roadmap_dependencies
                WHERE workspace_id = ? AND item_id = ?
                UNION
                SELECT d.depends_on_item_id FROM roadmap_dependencies d
                JOIN reachable r ON d.item_id = r.id
                WHERE d.workspace_id = ?
            )
            SELECT 1 FROM reachable WHERE id = ? LIMIT 1
            """,
            (workspace_id, depends_on_item_id, workspace_id, item_id),
        ).fetchone()
        if cycle is not None:
            raise DevelopmentError("roadmap_dependency_cycle", "Roadmap dependency would create a cycle.")
        try:
            connection.execute(
                "INSERT INTO roadmap_dependencies (workspace_id, item_id, depends_on_item_id, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (workspace_id, item_id, depends_on_item_id, actor, utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise DevelopmentError("roadmap_dependency_exists", "Roadmap dependency already exists.") from exc
        log_event(
            connection,
            event_type="development.roadmap.dependency_added",
            actor=actor,
            target_type="roadmap_item",
            target_id=item_id,
            workspace_id=workspace_id,
            payload={"depends_on_item_id": depends_on_item_id},
        )
        connection.commit()


def remove_dependency(workspace_id: str, item_id: str, depends_on_item_id: str, actor: str) -> None:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        result = connection.execute(
            "DELETE FROM roadmap_dependencies WHERE workspace_id = ? AND item_id = ? AND depends_on_item_id = ?",
            (workspace_id, item_id, depends_on_item_id),
        )
        if result.rowcount != 1:
            raise DevelopmentError("roadmap_dependency_not_found", "Roadmap dependency not found.")
        log_event(
            connection,
            event_type="development.roadmap.dependency_removed",
            actor=actor,
            target_type="roadmap_item",
            target_id=item_id,
            workspace_id=workspace_id,
            payload={"depends_on_item_id": depends_on_item_id},
        )
        connection.commit()


def delete_roadmap_item(workspace_id: str, item_id: str, expected_revision: int, actor: str) -> None:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        current = _roadmap_row(connection, workspace_id, item_id)
        if int(current["revision"]) != expected_revision:
            raise DevelopmentError("roadmap_item_stale", "Roadmap item revision is stale.")
        blocker = connection.execute(
            """
            SELECT 1 FROM roadmap_dependencies
            WHERE workspace_id = ? AND (item_id = ? OR depends_on_item_id = ?)
            UNION ALL
            SELECT 1 FROM calendar_allocations
            WHERE workspace_id = ? AND roadmap_item_id = ?
            LIMIT 1
            """,
            (workspace_id, item_id, item_id, workspace_id, item_id),
        ).fetchone()
        if blocker is not None:
            raise DevelopmentError(
                "roadmap_item_delete_blocked",
                "Roadmap item still has dependency or Calendar references that must be resolved explicitly.",
            )
        result = connection.execute(
            "DELETE FROM roadmap_items WHERE workspace_id = ? AND id = ? AND revision = ?",
            (workspace_id, item_id, expected_revision),
        )
        if result.rowcount != 1:
            raise DevelopmentError("roadmap_item_stale", "Roadmap item revision is stale.")
        log_event(
            connection,
            event_type="development.roadmap.deleted",
            actor=actor,
            target_type="roadmap_item",
            target_id=item_id,
            workspace_id=workspace_id,
            payload={"revision": expected_revision},
        )
        connection.commit()


def create_calendar_allocation(payload: CalendarAllocationCreate) -> dict[str, object]:
    try:
        start, end = resolve_interval(
            payload.start_local,
            payload.end_local,
            payload.timezone,
            start_utc_offset_minutes=payload.start_utc_offset_minutes,
            end_utc_offset_minutes=payload.end_utc_offset_minutes,
        )
    except DevelopmentTimeError as exc:
        raise DevelopmentError(exc.code, exc.message) from exc
    allocation_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, payload.workspace_id)
        if payload.roadmap_item_id is not None:
            _roadmap_row(connection, payload.workspace_id, payload.roadmap_item_id)
        connection.execute(
            """
            INSERT INTO calendar_allocations (
                id, workspace_id, roadmap_item_id, title, event_type, start_instant, end_instant,
                timezone, all_day, deadline, description, priority, domain, location, meeting_link,
                reminder_json, tags_json, created_by, created_at, updated_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                allocation_id, payload.workspace_id, payload.roadmap_item_id, payload.title.strip(), payload.event_type,
                start.instant_utc.isoformat(), end.instant_utc.isoformat(), payload.timezone, int(payload.all_day),
                int(payload.deadline), payload.description, payload.priority, payload.domain, payload.location,
                payload.meeting_link, None if payload.reminder is None else json.dumps(payload.reminder),
                json.dumps(payload.tags), payload.created_by, now, now,
            ),
        )
        log_event(
            connection,
            event_type="development.calendar.created",
            actor=payload.created_by,
            target_type="calendar_allocation",
            target_id=allocation_id,
            workspace_id=payload.workspace_id,
            payload={"revision": 1, "roadmap_item_id": payload.roadmap_item_id, "timezone": payload.timezone},
        )
        row = _calendar_row(connection, payload.workspace_id, allocation_id)
        connection.commit()
        return _calendar_payload(row)


def list_calendar_allocations(workspace_id: str) -> list[dict[str, object]]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM calendar_allocations WHERE workspace_id = ? ORDER BY start_instant, id",
            (workspace_id,),
        ).fetchall()
        return [_calendar_payload(row) for row in rows]


def get_calendar_allocation(workspace_id: str, allocation_id: str) -> dict[str, object]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        return _calendar_payload(_calendar_row(connection, workspace_id, allocation_id))


def update_calendar_allocation(allocation_id: str, payload: CalendarAllocationUpdate) -> dict[str, object]:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, payload.workspace_id)
        current = _calendar_row(connection, payload.workspace_id, allocation_id)
        if int(current["revision"]) != payload.expected_revision:
            raise DevelopmentError("calendar_allocation_stale", "Calendar allocation revision is stale.")
        changes = payload.model_dump(exclude_unset=True)
        changes.pop("workspace_id", None)
        changes.pop("expected_revision", None)
        actor = str(changes.pop("actor"))
        roadmap_item_id = changes.get("roadmap_item_id", current["roadmap_item_id"])
        if roadmap_item_id is not None:
            _roadmap_row(connection, payload.workspace_id, str(roadmap_item_id))

        timezone = str(changes.get("timezone", current["timezone"]))
        start_local = changes.get("start_local")
        end_local = changes.get("end_local")
        time_fields = {"start_local", "end_local", "timezone", "start_utc_offset_minutes", "end_utc_offset_minutes"}
        if time_fields.intersection(changes):
            if start_local is None or end_local is None:
                raise DevelopmentError(
                    "calendar_time_update_incomplete",
                    "Updating Calendar time requires both local start and end values.",
                )
            try:
                start, end = resolve_interval(
                    start_local,
                    end_local,
                    timezone,
                    start_utc_offset_minutes=changes.get("start_utc_offset_minutes"),
                    end_utc_offset_minutes=changes.get("end_utc_offset_minutes"),
                )
            except DevelopmentTimeError as exc:
                raise DevelopmentError(exc.code, exc.message) from exc
            start_instant, end_instant = start.instant_utc.isoformat(), end.instant_utc.isoformat()
        else:
            start_instant, end_instant = str(current["start_instant"]), str(current["end_instant"])

        merged = dict(current)
        for key, value in changes.items():
            if key in {"start_local", "end_local", "start_utc_offset_minutes", "end_utc_offset_minutes"}:
                continue
            if key == "tags":
                merged["tags_json"] = json.dumps(value)
            elif key == "reminder":
                merged["reminder_json"] = None if value is None else json.dumps(value)
            elif key in {"all_day", "deadline"}:
                merged[key] = int(bool(value))
            else:
                merged[key] = value
        merged["start_instant"] = start_instant
        merged["end_instant"] = end_instant
        merged["timezone"] = timezone
        now = utc_now()
        result = connection.execute(
            """
            UPDATE calendar_allocations SET
                roadmap_item_id = ?, title = ?, event_type = ?, start_instant = ?, end_instant = ?, timezone = ?,
                all_day = ?, deadline = ?, description = ?, priority = ?, domain = ?, location = ?, meeting_link = ?,
                reminder_json = ?, tags_json = ?, updated_at = ?, revision = revision + 1
            WHERE workspace_id = ? AND id = ? AND revision = ?
            """,
            (
                merged["roadmap_item_id"], merged["title"], merged["event_type"], merged["start_instant"],
                merged["end_instant"], merged["timezone"], merged["all_day"], merged["deadline"],
                merged["description"], merged["priority"], merged["domain"], merged["location"],
                merged["meeting_link"], merged["reminder_json"], merged["tags_json"], now,
                payload.workspace_id, allocation_id, payload.expected_revision,
            ),
        )
        if result.rowcount != 1:
            raise DevelopmentError("calendar_allocation_stale", "Calendar allocation revision is stale.")
        log_event(
            connection,
            event_type="development.calendar.updated",
            actor=actor,
            target_type="calendar_allocation",
            target_id=allocation_id,
            workspace_id=payload.workspace_id,
            payload={"revision": payload.expected_revision + 1, "fields": sorted(changes)},
        )
        row = _calendar_row(connection, payload.workspace_id, allocation_id)
        connection.commit()
        return _calendar_payload(row)


def delete_calendar_allocation(workspace_id: str, allocation_id: str, expected_revision: int, actor: str) -> None:
    with open_sqlite_connection() as connection:
        _workspace_exists(connection, workspace_id)
        current = _calendar_row(connection, workspace_id, allocation_id)
        if int(current["revision"]) != expected_revision:
            raise DevelopmentError("calendar_allocation_stale", "Calendar allocation revision is stale.")
        result = connection.execute(
            "DELETE FROM calendar_allocations WHERE workspace_id = ? AND id = ? AND revision = ?",
            (workspace_id, allocation_id, expected_revision),
        )
        if result.rowcount != 1:
            raise DevelopmentError("calendar_allocation_stale", "Calendar allocation revision is stale.")
        log_event(
            connection,
            event_type="development.calendar.deleted",
            actor=actor,
            target_type="calendar_allocation",
            target_id=allocation_id,
            workspace_id=workspace_id,
            payload={"revision": expected_revision},
        )
        connection.commit()
