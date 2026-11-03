"""Version-scoped Design component tree with optimistic edits."""

import hashlib
import json
import math
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.core.database import open_sqlite_connection
from app.modules.bluecad.cfd_pipe import case_runs
from app.modules.bluecad.cfd_pipe.casegen import MAX_PROCESSORS
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract, Regime
from app.modules.bluecad.cfd_pipe.domain import FluidDomain, FluidDomainError, derive_fluid_domain
from app.modules.bluecad.cfd_pipe.reduced_model import (
    PublicationRefused,
    ReducedModel,
    Tolerances,
    obsolescence,
    publish,
    study_template_digest,
)
from app.modules.bluecad.cfd_pipe.studio_publication import publish_to_studio, reduced_model_of_source
from app.modules.bluecad.cfd_pipe.study_from_runs import publication_inputs
from app.modules.bluecad.ledger import get_candidate_from_connection
from app.modules.bluecad.registry import ToolRegistryError, resolve_tool
from app.modules.bluecad.spec import canonicalize_geometry_spec
from app.modules.bluecad.template import (
    SERPENTINE_MAX_SEGMENTS,
    SerpentineTemplateCreate,
    SerpentineTemplateParams,
    TemplateError,
    TubeTemplateCreate,
    TubeTemplateParams,
    create_template_candidate,
)
from app.modules.events.service import log_event, utc_now
from app.modules.process_stack import draft
from app.modules.process_stack.draft import DraftError
from app.modules.process_stack.draft_models import SetUnitHydraulics, UnitHydraulicsPin
from app.modules.studio.service import category_of_kind, published_source

router = APIRouter(
    prefix="/workspaces/{project_id}/design-versions/{version_id}/components",
    tags=["design"],
)


REDUCED_MODEL_FIDELITY = "CFD reduced model (scientifically unqualified)"


class ComponentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None
    expected_active_version_id: str


