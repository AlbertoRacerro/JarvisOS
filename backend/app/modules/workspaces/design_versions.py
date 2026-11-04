"""One backend-owned Design version context over existing workspace-scoped engines."""

import shutil
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.events.service import log_event, utc_now
from app.modules.process_stack import draft
from app.modules.process_stack.draft import DraftError

router = APIRouter(prefix="/workspaces/{project_id}/design-versions", tags=["design"])


class VersionCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    expected_active_version_id: str
    # "fork" derives the new version from the active one; "empty" starts a blank data context.
    mode: Literal["fork", "empty"] = "fork"


class VersionActivate(BaseModel):
    version_id: str
    expected_active_version_id: str


class DesignVersion(BaseModel):
    id: str
    project_id: str
    data_workspace_id: str
    parent_version_id: str | None
    label: str
    active: bool
    starts_empty: bool
    source_mode: Literal["fork", "empty"] | None = None
    created_at: str


class DesignVersionSet(BaseModel):
    project_id: str
    active_version_id: str
    versions: list[DesignVersion]


def _project(connection, project_id: str):
    project = connection.execute(
        "SELECT * FROM workspaces WHERE id = ? AND status != 'design_version'", (project_id,)
    ).fetchone()
    if project is None:
        raise HTTPException(404, detail="Project not found.")
    return project


def _fork_content(connection, parent_id: str, drafts: dict[str, dict[str, str]], version_id: str, now: str) -> dict[str, int]:
    """Copy the parent version's rows into the new version by reference to shared artifacts.

    Components get new ids (hierarchy remapped); geometry keeps the same bluecad candidate_id and model
    pins keep their exact model/source/contract identity, so no artifact or candidate is duplicated.
    Process drafts are forked on disk by the caller first; `revisions` maps their source head revision
    to the forked one so geometry staleness evidence stays truthful.
    """
    revisions = {fork["source_revision"]: fork["revision"] for fork in drafts.values()}
    components = connection.execute(
        "SELECT * FROM design_components WHERE version_id = ? ORDER BY created_at, id", (parent_id,)
    ).fetchall()
    ids = {row["id"]: str(uuid4()) for row in components}
    for row in components:
        connection.execute(
            "INSERT INTO design_components (id, project_id, version_id, parent_id, name, revision, created_at, "
            "updated_at) VALUES (?, ?, ?, NULL, ?, 1, ?, ?)",
            (ids[row["id"]], row["project_id"], version_id, row["name"], row["created_at"], now),
        )
    for row in components:
        if row["parent_id"] is not None:
            connection.execute("UPDATE design_components SET parent_id = ? WHERE id = ?",
                               (ids[row["parent_id"]], ids[row["id"]]))
    counts = {"components": len(components), "units": 0, "geometry": 0, "models": 0, "drafts": len(drafts)}
    for old, new in ids.items():
        unit = connection.execute("SELECT draft_id, unit_id FROM design_component_units WHERE component_id = ?",
                                  (old,)).fetchone()
        if unit is not None:
            connection.execute("INSERT INTO design_component_units (component_id, version_id, draft_id, unit_id) "
                               "VALUES (?, ?, ?, ?)", (new, version_id, unit["draft_id"], unit["unit_id"]))
            counts["units"] += 1
        geometry = connection.execute(
            "SELECT candidate_id, source_draft_revision FROM design_component_geometry WHERE component_id = ?",
            (old,)).fetchone()
        if geometry is not None:
            source = geometry["source_draft_revision"]
            connection.execute(
                "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, "
                "updated_at) VALUES (?, ?, ?, ?)",
                (new, geometry["candidate_id"], revisions.get(source, source), now))
            counts["geometry"] += 1
        history = connection.execute(
            "SELECT sequence_json, sequence_digest, compiled_spec_id, candidate_id, revision, updated_by "
            "FROM design_component_feature_history WHERE component_id = ?", (old,),
        ).fetchone()
        if history is not None:
            connection.execute(
                "INSERT INTO design_component_feature_history "
                "(component_id, sequence_json, sequence_digest, compiled_spec_id, candidate_id, revision, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new, history["sequence_json"], history["sequence_digest"], history["compiled_spec_id"],
                 history["candidate_id"], history["revision"], now, history["updated_by"]),
            )
        for pin in connection.execute(
                "SELECT model_version_id, node, category, source_sha256, contract_sha256, attached_at "
                "FROM design_component_models WHERE component_id = ? ORDER BY rowid", (old,)).fetchall():
            connection.execute(
                "INSERT INTO design_component_models (component_id, model_version_id, node, category, "
                "source_sha256, contract_sha256, attached_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new, pin["model_version_id"], pin["node"], pin["category"], pin["source_sha256"],
                 pin["contract_sha256"], pin["attached_at"]))
            counts["models"] += 1
    return counts