class ComponentEdit(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None
    expected_revision: int = Field(ge=1)
    expected_active_version_id: str


class AttachedModel(BaseModel):
    model_version_id: str
    node: str
    category: str
    source_sha256: str
    contract_sha256: str
    attached_at: str


class ComponentRead(BaseModel):
    id: str
    project_id: str
    version_id: str
    parent_id: str | None
    name: str
    revision: int
    created_at: str
    updated_at: str
    source_draft_id: str | None = None
    source_unit_id: str | None = None
    geometry_candidate_id: str | None = None
    geometry_source_draft_revision: str | None = None
    # Workspace that owns the attached candidate; differs from the version's data workspace when the
    # geometry was inherited through a version fork (candidates and artifacts are shared, not copied).
    geometry_workspace_id: str | None = None
    models: list[AttachedModel] = Field(default_factory=list)


class ModelAttachment(BaseModel):
    model_version_id: str
    expected_revision: int = Field(ge=1)
    expected_active_version_id: str


# Studio categories that belong to a component node; process units and procedures attach to Plant
# through their existing Process owners instead.
COMPONENT_NODE_OF_CATEGORY = {"calculation": "Physics", "geometry": "Geometry"}
_PLANT_CATEGORIES = {"process_unit": "a Process unit model", "procedure": "an operating procedure"}


class ReducedModelPublication(BaseModel):
    """Names server-recorded runs only; measured values are never accepted from the caller."""

    model_config = {"extra": "forbid"}
    model_id: str
    run_ids: list[str]
    mesh_study_run_ids: dict[Regime, list[str]] = Field(default_factory=dict)
    tolerances: Tolerances
    expected_revision: int = Field(ge=1)
    expected_active_version_id: str


class StudyTemplateSet(BaseModel):
    model_config = {"extra": "forbid"}
    contract: PipeStudyContract
    expected_revision: int = Field(ge=1)
    expected_active_version_id: str


class StudyTemplateRead(BaseModel):
    regime: str
    template_digest: str
    updated_at: str
    contract: dict


class CfdToolStatus(BaseModel):
    tool_id: str
    binary_verified: bool
    version_pin: str | None = None
    reason: str | None = None


class CfdToolchainStatus(BaseModel):
    binary_verified: bool
    tools: list[CfdToolStatus]


class ReducedModelProcessPin(BaseModel):
    model_config = {"extra": "forbid"}
    expected_active_version_id: str
    expected_revision: int = Field(ge=1)
    expected_draft_revision: str
    out_of_range: Literal["refuse", "warn"] = "refuse"


class UnitEntry(BaseModel):
    draft_id: str
    unit_id: str
    expected_draft_revision: str
    expected_active_version_id: str


class GeometryAttachment(BaseModel):
    candidate_id: str
    expected_revision: int = Field(ge=1)
    expected_active_version_id: str
    expected_draft_revision: str | None = None


class ProcessGeometryHandoff(BaseModel):
    wall_t_mm: float = Field(gt=0)
    expected_revision: int = Field(ge=1)
    expected_active_version_id: str
    expected_draft_revision: str


SINGLE_RUN_LIMIT_MM = 5000.0  # BlueCad single-solid tube limit; longer loops become serpentines
MIN_LOOP_BEND_RADIUS_MM = 100.0


class ProcessGeometryMapping(BaseModel):
    supported: bool
    unit_type: str
    template: str | None = None
    inner_d_mm: float | None = None
    length_mm: float | None = None
    segment_count: int | None = None
    segment_length_mm: float | None = None
    bend_radius_mm: float | None = None
    basis: str
    draft_revision: str


class ReducedSegmentStatus(BaseModel):
    regime: str
    reynolds_min: float
    reynolds_max: float
    diameter_m: float
    density_kg_m3: float
    dynamic_viscosity_pa_s: float
    turbulence_model: str | None
    wall_treatment: str
    combined_uncertainty: float
    reference: str


class ReducedModelStatus(BaseModel):
    model_version_id: str
    reduced_model_id: str | None = None
    reduced_model_version_id: str | None = None
    node: str
    attached_at: str
    fidelity: str = REDUCED_MODEL_FIDELITY
    status: str
    segments: list[ReducedSegmentStatus] = Field(default_factory=list)
    geometry_digest: str | None = None
    current_geometry_digest: str | None = None
    obsolete_reasons: list[str] = Field(default_factory=list)
    process_unit_pinned: bool = False


def _si(unit: dict, key: str) -> float | None:
    value = (unit.get("params") or {}).get(key)
    return float(value["si"]) if isinstance(value, dict) and value.get("si") is not None else None


def _tube_mapping(unit: dict, draft_revision: str) -> ProcessGeometryMapping:
    """Map typed Process unit parameters onto the BlueCad tube template, or say why not."""

    kind = unit.get("type", "")
    inner = length = None
    if kind == "PhotobioreactorT1":
        inner, length = _si(unit, "tube_inner_diameter"), _si(unit, "tube_length")
        basis = "One representative PBR tube from tube_inner_diameter and tube_length; tube_count and layout are not modelled."
    elif kind == "PFR":
        volume, length = _si(unit, "volume"), _si(unit, "length")
        inner = math.sqrt(4 * volume / (math.pi * length)) if volume and length else None
        basis = "PFR as one straight tube; inner diameter derived as sqrt(4·volume/(π·length))."
    else:
        return ProcessGeometryMapping(
            supported=False, unit_type=kind, draft_revision=draft_revision,
            basis=f"No parametric BlueCad geometry mapping exists for Process unit type {kind or 'unknown'} yet.",
        )
    if not inner or not length:
        return ProcessGeometryMapping(
            supported=False, unit_type=kind, draft_revision=draft_revision,
            basis="The Process unit is missing the typed dimensions this mapping needs.",
        )
    inner_mm, length_mm = inner * 1000, length * 1000
    if length_mm <= SINGLE_RUN_LIMIT_MM:
        return ProcessGeometryMapping(
            supported=True, unit_type=kind, template="tube", inner_d_mm=inner_mm, length_mm=length_mm,
            basis=basis, draft_revision=draft_revision,
        )
    # Long loop: fewest runs of at most the single-solid limit, joined by U-bends, so that
    # N*segment + (N-1)*pi*R equals the Process length. R = max(2 * inner Ø, 100 mm).
    bend_mm = max(2 * inner_mm, MIN_LOOP_BEND_RADIUS_MM)
    arc_mm = math.pi * bend_mm
    count = math.ceil((length_mm + arc_mm) / (SINGLE_RUN_LIMIT_MM + arc_mm))
    if count > SERPENTINE_MAX_SEGMENTS:
        return ProcessGeometryMapping(
            supported=False, unit_type=kind, draft_revision=draft_revision, length_mm=length_mm,
            basis=f"The {length_mm / 1000:g} m tube would need {count} runs; the BlueCad serpentine template is limited to "
                  f"{SERPENTINE_MAX_SEGMENTS} runs of {SINGLE_RUN_LIMIT_MM:g} mm.",
        )
    return ProcessGeometryMapping(
        supported=True, unit_type=kind, template="serpentine", inner_d_mm=inner_mm, length_mm=length_mm,
        segment_count=count, segment_length_mm=(length_mm - (count - 1) * arc_mm) / count, bend_radius_mm=bend_mm,
        basis=f"{basis} The {length_mm / 1000:g} m loop exceeds the {SINGLE_RUN_LIMIT_MM:g} mm single-solid limit, so it is "
              f"represented as a serpentine of {count} runs + {count - 1} U-bends (centreline bend radius "
              f"{bend_mm:g} mm = max(2 × inner Ø, {MIN_LOOP_BEND_RADIUS_MM:g} mm)) with total centreline length preserved.",
        draft_revision=draft_revision,
    )


def _process_unit(connection, project_id: str, version_id: str, component_id: str) -> tuple[str, dict, str]:
    _component(connection, project_id, version_id, component_id)
    source = connection.execute(
        "SELECT draft_id, unit_id FROM design_component_units WHERE component_id = ?", (component_id,)
    ).fetchone()
    if source is None:
        raise HTTPException(422, detail="This component has no Process unit source.")
    version = connection.execute(
        "SELECT data_workspace_id FROM design_project_versions WHERE id = ? AND project_id = ?",
        (version_id, project_id),
    ).fetchone()
    try:
        process = draft.projection(version["data_workspace_id"], source["draft_id"])
    except DraftError as error:
        raise HTTPException(error.status, detail=str(error)) from error
    unit = next((item for item in process["objects"] if item["id"] == source["unit_id"]
                 and item["kind"] == "unit"), None)
    if unit is None:
        raise HTTPException(404, detail="Process unit not found in this version.")
    return version["data_workspace_id"], unit, process["revision"]


def _read_component(connection, row) -> ComponentRead:
    source = connection.execute(
        "SELECT draft_id, unit_id FROM design_component_units WHERE component_id = ?", (row["id"],)
    ).fetchone()
    geometry = connection.execute(
        "SELECT g.candidate_id, g.source_draft_revision, c.workspace_id FROM design_component_geometry g "
        "LEFT JOIN bluecad_candidates c ON c.id = g.candidate_id WHERE g.component_id = ?", (row["id"],)
    ).fetchone()
    return ComponentRead(**dict(row), source_draft_id=source["draft_id"] if source else None,
                         source_unit_id=source["unit_id"] if source else None,
                         geometry_candidate_id=geometry["candidate_id"] if geometry else None,
                         geometry_source_draft_revision=geometry["source_draft_revision"] if geometry else None,
                         geometry_workspace_id=geometry["workspace_id"] if geometry else None,
                         models=[AttachedModel(**dict(item)) for item in connection.execute(
                             "SELECT model_version_id, node, category, source_sha256, contract_sha256, attached_at "
                             "FROM design_component_models WHERE component_id = ? ORDER BY attached_at, rowid",
                             (row["id"],)).fetchall()])


def _active_version(connection, project_id: str, version_id: str, expected: str) -> None:
    row = connection.execute(
        "SELECT id FROM design_project_versions WHERE project_id = ? AND active = 1",
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail="Project not found.")
    if row["id"] != expected or row["id"] != version_id:
        raise HTTPException(409, detail="Active Design version changed; reload before editing.")


def _component(connection, project_id: str, version_id: str, component_id: str):
    row = connection.execute(
        "SELECT * FROM design_components WHERE id = ? AND project_id = ? AND version_id = ?",
        (component_id, project_id, version_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail="Component not found in this Design version.")
    return row


def _parent(connection, project_id: str, version_id: str, parent_id: str | None) -> None:
    if parent_id is not None:
        _component(connection, project_id, version_id, parent_id)


@router.get("", response_model=list[ComponentRead])
def list_design_components(project_id: str, version_id: str) -> list[ComponentRead]:
    with open_sqlite_connection() as connection:
        version = connection.execute(
            "SELECT id FROM design_project_versions WHERE project_id = ? AND id = ?",
            (project_id, version_id),
        ).fetchone()
        if version is None:
            raise HTTPException(404, detail="Design version not found in this project.")
        rows = connection.execute(
            "SELECT * FROM design_components WHERE project_id = ? AND version_id = ? ORDER BY created_at, id",
            (project_id, version_id),
        ).fetchall()
        return [_read_component(connection, row) for row in rows]


@router.post("/from-unit", response_model=ComponentRead)
def open_unit_component(project_id: str, version_id: str, payload: UnitEntry) -> ComponentRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        version = connection.execute(
            "SELECT data_workspace_id FROM design_project_versions WHERE id = ? AND project_id = ?",
            (version_id, project_id),
        ).fetchone()
        try:
            process = draft.projection(version["data_workspace_id"], payload.draft_id)
        except DraftError as error:
            raise HTTPException(error.status, detail=str(error)) from error
        if process["revision"] != payload.expected_draft_revision:
            raise HTTPException(409, detail="Process draft changed; reload before opening its unit.")
        unit = next((item for item in process["objects"] if item["id"] == payload.unit_id
                     and item["kind"] == "unit"), None)
        if unit is None:
            raise HTTPException(404, detail="Process unit not found in this version.")
        existing = connection.execute(
            "SELECT c.* FROM design_components c JOIN design_component_units u ON u.component_id = c.id "
            "WHERE u.version_id = ? AND u.draft_id = ? AND u.unit_id = ?",
            (version_id, payload.draft_id, payload.unit_id),
        ).fetchone()
        if existing:
            return _read_component(connection, existing)
        component_id, now = str(uuid4()), utc_now()
        connection.execute(
            "INSERT INTO design_components "
            "(id, project_id, version_id, parent_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, 1, ?, ?)",
            (component_id, project_id, version_id, unit["tag"], now, now),
        )
        connection.execute(
            "INSERT INTO design_component_units (component_id, version_id, draft_id, unit_id) "
            "VALUES (?, ?, ?, ?)", (component_id, version_id, payload.draft_id, payload.unit_id),
        )
        log_event(connection, event_type="DesignComponentOpenedFromProcess", actor="local-user",
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "draft_id": payload.draft_id,
                           "unit_id": payload.unit_id, "draft_revision": process["revision"]})
        row = _component(connection, project_id, version_id, component_id)
        connection.commit()
        return _read_component(connection, row)


@router.post("", response_model=ComponentRead, status_code=201)
def create_design_component(project_id: str, version_id: str, payload: ComponentCreate) -> ComponentRead:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, detail="Component name cannot be empty.")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        _parent(connection, project_id, version_id, payload.parent_id)
        component_id, now = str(uuid4()), utc_now()
        connection.execute(
            "INSERT INTO design_components "
            "(id, project_id, version_id, parent_id, name, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (component_id, project_id, version_id, payload.parent_id, name, now, now),
        )
        log_event(connection, event_type="DesignComponentCreated", actor="local-user",
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "parent_id": payload.parent_id})
        row = _component(connection, project_id, version_id, component_id)
        connection.commit()
        return _read_component(connection, row)


@router.put("/{component_id}", response_model=ComponentRead)
def edit_design_component(
    project_id: str, version_id: str, component_id: str, payload: ComponentEdit,
) -> ComponentRead:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, detail="Component name cannot be empty.")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        current = _component(connection, project_id, version_id, component_id)
        if current["revision"] != payload.expected_revision:
            raise HTTPException(409, detail="Component changed; reload before editing.")
        _parent(connection, project_id, version_id, payload.parent_id)
        ancestor = payload.parent_id
        while ancestor is not None:
            if ancestor == component_id:
                raise HTTPException(409, detail="Component nesting would create a cycle.")
            ancestor = _component(connection, project_id, version_id, ancestor)["parent_id"]
        connection.execute(
            "UPDATE design_components SET name = ?, parent_id = ?, revision = revision + 1, "
            "updated_at = ? WHERE id = ?",
            (name, payload.parent_id, utc_now(), component_id),
        )
        log_event(connection, event_type="DesignComponentEdited", actor="local-user",
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "revision": payload.expected_revision + 1,
                           "parent_id": payload.parent_id})
        row = _component(connection, project_id, version_id, component_id)
        connection.commit()
        return _read_component(connection, row)


@router.put("/{component_id}/geometry", response_model=ComponentRead)
def attach_component_geometry(
    project_id: str, version_id: str, component_id: str, payload: GeometryAttachment,
) -> ComponentRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        current = _component(connection, project_id, version_id, component_id)
        if current["revision"] != payload.expected_revision:
            raise HTTPException(409, detail="Component changed; reload before attaching geometry.")
        version = connection.execute(
            "SELECT data_workspace_id FROM design_project_versions WHERE id = ? AND project_id = ?",
            (version_id, project_id),
        ).fetchone()
        candidate = connection.execute(
            "SELECT id, status FROM bluecad_candidates WHERE id = ? AND workspace_id = ?",
            (payload.candidate_id, version["data_workspace_id"]),
        ).fetchone()
        if candidate is None:
            raise HTTPException(404, detail="BlueCad candidate not found in this Design version.")
        if candidate["status"] != "valid":
            raise HTTPException(409, detail="Only a validated BlueCad candidate can be attached.")
        source = connection.execute(
            "SELECT draft_id FROM design_component_units WHERE component_id = ?", (component_id,)
        ).fetchone()
        if source:
            if payload.expected_draft_revision is None:
                raise HTTPException(422, detail="Process draft revision is required for this component.")
            try:
                process = draft.projection(version["data_workspace_id"], source["draft_id"])
            except DraftError as error:
                raise HTTPException(error.status, detail=str(error)) from error
            if process["revision"] != payload.expected_draft_revision:
                raise HTTPException(409, detail="Process draft changed; reload before attaching geometry.")
        elif payload.expected_draft_revision is not None:
            raise HTTPException(422, detail="This component has no Process draft source.")
        now = utc_now()
        connection.execute(
            "INSERT INTO design_component_geometry (component_id, candidate_id, source_draft_revision, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(component_id) DO UPDATE SET candidate_id = excluded.candidate_id, "
            "source_draft_revision = excluded.source_draft_revision, updated_at = excluded.updated_at",
            (component_id, payload.candidate_id, payload.expected_draft_revision, now),
        )
        connection.execute(
            "UPDATE design_components SET revision = revision + 1, updated_at = ? WHERE id = ?", (now, component_id),
        )
        log_event(connection, event_type="DesignComponentGeometryAttached", actor="local-user",
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "candidate_id": payload.candidate_id,
                           "source_draft_revision": payload.expected_draft_revision})
        row = _component(connection, project_id, version_id, component_id)
        connection.commit()
        return _read_component(connection, row)