def _read(connection, project_id: str) -> DesignVersionSet:
    rows = connection.execute(
        "SELECT * FROM design_project_versions WHERE project_id = ? ORDER BY created_at, id", (project_id,)
    ).fetchall()
    versions = [DesignVersion(
        id=row["id"], project_id=row["project_id"], data_workspace_id=row["data_workspace_id"],
        parent_version_id=row["parent_version_id"], label=row["label"], active=bool(row["active"]),
        # Versions created before source_mode existed started empty; only the initial version has no parent.
        starts_empty=row["source_mode"] == "empty" or (row["source_mode"] is None and row["parent_version_id"] is not None),
        source_mode=row["source_mode"], created_at=row["created_at"],
    ) for row in rows]
    active = next((version.id for version in versions if version.active), None)
    if active is None:
        raise HTTPException(409, detail="Project has no active Design version.")
    return DesignVersionSet(project_id=project_id, active_version_id=active, versions=versions)


@router.get("", response_model=DesignVersionSet)
def list_design_versions(project_id: str) -> DesignVersionSet:
    with open_sqlite_connection() as connection:
        _project(connection, project_id)
        return _read(connection, project_id)


@router.post("", response_model=DesignVersionSet, status_code=201)
def create_design_version(project_id: str, payload: VersionCreate) -> DesignVersionSet:
    label = payload.label.strip()
    if not label:
        raise HTTPException(422, detail="Version label cannot be empty.")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        project = _project(connection, project_id)
        current = _read(connection, project_id)
        if current.active_version_id != payload.expected_active_version_id:
            raise HTTPException(409, detail="Active Design version changed; reload before creating a version.")
        version_id, workspace_id, now = str(uuid4()), str(uuid4()), utc_now()
        parent = connection.execute(
            "SELECT id, data_workspace_id FROM design_project_versions WHERE id = ?", (current.active_version_id,)
        ).fetchone()
        connection.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'design_version', ?, ?)",
            (workspace_id, f"{project['name']} · {label}", f"design-{workspace_id}",
             "Independent Design version data context", now, now),
        )
        connection.execute(
            "INSERT INTO design_project_versions "
            "(id, project_id, data_workspace_id, parent_version_id, label, active, created_at, source_mode) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (version_id, project_id, workspace_id, current.active_version_id, label, now, payload.mode),
        )
        counts = {"components": 0, "units": 0, "geometry": 0, "models": 0, "drafts": 0}
        workspace_dir = build_paths().workspaces_dir / workspace_id
        try:
            if payload.mode == "fork":
                # Draft files go to a directory nothing references until commit; removed again on any failure.
                drafts = draft.fork_drafts(parent["data_workspace_id"], workspace_id)
                counts = _fork_content(connection, parent["id"], drafts, version_id, now)
            log_event(connection, event_type="DesignVersionCreated", actor="local-user",
                      target_type="DesignVersion", target_id=version_id, workspace_id=project_id,
                      payload={"parent_version_id": current.active_version_id, "mode": payload.mode,
                               "starts_empty": payload.mode == "empty", "forked": counts})
            result = _read(connection, project_id)
            connection.commit()
        except DraftError as error:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise HTTPException(error.status, detail=str(error)) from error
        except BaseException:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise
        return result


@router.post("/activate", response_model=DesignVersionSet)
def activate_design_version(project_id: str, payload: VersionActivate) -> DesignVersionSet:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _project(connection, project_id)
        current = _read(connection, project_id)
        if current.active_version_id != payload.expected_active_version_id:
            raise HTTPException(409, detail="Active Design version changed; reload before switching.")
        if not any(version.id == payload.version_id for version in current.versions):
            raise HTTPException(404, detail="Design version not found in this project.")
        if payload.version_id != current.active_version_id:
            connection.execute("UPDATE design_project_versions SET active = 0 WHERE project_id = ? AND active = 1", (project_id,))
            connection.execute("UPDATE design_project_versions SET active = 1 WHERE id = ? AND project_id = ?",
                               (payload.version_id, project_id))
            log_event(connection, event_type="DesignVersionActivated", actor="local-user",
                      target_type="DesignVersion", target_id=payload.version_id, workspace_id=project_id,
                      payload={"previous_version_id": current.active_version_id})
        result = _read(connection, project_id)
        connection.commit()
        return result