def _pin_model(connection, project_id: str, version_id: str, component_id: str, model_version_id: str) -> tuple[str, str]:
    """Insert the pin of a published Studio version on its component node and bump the revision; (node, source sha)."""
    published = connection.execute(
        "SELECT mv.implementation_kind, sp.source_sha256, sp.contract_sha256 FROM studio_published_versions sp "
        "JOIN model_versions mv ON mv.id = sp.version_id JOIN design_project_versions v "
        "ON v.data_workspace_id = mv.workspace_id WHERE sp.version_id = ? AND v.id = ? AND v.project_id = ?",
        (model_version_id, version_id, project_id),
    ).fetchone()
    if published is None:
        raise HTTPException(404, detail="Published Studio model not found in this Design version.")
    category = category_of_kind(published["implementation_kind"])
    node = COMPONENT_NODE_OF_CATEGORY.get(category)
    if node is None:
        raise HTTPException(422, detail=f"This Studio model is {_PLANT_CATEGORIES.get(category, category)}; "
                            "it attaches to Plant through Process, not to a component node.")
    if connection.execute(
        "SELECT 1 FROM design_component_models WHERE component_id = ? AND model_version_id = ?",
        (component_id, model_version_id),
    ).fetchone():
        raise HTTPException(409, detail="This Studio model version is already attached to the component.")
    now = utc_now()
    connection.execute(
        "INSERT INTO design_component_models (component_id, model_version_id, node, category, source_sha256, "
        "contract_sha256, attached_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (component_id, model_version_id, node, category, published["source_sha256"],
         published["contract_sha256"], now),
    )
    connection.execute(
        "UPDATE design_components SET revision = revision + 1, updated_at = ? WHERE id = ?", (now, component_id),
    )
    return node, published["source_sha256"]


@router.put("/{component_id}/models", response_model=ComponentRead)
def attach_component_model(
    project_id: str, version_id: str, component_id: str, payload: ModelAttachment,
) -> ComponentRead:
    """Pin a published Studio version from this project version to its compatible component node."""

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        if _component(connection, project_id, version_id, component_id)["revision"] != payload.expected_revision:
            raise HTTPException(409, detail="Component changed; reload before attaching a model.")
        node, source_sha256 = _pin_model(connection, project_id, version_id, component_id, payload.model_version_id)
        log_event(connection, event_type="DesignComponentModelAttached", actor="local-user",
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "model_version_id": payload.model_version_id,
                           "node": node, "source_sha256": source_sha256})
        row = _component(connection, project_id, version_id, component_id)
        connection.commit()
        return _read_component(connection, row)


@router.get("/{component_id}/geometry/process-mapping", response_model=ProcessGeometryMapping)
def process_geometry_mapping(project_id: str, version_id: str, component_id: str) -> ProcessGeometryMapping:
    with open_sqlite_connection() as connection:
        _, unit, revision = _process_unit(connection, project_id, version_id, component_id)
    return _tube_mapping(unit, revision)


@router.post("/{component_id}/geometry/from-process", response_model=ComponentRead)
def deepen_component_geometry(
    project_id: str, version_id: str, component_id: str, payload: ProcessGeometryHandoff,
) -> ComponentRead:
    """Build a template candidate from typed Process dimensions, then attach it through the guarded owner."""

    with open_sqlite_connection() as connection:
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        if _component(connection, project_id, version_id, component_id)["revision"] != payload.expected_revision:
            raise HTTPException(409, detail="Component changed; reload before generating geometry.")
        workspace_id, unit, revision = _process_unit(connection, project_id, version_id, component_id)
    if revision != payload.expected_draft_revision:
        raise HTTPException(409, detail="Process draft changed; reload before generating geometry.")
    mapping = _tube_mapping(unit, revision)
    if not mapping.supported or mapping.inner_d_mm is None or mapping.length_mm is None:
        raise HTTPException(422, detail=mapping.basis)
    outer_mm = mapping.inner_d_mm + 2 * payload.wall_t_mm
    try:
        request: SerpentineTemplateCreate | TubeTemplateCreate
        if mapping.template == "serpentine":
            if mapping.segment_length_mm is None or mapping.segment_count is None or mapping.bend_radius_mm is None:
                raise HTTPException(422, detail=mapping.basis)
            request = SerpentineTemplateCreate(template="serpentine", params=SerpentineTemplateParams(
                outer_d_mm=outer_mm, wall_t_mm=payload.wall_t_mm, segment_length_mm=mapping.segment_length_mm,
                segment_count=mapping.segment_count, bend_radius_mm=mapping.bend_radius_mm,
            ))
        else:
            request = TubeTemplateCreate(template="tube", params=TubeTemplateParams(
                outer_d_mm=outer_mm, wall_t_mm=payload.wall_t_mm, length_mm=mapping.length_mm,
            ))
    except ValidationError as error:
        raise HTTPException(422, detail="Process dimensions are outside the BlueCad tube template range: "
                            + "; ".join(item["msg"] for item in error.errors())) from error
    try:
        candidate = create_template_candidate(workspace_id, request)
    except TemplateError as error:
        raise HTTPException(error.status_code, detail=error.message) from error
    if candidate.status != "valid":
        raise HTTPException(409, detail=f"BlueCad candidate {candidate.id} did not validate; it was not attached.")
    return attach_component_geometry(project_id, version_id, component_id, GeometryAttachment(
        candidate_id=candidate.id, expected_revision=payload.expected_revision,
        expected_active_version_id=payload.expected_active_version_id,
        expected_draft_revision=payload.expected_draft_revision,
    ))


def _current_fluid_domain(connection, workspace_id: str, component_id: str) -> tuple[FluidDomain | None, str | None]:
    """Fluid domain of the attached candidate and, when it cannot be derived, why."""
    attached = connection.execute(
        "SELECT candidate_id FROM design_component_geometry WHERE component_id = ?", (component_id,)
    ).fetchone()
    if attached is None:
        return None, "component has no attached geometry"
    candidate = get_candidate_from_connection(connection, workspace_id, attached["candidate_id"])
    artifact = candidate and candidate.spec_artifact_id and connection.execute(
        "SELECT stored_path FROM artifacts WHERE id = ? AND workspace_id = ? AND status = 'registered'",
        (candidate.spec_artifact_id, workspace_id),
    ).fetchone()
    if not artifact or not Path(artifact["stored_path"]).is_file():
        return None, "attached geometry has no readable GeometrySpec"
    try:
        spec = canonicalize_geometry_spec(json.loads(Path(artifact["stored_path"]).read_text(encoding="utf-8")))
        return derive_fluid_domain(spec), None
    except FluidDomainError as error:
        return None, f"attached geometry is not a single smooth tube bore: {error}"
    except ValueError as error:
        return None, f"attached GeometrySpec is invalid: {error}"


def _current_geometry_digest(connection, workspace_id: str, component_id: str) -> tuple[str | None, str | None]:
    """Fluid-domain digest of the attached candidate and, when unknown, why."""
    domain, why = _current_fluid_domain(connection, workspace_id, component_id)
    return (domain.geometry_digest if domain else None), why


@router.get("/{component_id}/fluid-domain", response_model=FluidDomain)
def component_fluid_domain(project_id: str, version_id: str, component_id: str) -> FluidDomain:
    """Read-only fluid domain derived from the component's attached geometry; the source for study contracts."""
    with open_sqlite_connection() as connection:
        _component(connection, project_id, version_id, component_id)
        domain, why = _current_fluid_domain(connection, _data_workspace_id(connection, project_id, version_id),
                                            component_id)
    if domain is None:
        raise HTTPException(409, detail=f"Component tube geometry is unknown ({why}).")
    return domain


def _study_templates(connection, component_id: str) -> dict[str, str]:
    """Recorded current study template digest per regime; a regime without a record is not input-checked."""
    return {row["regime"]: row["template_digest"] for row in connection.execute(
        "SELECT regime, template_digest FROM design_component_study_templates WHERE component_id = ?",
        (component_id,)).fetchall()}


def _template_read(row) -> StudyTemplateRead:
    return StudyTemplateRead(regime=row["regime"], template_digest=row["template_digest"],
                             updated_at=row["updated_at"], contract=json.loads(row["contract_json"]))


@router.get("/{component_id}/cfd-toolchain", response_model=CfdToolchainStatus)
def component_cfd_toolchain(project_id: str, version_id: str, component_id: str) -> CfdToolchainStatus:
    """Report verified binaries for the serial smooth-tube study without attempting a run."""
    with open_sqlite_connection() as connection:
        _component(connection, project_id, version_id, component_id)
    tools = []
    for tool_id in ("openfoam_blockmesh", "openfoam_checkmesh", "openfoam_simplefoam"):
        try:
            entry = resolve_tool(tool_id)
        except ToolRegistryError as error:
            tools.append(CfdToolStatus(tool_id=tool_id, binary_verified=False, reason=error.code))
        except OSError:
            tools.append(CfdToolStatus(tool_id=tool_id, binary_verified=False, reason="TOOL_ACCESS_ERROR"))
        else:
            tools.append(CfdToolStatus(tool_id=tool_id, binary_verified=True, version_pin=entry["version_pin"]))
    return CfdToolchainStatus(binary_verified=all(tool.binary_verified for tool in tools), tools=tools)


@router.get("/{component_id}/cfd-study-templates", response_model=list[StudyTemplateRead])
def list_component_study_templates(project_id: str, version_id: str, component_id: str) -> list[StudyTemplateRead]:
    with open_sqlite_connection() as connection:
        _component(connection, project_id, version_id, component_id)
        return [_template_read(row) for row in connection.execute(
            "SELECT * FROM design_component_study_templates WHERE component_id = ? ORDER BY regime",
            (component_id,)).fetchall()]


def record_study_template(
    project_id: str, version_id: str, component_id: str, contract: PipeStudyContract, *,
    expected_revision: int, expected_active_version_id: str, actor: str,
) -> StudyTemplateRead:
    """The one typed write of a component CFD study template (GUI route and agent Apply share it)."""
    regime = contract.regime
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_version(connection, project_id, version_id, expected_active_version_id)
        current = _component(connection, project_id, version_id, component_id)
        if current["revision"] != expected_revision:
            raise HTTPException(409, detail="Component changed; reload before setting the study template.")
        domain, why = _current_fluid_domain(connection, _data_workspace_id(connection, project_id, version_id),
                                            component_id)
        if domain is None:
            raise HTTPException(409, detail=f"Component tube geometry is unknown ({why}); no study template was recorded.")
        if contract.domain != domain:
            raise HTTPException(422, detail="The study contract fluid domain differs from this component's current "
                                "tube geometry; no study template was recorded.")
        template = study_template_digest(contract)
        now = utc_now()
        connection.execute(
            "INSERT INTO design_component_study_templates (component_id, regime, contract_json, template_digest, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(component_id, regime) DO UPDATE "
            "SET contract_json = excluded.contract_json, template_digest = excluded.template_digest, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (component_id, regime, json.dumps(contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                                              allow_nan=False), template, now, actor))
        connection.execute(
            "UPDATE design_components SET revision = revision + 1, updated_at = ? WHERE id = ?", (now, component_id))
        log_event(connection, event_type="DesignComponentStudyTemplateSet", actor=actor,
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "regime": regime, "template_digest": template,
                           "geometry_digest": domain.geometry_digest, "revision": expected_revision + 1})
        row = connection.execute(
            "SELECT * FROM design_component_study_templates WHERE component_id = ? AND regime = ?",
            (component_id, regime)).fetchone()
        connection.commit()
        return _template_read(row)


@router.put("/{component_id}/cfd-study-templates/{regime}", response_model=StudyTemplateRead)
def set_component_study_template(
    project_id: str, version_id: str, component_id: str, regime: str, payload: StudyTemplateSet,
) -> StudyTemplateRead:
    """Record the component's current CFD study inputs for one regime; reduced models fitted on others go obsolete."""
    if payload.contract.regime != regime:
        raise HTTPException(422, detail=f"Study contract regime {payload.contract.regime} does not match path regime {regime}.")
    return record_study_template(project_id, version_id, component_id, payload.contract,
                                 expected_revision=payload.expected_revision,
                                 expected_active_version_id=payload.expected_active_version_id, actor="local-user")


class StudyTemplateRefused(Exception):
    """A Design-owned refusal to resolve an agent-proposed CFD study template."""


def resolve_process_study_template(workspace_id: str, draft_id: str, unit_id: str, regime: str,
                                   inputs: dict) -> tuple[PipeStudyContract, dict]:
    """Read-only: the full validated study contract for a Process unit's linked Design component.

    The fluid domain always comes from the component's current tube geometry, never from the caller.
    """
    with open_sqlite_connection() as connection:
        link = connection.execute(
            "SELECT u.component_id, c.project_id, u.version_id, c.revision FROM design_component_units u "
            "JOIN design_components c ON c.id = u.component_id "
            "JOIN design_project_versions v ON v.id = u.version_id "
            "WHERE u.draft_id = ? AND u.unit_id = ? AND v.data_workspace_id = ? ORDER BY v.created_at DESC, u.rowid DESC",
            (draft_id, unit_id, workspace_id)).fetchone()
        if link is None:
            raise StudyTemplateRefused("This Process unit has no linked Design component.")
        domain, why = _current_fluid_domain(connection, workspace_id, link["component_id"])
        active = connection.execute(
            "SELECT id FROM design_project_versions WHERE project_id = ? AND active = 1", (link["project_id"],)).fetchone()
        previous = _study_templates(connection, link["component_id"]).get(regime)
    if domain is None:
        raise StudyTemplateRefused(f"The component's tube geometry is unknown ({why}).")
    if active is None or active["id"] != link["version_id"]:
        raise StudyTemplateRefused("The linked Design version is not the active version.")
    try:
        contract = PipeStudyContract.model_validate({**inputs, "regime": regime, "domain": domain.model_dump(mode="json")})
    except ValidationError as error:
        raise StudyTemplateRefused("; ".join(
            (".".join(str(part) for part in item["loc"]) + ": " if item["loc"] else "")
            + str(item["msg"]).removeprefix("Value error, ") for item in error.errors()[:4])) from error
    contract_digest = "sha256:" + hashlib.sha256(json.dumps(
        contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return contract, {
        "design_component_id": link["component_id"], "design_project_id": link["project_id"],
        "design_version_id": link["version_id"], "component_revision": link["revision"],
        "active_version_id": active["id"], "template_digest": study_template_digest(contract),
        "contract_digest": contract_digest, "geometry_digest": domain.geometry_digest,
        "previous_template_digest": previous}


class CfdRunStart(BaseModel):
    model_config = {"extra": "forbid"}
    regime: Regime
    retry_of: str | None = None
    processors: int = Field(default=1, ge=1, le=MAX_PROCESSORS, strict=True)  # MPI ranks; not part of the contract


def _run_failure(error: case_runs.CaseRunError) -> HTTPException:
    return HTTPException(error.status, detail=error.message)


@router.post("/{component_id}/cfd-runs", status_code=202)
def start_component_cfd_run(project_id: str, version_id: str, component_id: str, payload: CfdRunStart) -> dict:
    """Start a background pipe-case run from the saved study template of one regime; evidence only."""
    with open_sqlite_connection() as connection:
        _component(connection, project_id, version_id, component_id)
        workspace_id = _data_workspace_id(connection, project_id, version_id)
        row = connection.execute(
            "SELECT contract_json, template_digest FROM design_component_study_templates "
            "WHERE component_id = ? AND regime = ?", (component_id, payload.regime)).fetchone()
    if row is None:
        raise HTTPException(409, detail=f"No saved {payload.regime} study template; record one before running.")
    try:
        contract = PipeStudyContract.model_validate_json(row["contract_json"])
    except ValidationError as error:
        raise HTTPException(409, detail="The saved study template is no longer a valid contract.") from error
    try:
        return case_runs.start_run(workspace_id, component_id, payload.regime, contract, row["template_digest"],
                                   payload.retry_of, payload.processors)
    except case_runs.CaseRunError as error:
        raise _run_failure(error) from error


def _run_scope(project_id: str, version_id: str, component_id: str) -> str:
    with open_sqlite_connection() as connection:
        _component(connection, project_id, version_id, component_id)
        return _data_workspace_id(connection, project_id, version_id)


@router.get("/{component_id}/cfd-runs")
def list_component_cfd_runs(project_id: str, version_id: str, component_id: str) -> list[dict]:
    return case_runs.list_runs(_run_scope(project_id, version_id, component_id), component_id)


@router.get("/{component_id}/cfd-runs/{run_id}")
def get_component_cfd_run(project_id: str, version_id: str, component_id: str, run_id: str) -> dict:
    try:
        return case_runs.read_run(_run_scope(project_id, version_id, component_id), component_id, run_id)
    except case_runs.CaseRunError as error:
        raise _run_failure(error) from error


@router.get("/{component_id}/cfd-runs/{run_id}/events")
def get_component_cfd_run_events(project_id: str, version_id: str, component_id: str, run_id: str,
                                 cursor: int = 0) -> dict:
    try:
        return case_runs.read_events(_run_scope(project_id, version_id, component_id), component_id, run_id, cursor)
    except case_runs.CaseRunError as error:
        raise _run_failure(error) from error


@router.get("/{component_id}/cfd-runs/{run_id}/fields")
def get_component_cfd_run_fields(project_id: str, version_id: str, component_id: str, run_id: str) -> dict:
    """Pressure and velocity field slice from a finished run's VTK export, or an honest unavailable reason."""
    try:
        return case_runs.read_run_fields(_run_scope(project_id, version_id, component_id), component_id, run_id)
    except case_runs.CaseRunError as error:
        raise _run_failure(error) from error


@router.post("/{component_id}/cfd-runs/{run_id}/stop")
def stop_component_cfd_run(project_id: str, version_id: str, component_id: str, run_id: str) -> dict:
    try:
        return case_runs.request_stop(_run_scope(project_id, version_id, component_id), component_id, run_id)
    except case_runs.CaseRunError as error:
        raise _run_failure(error) from error


def _segment_status(segment) -> ReducedSegmentStatus:
    v = segment.validity
    return ReducedSegmentStatus(
        regime=segment.regime, reynolds_min=v.reynolds_min, reynolds_max=v.reynolds_max, diameter_m=v.diameter_m,
        density_kg_m3=v.density_kg_m3, dynamic_viscosity_pa_s=v.dynamic_viscosity_pa_s,
        turbulence_model=v.turbulence_model, wall_treatment=v.wall_treatment,
        combined_uncertainty=segment.uncertainty.combined, reference=segment.reference)


@router.get("/{component_id}/reduced-models", response_model=list[ReducedModelStatus])
def component_reduced_models(project_id: str, version_id: str, component_id: str) -> list[ReducedModelStatus]:
    """Read-only CFD reduced-model status of the pinned Studio versions; the pin is never changed here."""
    with open_sqlite_connection() as connection:
        _component(connection, project_id, version_id, component_id)
        workspace_id = _data_workspace_id(connection, project_id, version_id)
        attached = connection.execute(
            "SELECT model_version_id, node, attached_at FROM design_component_models WHERE component_id = ? "
            "ORDER BY attached_at, rowid", (component_id,)).fetchall()
        current: tuple[str | None, str | None] | None = None
        templates = _study_templates(connection, component_id)
        result = []
        try:
            pinned_id = (_process_unit(connection, project_id, version_id, component_id)[1].get("hydraulics_model")
                         or {}).get("studio_version_id")
        except HTTPException:
            pinned_id = None
        for item in attached:
            identity = {"model_version_id": item["model_version_id"], "node": item["node"],
                        "attached_at": item["attached_at"]}
            source = published_source(connection, workspace_id, item["model_version_id"])
            if source is None:
                result.append(ReducedModelStatus(**identity, status="invalid", obsolete_reasons=[
                    "published Studio source is missing or does not match its digest"]))
                continue
            try:
                model: ReducedModel = reduced_model_of_source(source)
            except PublicationRefused as error:
                if "REDUCED_MODEL_JSON = " in source:
                    result.append(ReducedModelStatus(**identity, status="invalid", obsolete_reasons=list(error.reasons)))
                continue
            except (ValueError, SyntaxError) as error:
                result.append(ReducedModelStatus(**identity, status="invalid", obsolete_reasons=[
                    f"carried reduced model is unreadable: {error}"]))
                continue
            if current is None:
                current = _current_geometry_digest(connection, workspace_id, component_id)
            digest, why = current
            reasons = (list(obsolescence(model, geometry_digest=digest, study_templates=templates)) if digest
                       else ["component geometry unavailable; obsolescence cannot be checked",
                             *([why] if why else [])])
            result.append(ReducedModelStatus(
                **identity, reduced_model_id=model.model_id, reduced_model_version_id=model.version_id,
                status="unknown" if digest is None else "obsolete" if reasons else "current",
                segments=[_segment_status(segment) for segment in model.segments],
                geometry_digest=model.geometry_digest, current_geometry_digest=digest, obsolete_reasons=reasons))
        for entry in result:
            entry.process_unit_pinned = pinned_id is not None and entry.model_version_id == pinned_id
        return result


def pinned_reduced_model_obsolescence(workspace_id: str, studio_version_id: str, geometry_digest: str) -> list[str]:
    """Read-only: why Design components pinning this Studio version no longer match the study geometry."""
    reasons: list[str] = []
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT DISTINCT c.id, c.name FROM design_component_models m JOIN design_components c ON c.id = m.component_id "
            "JOIN design_project_versions v ON v.id = c.version_id WHERE m.model_version_id = ? "
            "AND v.data_workspace_id = ? ORDER BY c.id", (studio_version_id, workspace_id)).fetchall()
        source = published_source(connection, workspace_id, studio_version_id)
        try:
            model = reduced_model_of_source(source) if source else None
        except (PublicationRefused, ValueError, SyntaxError):
            model = None
        for row in rows:
            digest, _ = _current_geometry_digest(connection, workspace_id, row["id"])
            name = row["name"] or row["id"]
            if digest is None:
                reasons.append(f"component {name} geometry unavailable; obsolescence cannot be checked")
            elif digest != geometry_digest:
                reasons.append(f"component {name} geometry changed since the study (reduced model obsolete, rerun the study)")
            templates = _study_templates(connection, row["id"])
            reasons.extend(f"component {name} {segment.regime} study inputs changed since the study "
                           "(reduced model obsolete, rerun the study)" for segment in (model.segments if model else ())
                           if templates.get(segment.regime, segment.study_template_digest) != segment.study_template_digest)
    return reasons


def _data_workspace_id(connection, project_id: str, version_id: str) -> str:
    return connection.execute(
        "SELECT data_workspace_id FROM design_project_versions WHERE id = ? AND project_id = ?",
        (version_id, project_id),
    ).fetchone()["data_workspace_id"]


@router.post("/{component_id}/reduced-models", response_model=ReducedModelStatus)
def publish_component_reduced_model(
    project_id: str, version_id: str, component_id: str, payload: ReducedModelPublication,
) -> ReducedModelStatus:
    """Fit, publish through Studio and pin a CFD smooth-tube reduced model for this component."""

    def unchanged(connection, action: str) -> None:
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        if _component(connection, project_id, version_id, component_id)["revision"] != payload.expected_revision:
            raise HTTPException(409, detail=f"Component changed; reload before {action}.")

    with open_sqlite_connection() as connection:
        unchanged(connection, "publishing a reduced model")
        workspace_id = _data_workspace_id(connection, project_id, version_id)
    try:
        points, discretization = publication_inputs(workspace_id, component_id, payload.run_ids,
                                                    payload.mesh_study_run_ids)
        model = publish(payload.model_id, points, discretization, payload.tolerances)
    except PublicationRefused as error:
        raise HTTPException(422, detail={"message": "Reduced model publication refused; nothing was created.",
                                         "reasons": list(error.reasons)}) from error
    with open_sqlite_connection() as connection:
        digest, why = _current_geometry_digest(connection, workspace_id, component_id)
        templates = _study_templates(connection, component_id)
    if digest is None:
        raise HTTPException(409, detail=f"Component tube geometry is unknown ({why}); nothing was published.")
    if digest != model.geometry_digest:
        raise HTTPException(409, detail="The study was run on a different tube geometry than this component's "
                            "current one; rerun the study. Nothing was published.")
    stale = [segment.regime for segment in model.segments
             if templates.get(segment.regime, segment.study_template_digest) != segment.study_template_digest]
    if stale:
        raise HTTPException(409, detail=f"The study points do not come from this component's current {', '.join(stale)} "
                            "study inputs (recorded study template differs); rerun the study. Nothing was published.")
    try:  # Studio opens its own connections: no write transaction may be held here
        version = publish_to_studio(workspace_id, model)
    except PublicationRefused as error:
        raise HTTPException(422, detail={"message": "Studio publication refused; nothing was pinned.",
                                         "reasons": list(error.reasons)}) from error
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            unchanged(connection, "pinning the reduced model")
        except HTTPException as error:
            if error.status_code != 409:
                raise
            raise HTTPException(409, detail=f"{error.detail} Studio version {version['id']} was published but not "
                                "pinned; attach it explicitly once the component is reloaded.") from error
        _pin_model(connection, project_id, version_id, component_id, version["id"])
        log_event(connection, event_type="DesignComponentReducedModelPublished", actor="local-user",
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "model_version_id": version["id"],
                           "reduced_model_id": model.model_id, "reduced_model_version_id": model.version_id,
                           "geometry_digest": model.geometry_digest, "run_ids": list(model.provenance["run_ids"])})
        connection.commit()
    return next(item for item in component_reduced_models(project_id, version_id, component_id)
                if item.model_version_id == version["id"])


class HydraulicsPinRefused(Exception):
    """A Design-owned refusal to pin a CFD reduced model on a Process unit; status mirrors the HTTP route."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _resolve_component_pin(project_id: str, version_id: str, component_id: str, model_version_id: str,
                           out_of_range: Literal["refuse", "warn"]) -> tuple[dict, str, UnitHydraulicsPin]:
    """Read-only: the Process link, data workspace and typed pin of one current reduced model of a component."""
    with open_sqlite_connection() as connection:
        link = connection.execute(
            "SELECT draft_id, unit_id FROM design_component_units WHERE component_id = ?", (component_id,)).fetchone()
        if link is None:
            raise HydraulicsPinRefused(422, "This component has no Process unit source.")
        row = connection.execute(
            "SELECT source_sha256 FROM design_component_models WHERE component_id = ? AND model_version_id = ?",
            (component_id, model_version_id)).fetchone()
        if row is None:
            raise HydraulicsPinRefused(404, "Reduced model is not pinned on this component.")
        workspace_id = _data_workspace_id(connection, project_id, version_id)
        source = published_source(connection, workspace_id, model_version_id)
    status = next((item for item in component_reduced_models(project_id, version_id, component_id)
                   if item.model_version_id == model_version_id), None)
    if status is None or status.status != "current" or not status.reduced_model_version_id or not status.geometry_digest:
        reasons = "; ".join(status.obsolete_reasons) if status else "not a CFD reduced model"
        raise HydraulicsPinRefused(409, f"Reduced model is not current ({status.status if status else 'unavailable'}): "
                                   f"{reasons}. Nothing was pinned on the Process unit.")
    if source is None or hashlib.sha256(source.encode("utf-8")).hexdigest() != row["source_sha256"]:
        raise HydraulicsPinRefused(409, "Published Studio source no longer matches the attached digest. Nothing was pinned.")
    pin = UnitHydraulicsPin(kind="cfd_reduced_model", studio_version_id=model_version_id, source_sha256=row["source_sha256"],
                            reduced_model_version_id=status.reduced_model_version_id, geometry_digest=status.geometry_digest,
                            out_of_range=out_of_range)
    return dict(link), workspace_id, pin


def resolve_process_hydraulics_pin(workspace_id: str, draft_id: str, unit_id: str, model_version_id: str,
                                   out_of_range: Literal["refuse", "warn"]) -> tuple[UnitHydraulicsPin, dict]:
    """Read-only: the typed pin for a Process unit through the Design component linked to it (agent and GUI share it)."""
    with open_sqlite_connection() as connection:
        links = connection.execute(
            "SELECT u.component_id, c.project_id, u.version_id FROM design_component_units u "
            "JOIN design_components c ON c.id = u.component_id "
            "JOIN design_project_versions v ON v.id = u.version_id "
            "WHERE u.draft_id = ? AND u.unit_id = ? AND v.data_workspace_id = ? ORDER BY v.created_at DESC, u.rowid DESC",
            (draft_id, unit_id, workspace_id)).fetchall()
        attached = {row["component_id"] for row in connection.execute(
            "SELECT component_id FROM design_component_models WHERE model_version_id = ?", (model_version_id,)).fetchall()}
    if not links:
        raise HydraulicsPinRefused(422, "This Process unit has no linked Design component.")
    link = next((item for item in links if item["component_id"] in attached), links[0])
    _, _, pin = _resolve_component_pin(link["project_id"], link["version_id"], link["component_id"],
                                       model_version_id, out_of_range)
    return pin, {"design_component_id": link["component_id"], "design_version_id": link["version_id"],
                 "model_version_id": model_version_id, "design_project_id": link["project_id"]}


def record_process_hydraulics_pin(provenance: dict, *, draft_id: str, unit_id: str, pin: UnitHydraulicsPin,
                                  draft_revision: str, actor: str) -> None:
    """Log the Design component history event for an applied Process hydraulics pin (no draft write here)."""
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        log_event(connection, event_type="DesignComponentProcessHydraulicsPinned", actor=actor,
                  target_type="DesignComponent", target_id=provenance["design_component_id"],
                  workspace_id=provenance["design_project_id"],
                  payload={"version_id": provenance["design_version_id"], "draft_id": draft_id, "unit_id": unit_id,
                           "model_version_id": provenance["model_version_id"],
                           "reduced_model_version_id": pin.reduced_model_version_id,
                           "geometry_digest": pin.geometry_digest, "draft_revision": draft_revision})
        connection.commit()


@router.post("/{component_id}/reduced-models/{model_version_id}/process-pin", response_model=ReducedModelStatus)
def pin_reduced_model_to_process_unit(
    project_id: str, version_id: str, component_id: str, model_version_id: str, payload: ReducedModelProcessPin,
) -> ReducedModelStatus:
    """Pin one current CFD reduced model on this component's Process PBR unit through a typed draft operation."""
    with open_sqlite_connection() as connection:
        _active_version(connection, project_id, version_id, payload.expected_active_version_id)
        if _component(connection, project_id, version_id, component_id)["revision"] != payload.expected_revision:
            raise HTTPException(409, detail="Component changed; reload before pinning the reduced model.")
    try:
        link, workspace_id, pin = _resolve_component_pin(project_id, version_id, component_id, model_version_id,
                                                          payload.out_of_range)
    except HydraulicsPinRefused as error:
        raise HTTPException(error.status, detail=str(error)) from error
    try:  # the draft has its own file lock: no sqlite write transaction is held here
        process = draft.patch(workspace_id, link["draft_id"], payload.expected_draft_revision, [
            SetUnitHydraulics(op="set_unit_hydraulics", unit=link["unit_id"], hydraulics=pin)], actor="local-user",
            provenance={"design_component_id": component_id, "design_version_id": version_id,
                        "model_version_id": model_version_id})
    except DraftError as error:
        raise HTTPException(error.status, detail=str(error)) from error
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        log_event(connection, event_type="DesignComponentProcessHydraulicsPinned", actor="local-user",
                  target_type="DesignComponent", target_id=component_id, workspace_id=project_id,
                  payload={"version_id": version_id, "draft_id": link["draft_id"], "unit_id": link["unit_id"],
                           "model_version_id": model_version_id, "reduced_model_version_id": pin.reduced_model_version_id,
                           "geometry_digest": pin.geometry_digest, "draft_revision": process["revision"]})
        connection.commit()
    return next(item for item in component_reduced_models(project_id, version_id, component_id)
                if item.model_version_id == model_version_id)
