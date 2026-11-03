from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.events.service import utc_now
from app.modules.flowsheet.service import (
    FlowsheetError,
    build_flowsheet_graph_from_connection,
    resolve_flowsheet_node_from_connection,
)
from app.modules.project_knowledge.apply import ProjectBasisApplyError, ProjectBasisApplyService
from app.modules.project_knowledge.models import (
    ApprovalRead,
    ApprovalRequest,
    DraftCreate,
    DraftRead,
    DraftUpdate,
    ImpactPreview,
    ProjectKnowledgeOperation,
    ReconcileRead,
    ReconcileRequest,
    RevalidationRead,
    ScalarAdmissionRequest,
    ScalarResultRead,
    SnapshotRead,
    ValidationRead,
    ValidationRequest,
    WorkingRevisionRead,
)

MAX_ANCESTORS = 64
MAX_AFFECTED_REFS = 1000
CRITERION_RULE_VERSION = "scalar-v1"
VALIDATOR_ID = "project-knowledge-scalar"
VALIDATOR_VERSION = "1"
SNAPSHOT_MANIFEST_VERSION = "1"
SCALAR_EXTRACTOR_ID = "project-knowledge-json-scalar"
SCALAR_EXTRACTOR_VERSION = "1"


class ProjectKnowledgeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except ValueError as exc:
        raise ProjectKnowledgeError(
            "canonical_json_non_finite",
            "Project Knowledge canonical JSON requires finite numeric values.",
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decode_snapshot_json(raw: object, *, label: str) -> object:
    if not isinstance(raw, str):
        raise ProjectKnowledgeError("snapshot_manifest_invalid", f"Reconciled {label} snapshot manifest is malformed.")
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProjectKnowledgeError("snapshot_manifest_invalid", f"Reconciled {label} snapshot manifest is malformed.") from exc


def _decode_snapshot_owner_manifest(raw: object) -> list[dict[str, object]]:
    decoded = _decode_snapshot_json(raw, label="owner")
    if not isinstance(decoded, list):
        raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled owner snapshot manifest is malformed.")
    manifest: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in decoded:
        if not isinstance(item, dict):
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled owner snapshot manifest is malformed.")
        kind = item.get("kind")
        owner_id = item.get("id")
        token = item.get("owner_revision_token")
        state = item.get("state")
        state_digest = item.get("state_digest")
        if (
            not isinstance(kind, str)
            or not kind
            or not isinstance(owner_id, str)
            or not owner_id
            or not isinstance(token, str)
            or not token
            or not isinstance(state, dict)
            or not isinstance(state_digest, str)
            or not state_digest
        ):
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled owner snapshot manifest is malformed.")
        identity = (kind, owner_id)
        if identity in seen:
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled owner snapshot contains duplicate owner identities.")
        if _digest(state) != state_digest:
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled owner snapshot state digest is invalid.")
        seen.add(identity)
        manifest.append(item)
    return manifest


def _decode_snapshot_edge_manifest(raw: object) -> list[dict[str, object]]:
    decoded = _decode_snapshot_json(raw, label="graph")
    if not isinstance(decoded, list):
        raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled graph snapshot manifest is malformed.")
    manifest: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in decoded:
        if not isinstance(item, dict):
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled graph snapshot manifest is malformed.")
        values = tuple(item.get(key) for key in ("upstream_ref", "downstream_ref", "relation", "edge_class"))
        if any(not isinstance(value, str) or not value for value in values):
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled graph snapshot manifest is malformed.")
        identity = (str(values[0]), str(values[1]), str(values[2]), str(values[3]))
        if identity in seen:
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled graph snapshot contains duplicate edge identities.")
        seen.add(identity)
        manifest.append(item)
    return manifest


def _decode_snapshot_canonical_id_map(raw: object) -> dict[str, str]:
    decoded = _decode_snapshot_json(raw, label="canonical id map")
    if not isinstance(decoded, dict):
        raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled canonical id map is malformed.")
    result: dict[str, str] = {}
    for key, value in decoded.items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
            raise ProjectKnowledgeError("snapshot_manifest_invalid", "Reconciled canonical id map is malformed.")
        result[key] = value
    return result


def _workspace_exists(connection: sqlite3.Connection, workspace_id: str) -> bool:
    return connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is not None


def _require_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    if not _workspace_exists(connection, workspace_id):
        raise ProjectKnowledgeError("workspace_not_found", "Workspace was not found.")


def _normalize_operations(draft_id: str, operations: list[ProjectKnowledgeOperation]) -> list[ProjectKnowledgeOperation]:
    normalized: list[ProjectKnowledgeOperation] = []
    seen: set[str] = set()
    for operation in operations:
        operation_id = operation.operation_id or str(uuid4())
        if operation_id in seen:
            raise ProjectKnowledgeError("operation_id_duplicate", "Draft operation ids must be unique.")
        seen.add(operation_id)
        data = operation.model_dump()
        data["operation_id"] = operation_id
        if operation.operation_kind == "create":
            expected = f"draft:{draft_id}:op:{operation_id}"
            if operation.provisional_ref not in {None, expected}:
                raise ProjectKnowledgeError("provisional_ref_invalid", "Create provisional reference is server-owned.")
            data["provisional_ref"] = expected
        elif operation.provisional_ref and not operation.provisional_ref.startswith(f"draft:{draft_id}:op:"):
            raise ProjectKnowledgeError("provisional_ref_invalid", "Provisional reference belongs to another draft.")
        normalized.append(ProjectKnowledgeOperation.model_validate(data))
    return normalized


def _draft_from_row(row: sqlite3.Row) -> DraftRead:
    return DraftRead(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        parent_revision_id=row["parent_revision_id"],
        parent_kind=str(row["parent_kind"]),
        revision_token=str(row["revision_token"]),
        operations=[ProjectKnowledgeOperation.model_validate(item) for item in json.loads(row["operations_json"])],
        preview_digest=row["preview_digest"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def create_draft(payload: DraftCreate) -> DraftRead:
    draft_id = str(uuid4())
    now = utc_now()
    token = str(uuid4())
    operations = _normalize_operations(draft_id, payload.operations)
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_workspace(connection, payload.workspace_id)
            _validate_parent(connection, payload.workspace_id, payload.parent_kind, payload.parent_revision_id)
            connection.execute(
                """
                INSERT INTO project_knowledge_drafts (
                    id, workspace_id, parent_revision_id, parent_kind, revision_token,
                    operations_json, preview_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    draft_id,
                    payload.workspace_id,
                    payload.parent_revision_id,
                    payload.parent_kind,
                    token,
                    _canonical_json([item.model_dump() for item in operations]),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM project_knowledge_drafts WHERE id = ?", (draft_id,)).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    assert row is not None
    return _draft_from_row(row)


def update_draft(draft_id: str, payload: DraftUpdate) -> DraftRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM project_knowledge_drafts WHERE id = ? AND workspace_id = ?",
                (draft_id, payload.workspace_id),
            ).fetchone()
            if row is None:
                raise ProjectKnowledgeError("draft_not_found", "Draft was not found in workspace.")
            if row["revision_token"] != payload.expected_revision_token:
                raise ProjectKnowledgeError("draft_stale", "Draft changed since it was reviewed.")
            approved = connection.execute(
                "SELECT id FROM project_knowledge_approval_requests WHERE draft_id = ? AND state = 'success'",
                (draft_id,),
            ).fetchone()
            if approved is not None:
                raise ProjectKnowledgeError("draft_already_approved", "An approved draft is immutable.")
            operations = _normalize_operations(draft_id, payload.operations)
            now = utc_now()
            token = str(uuid4())
            cursor = connection.execute(
                """
                UPDATE project_knowledge_drafts
                SET operations_json = ?, revision_token = ?, preview_digest = NULL, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND revision_token = ?
                """,
                (
                    _canonical_json([item.model_dump() for item in operations]),
                    token,
                    now,
                    draft_id,
                    payload.workspace_id,
                    payload.expected_revision_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ProjectKnowledgeError("draft_stale", "Draft changed before commit.")
            row = connection.execute("SELECT * FROM project_knowledge_drafts WHERE id = ?", (draft_id,)).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    assert row is not None
    return _draft_from_row(row)


def get_draft(workspace_id: str, draft_id: str) -> DraftRead:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT * FROM project_knowledge_drafts WHERE id = ? AND workspace_id = ?",
            (draft_id, workspace_id),
        ).fetchone()
    if row is None:
        raise ProjectKnowledgeError("draft_not_found", "Draft was not found in workspace.")
    return _draft_from_row(row)


def _validate_parent(
    connection: sqlite3.Connection,
    workspace_id: str,
    parent_kind: str,
    parent_revision_id: str | None,
) -> None:
    if parent_revision_id is None:
        if parent_kind != "reconciled":
            raise ProjectKnowledgeError("parent_missing", "A working parent requires an exact revision id.")
        return
    row = connection.execute(
        "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ?",
        (parent_revision_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ProjectKnowledgeError("parent_missing", "Parent revision was not found in workspace.")
    if parent_kind == "working" and row["state"] != "working":
        raise ProjectKnowledgeError("parent_invalid", "Working parent is no longer an accepted working revision.")
    if parent_kind == "reconciled" and row["state"] != "reconciled":
        raise ProjectKnowledgeError("parent_invalid", "Reconciled parent is not a reconciled revision.")
    if parent_kind == "reconciled":
        snapshot = connection.execute(
            "SELECT id FROM project_knowledge_reconciled_snapshots WHERE reconciled_revision_id = ? AND workspace_id = ?",
            (parent_revision_id, workspace_id),
        ).fetchone()
        if snapshot is None:
            raise ProjectKnowledgeError("snapshot_missing", "Historical reconciled parent has no exact snapshot.")


def _working_chain(
    connection: sqlite3.Connection,
    workspace_id: str,
    parent_kind: str,
    parent_revision_id: str | None,
) -> tuple[list[sqlite3.Row], str | None]:
    if parent_revision_id is None:
        return [], None
    if parent_kind == "reconciled":
        _validate_parent(connection, workspace_id, parent_kind, parent_revision_id)
        return [], parent_revision_id
    chain: list[sqlite3.Row] = []
    seen: set[str] = set()
    current_id: str | None = parent_revision_id
    descendant_id: str | None = None
    reconciled_base: str | None = None
    while current_id is not None:
        if len(chain) >= MAX_ANCESTORS:
            raise ProjectKnowledgeError("ancestor_bound_exceeded", "Working ancestor chain exceeds the V0 bound.")
        if current_id in seen:
            raise ProjectKnowledgeError("ancestor_cycle", "Working ancestor chain contains a cycle.")
        seen.add(current_id)
        row = connection.execute(
            "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ?",
            (current_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ProjectKnowledgeError("ancestor_missing", "Working ancestor revision is missing.")
        row_state = str(row["state"])
        exact_superseded_parent = (
            row_state == "superseded"
            and descendant_id is not None
            and str(row["superseded_by_revision_id"] or "") == descendant_id
        )
        if row_state != "working" and not exact_superseded_parent:
            raise ProjectKnowledgeError("ancestor_invalid", "Working ancestor is discarded, superseded by another branch, or already consumed.")
        chain.append(row)
        if row["parent_kind"] == "reconciled":
            reconciled_base = row["parent_revision_id"]
            if reconciled_base is not None:
                _validate_parent(connection, workspace_id, "reconciled", str(reconciled_base))
            break
        descendant_id = str(row["id"])
        current_id = row["parent_revision_id"]
        if current_id is None:
            raise ProjectKnowledgeError("ancestor_missing", "Working ancestor chain has a missing parent.")
    chain.reverse()
    return chain, None if reconciled_base is None else str(reconciled_base)


def _base_edges(
    connection: sqlite3.Connection,
    workspace_id: str,
    reconciled_base_revision_id: str | None,
) -> tuple[set[tuple[str, str, str, str]], str, bool, list[str]]:
    if reconciled_base_revision_id is not None:
        snapshot = connection.execute(
            "SELECT * FROM project_knowledge_reconciled_snapshots WHERE workspace_id = ? AND reconciled_revision_id = ?",
            (workspace_id, reconciled_base_revision_id),
        ).fetchone()
        if snapshot is None:
            raise ProjectKnowledgeError("snapshot_missing", "Reconciled base snapshot is missing.")
        if str(snapshot["manifest_version"]) != SNAPSHOT_MANIFEST_VERSION:
            raise ProjectKnowledgeError("snapshot_version_unsupported", "Reconciled snapshot manifest version is unsupported.")
        edge_manifest = _decode_snapshot_edge_manifest(snapshot["edge_manifest_json"])
        if _digest(edge_manifest) != snapshot["graph_digest"]:
            raise ProjectKnowledgeError("snapshot_digest_mismatch", "Reconciled graph snapshot digest is invalid.")
        edges = {
            (str(item["upstream_ref"]), str(item["downstream_ref"]), str(item["relation"]), str(item["edge_class"]))
            for item in edge_manifest
        }
        return edges, str(snapshot["graph_digest"]), bool(snapshot["graph_complete"]), []

    graph = build_flowsheet_graph_from_connection(connection, workspace_id)
    manifest = [
        {
            "upstream_ref": edge.upstream_ref,
            "downstream_ref": edge.downstream_ref,
            "relation": edge.relation,
            "edge_class": edge.edge_class,
        }
        for edge in graph.edges
    ]
    diagnostics = [item.code for item in graph.diagnostics.unresolved_references]
    return (
        {(item["upstream_ref"], item["downstream_ref"], item["relation"], item["edge_class"]) for item in manifest},
        _digest(manifest),
        graph.is_acyclic and not diagnostics,
        diagnostics,
    )


def _owner_token(connection: sqlite3.Connection, workspace_id: str, kind: str, owner_id: str) -> str:
    table = {
        "requirement": "requirements",
        "decision": "decisions",
        "assumption": "assumptions",
        "parameter": "parameters",
        "model_spec": "model_specs",
        "requirement_applicability": "requirement_applicability",
    }.get(kind)
    if table is None:
        raise ProjectKnowledgeError("owner_kind_unsupported", "Owner kind has no V0 revision token.")
    row = connection.execute(
        f"SELECT updated_at FROM {table} WHERE id = ? AND workspace_id = ?",
        (owner_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ProjectKnowledgeError("owner_not_found", "Canonical owner was not found in workspace.")
    return str(row["updated_at"])


def _historical_snapshot_owner_basis(
    connection: sqlite3.Connection,
    workspace_id: str,
    reconciled_base_revision_id: str | None,
) -> tuple[dict[str, str], set[str]]:
    if reconciled_base_revision_id is None:
        return {}, set()
    snapshot = connection.execute(
        "SELECT * FROM project_knowledge_reconciled_snapshots WHERE workspace_id = ? AND reconciled_revision_id = ?",
        (workspace_id, reconciled_base_revision_id),
    ).fetchone()
    if snapshot is None:
        raise ProjectKnowledgeError("snapshot_missing", "Reconciled base snapshot is missing.")
    if str(snapshot["manifest_version"]) != SNAPSHOT_MANIFEST_VERSION:
        raise ProjectKnowledgeError("snapshot_version_unsupported", "Reconciled snapshot manifest version is unsupported.")
    owner_manifest = _decode_snapshot_owner_manifest(snapshot["owner_manifest_json"])
    if _digest(owner_manifest) != snapshot["owner_manifest_digest"]:
        raise ProjectKnowledgeError("snapshot_digest_mismatch", "Reconciled owner snapshot digest is invalid.")
    owner_tokens: dict[str, str] = {}
    owner_refs: set[str] = set()
    for item in owner_manifest:
        kind = str(item["kind"])
        owner_id = str(item["id"])
        token = str(item["owner_revision_token"])
        owner_ref = f"{kind}:{owner_id}"
        owner_tokens[owner_ref] = token
        owner_refs.add(owner_ref)
    return owner_tokens, owner_refs


def _validate_projected_dependency_ref(
    connection: sqlite3.Connection,
    workspace_id: str,
    node_ref: str,
    *,
    provisional_refs: set[str],
    snapshot_owner_refs: set[str],
) -> None:
    if node_ref in provisional_refs or node_ref in snapshot_owner_refs:
        return
    try:
        resolve_flowsheet_node_from_connection(connection, workspace_id, node_ref)
    except FlowsheetError as exc:
        raise ProjectKnowledgeError(
            "dependency_ref_unresolved",
            "Proposed dependency endpoint does not resolve to an exact projected node.",
        ) from exc


def _apply_edge_deltas(
    connection: sqlite3.Connection,
    workspace_id: str,
    edges: set[tuple[str, str, str, str]],
    operation: ProjectKnowledgeOperation,
    *,
    provisional_refs: set[str],
    snapshot_owner_refs: set[str],
) -> None:
    for upstream, downstream, relation in operation.dependency_remove:
        matches = [edge for edge in edges if edge[0] == upstream and edge[1] == downstream and edge[2] == relation]
        if not matches:
            raise ProjectKnowledgeError("edge_remove_missing", "Proposed dependency removal does not match the projected graph.")
        for edge in matches:
            edges.remove(edge)
    for upstream, downstream, relation in operation.dependency_add:
        _validate_projected_dependency_ref(
            connection,
            workspace_id,
            upstream,
            provisional_refs=provisional_refs,
            snapshot_owner_refs=snapshot_owner_refs,
        )
        _validate_projected_dependency_ref(
            connection,
            workspace_id,
            downstream,
            provisional_refs=provisional_refs,
            snapshot_owner_refs=snapshot_owner_refs,
        )
        edges.add((upstream, downstream, relation, "dependency"))


def _impact_from_connection(connection: sqlite3.Connection, draft: DraftRead) -> ImpactPreview:
    chain, reconciled_base = _working_chain(
        connection,
        draft.workspace_id,
        draft.parent_kind,
        draft.parent_revision_id,
    )
    edges, base_digest, graph_complete, diagnostics = _base_edges(connection, draft.workspace_id, reconciled_base)
    snapshot_owner_tokens, snapshot_owner_refs = _historical_snapshot_owner_basis(
        connection,
        draft.workspace_id,
        reconciled_base,
    )
    accepted_ops: list[ProjectKnowledgeOperation] = []
    ancestor_ids: list[str] = []
    for revision in chain:
        ancestor_ids.append(str(revision["id"]))
        accepted_ops.extend(ProjectKnowledgeOperation.model_validate(item) for item in json.loads(revision["change_set_json"]))
    all_ops = [*accepted_ops, *draft.operations]
    owner_tokens: dict[str, str] = {}
    changed_refs: set[str] = set()
    applicability_refs: set[str] = set()
    provisional_refs = {op.provisional_ref for op in all_ops if op.operation_kind == "create" and op.provisional_ref}
    for operation in all_ops:
        if operation.operation_kind != "create" and operation.owner_kind != "requirement_applicability":
            owner_id = operation.owner_id
            if owner_id is None:
                raise ProjectKnowledgeError("owner_identity_missing", "Existing-owner operation is missing its owner id.")
            if owner_id.startswith("draft:"):
                if owner_id not in provisional_refs:
                    raise ProjectKnowledgeError("provisional_ref_unresolved", "Provisional owner reference is not in the accepted chain.")
            else:
                owner_ref = f"{operation.owner_kind}:{owner_id}"
                if reconciled_base is None:
                    token = _owner_token(connection, draft.workspace_id, operation.owner_kind, owner_id)
                else:
                    token = snapshot_owner_tokens.get(owner_ref)
                    if token is None:
                        raise ProjectKnowledgeError(
                            "snapshot_owner_missing",
                            "Existing owner is absent from the exact reconciled parent snapshot.",
                        )
                if operation.expected_updated_at != token:
                    raise ProjectKnowledgeError("owner_stale", "Owner token changed since draft capture.")
                owner_tokens[owner_ref] = token
                changed_refs.add(owner_ref)
        elif operation.operation_kind == "create" and operation.provisional_ref:
            changed_refs.add(operation.provisional_ref)
        if operation.owner_kind == "requirement_applicability":
            relation_id = operation.owner_id
            if relation_id and operation.operation_kind == "retire_applicability":
                token = _owner_token(connection, draft.workspace_id, "requirement_applicability", relation_id)
                if operation.expected_updated_at != token:
                    raise ProjectKnowledgeError("applicability_stale", "Applicability relation changed since draft capture.")
                owner_tokens[f"requirement_applicability:{relation_id}"] = token
            applicability_refs.add(relation_id or operation.operation_id or "proposed")
        _apply_edge_deltas(
            connection,
            draft.workspace_id,
            edges,
            operation,
            provisional_refs=provisional_refs,
            snapshot_owner_refs=snapshot_owner_refs,
        )

    adjacency: dict[str, set[str]] = {}
    for upstream, downstream, _relation, edge_class in edges:
        if edge_class == "dependency":
            adjacency.setdefault(upstream, set()).add(downstream)
    affected = set(changed_refs)
    pending = list(changed_refs)
    while pending:
        current = pending.pop()
        for downstream in sorted(adjacency.get(current, ())):
            if downstream not in affected:
                if len(affected) >= MAX_AFFECTED_REFS:
                    raise ProjectKnowledgeError("impact_bound_exceeded", "Projected impact exceeds the V0 bound.")
                affected.add(downstream)
                pending.append(downstream)
    recomputation = sorted(
        ref for ref in affected if ref.startswith(("simulation_run:", "runner_job:", "evidence:"))
    )
    payload = {
        "base_graph_digest": base_digest,
        "ancestor_revision_ids": ancestor_ids,
        "draft_id": draft.id,
        "draft_revision_token": draft.revision_token,
        "owner_tokens": owner_tokens,
        "operations": [item.model_dump() for item in draft.operations],
        "edges": sorted(edges),
        "applicability_refs": sorted(applicability_refs),
    }
    return ImpactPreview(
        draft_id=draft.id,
        draft_revision_token=draft.revision_token,
        parent_kind=draft.parent_kind,
        parent_revision_id=draft.parent_revision_id,
        ancestor_revision_ids=ancestor_ids,
        affected_refs=sorted(affected),
        owner_tokens=owner_tokens,
        applicability_refs=sorted(applicability_refs),
        recomputation_required=recomputation,
        diagnostics=sorted(set(diagnostics)),
        complete=graph_complete and not diagnostics,
        digest=_digest(payload),
    )


def preview_impact(workspace_id: str, draft_id: str) -> ImpactPreview:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN")
        try:
            draft = get_draft_from_connection(connection, workspace_id, draft_id)
            preview = _impact_from_connection(connection, draft)
        finally:
            connection.rollback()
    return preview


def get_draft_from_connection(connection: sqlite3.Connection, workspace_id: str, draft_id: str) -> DraftRead:
    row = connection.execute(
        "SELECT * FROM project_knowledge_drafts WHERE id = ? AND workspace_id = ?",
        (draft_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ProjectKnowledgeError("draft_not_found", "Draft was not found in workspace.")
    return _draft_from_row(row)


def approve_draft(payload: ApprovalRequest) -> ApprovalRead:
    failure: tuple[str, str] | None = None
    request_digest: str | None = None
    request_id = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_workspace(connection, payload.workspace_id)
            existing = connection.execute(
                "SELECT * FROM project_knowledge_approval_requests WHERE workspace_id = ? AND approval_request_key = ?",
                (payload.workspace_id, payload.approval_request_key),
            ).fetchone()
            draft = get_draft_from_connection(connection, payload.workspace_id, payload.draft_id)
            request_digest = _digest(
                {
                    "draft_id": draft.id,
                    "draft_revision_token": payload.expected_draft_revision_token,
                    "parent_kind": draft.parent_kind,
                    "parent_revision_id": draft.parent_revision_id,
                    "preview_digest": payload.expected_preview_digest,
                    "operations": [item.model_dump() for item in draft.operations],
                }
            )
            if existing is not None:
                bound = (
                    existing["draft_id"],
                    existing["draft_revision_token"],
                    existing["parent_revision_id"],
                    existing["parent_kind"],
                    existing["request_digest"],
                )
                requested = (
                    draft.id,
                    payload.expected_draft_revision_token,
                    draft.parent_revision_id,
                    draft.parent_kind,
                    request_digest,
                )
                if bound != requested:
                    raise ProjectKnowledgeError("approval_idempotency_conflict", "Approval retry key is bound to different values.")
                connection.rollback()
                return ApprovalRead(
                    request_id=str(existing["id"]),
                    state=str(existing["state"]),
                    outcome=existing["outcome"],
                    working_revision_id=existing["working_revision_id"],
                    failure_code=existing["failure_code"],
                )
            approved = connection.execute(
                """
                SELECT id FROM project_knowledge_approval_requests
                WHERE workspace_id = ? AND draft_id = ? AND state = 'success'
                """,
                (payload.workspace_id, draft.id),
            ).fetchone()
            if approved is not None:
                raise ProjectKnowledgeError("draft_already_approved", "An approved draft is immutable.")
            if draft.revision_token != payload.expected_draft_revision_token:
                raise ProjectKnowledgeError("draft_stale", "Draft changed since approval was prepared.")
            preview = _impact_from_connection(connection, draft)
            if preview.digest != payload.expected_preview_digest:
                raise ProjectKnowledgeError("preview_stale", "Impact preview changed since approval was prepared.")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO project_knowledge_approval_requests (
                    id, workspace_id, approval_request_key, draft_id, draft_revision_token,
                    parent_revision_id, parent_kind, request_digest, state, outcome,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
                """,
                (
                    request_id,
                    payload.workspace_id,
                    payload.approval_request_key,
                    draft.id,
                    draft.revision_token,
                    draft.parent_revision_id,
                    draft.parent_kind,
                    request_digest,
                    now,
                ),
            )
            revision_id = str(uuid4())
            change_set_json = _canonical_json([item.model_dump() for item in draft.operations])
            connection.execute(
                """
                INSERT INTO project_knowledge_revisions (
                    id, workspace_id, parent_revision_id, parent_kind, state,
                    change_set_digest, change_set_json, projected_state_digest,
                    origin, created_at, accepted_at
                ) VALUES (?, ?, ?, ?, 'working', ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    payload.workspace_id,
                    draft.parent_revision_id,
                    draft.parent_kind,
                    hashlib.sha256(change_set_json.encode("utf-8")).hexdigest(),
                    change_set_json,
                    preview.digest,
                    payload.origin,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE project_knowledge_approval_requests
                SET state = 'success', outcome = 'approved', working_revision_id = ?, completed_at = ?
                WHERE id = ?
                """,
                (revision_id, now, request_id),
            )
            connection.commit()
            return ApprovalRead(
                request_id=request_id,
                state="success",
                outcome="approved",
                working_revision_id=revision_id,
            )
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, ProjectKnowledgeError):
                failure = (exc.code, exc.message)
            else:
                failure = ("approval_failed", str(exc)[:500])
    if failure is not None:
        with open_sqlite_connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO project_knowledge_approval_requests (
                        id, workspace_id, approval_request_key, draft_id, draft_revision_token,
                        parent_revision_id, parent_kind, request_digest, state, outcome,
                        failure_code, failure_detail, created_at, completed_at
                    )
                    SELECT ?, ?, ?, ?, ?, parent_revision_id, parent_kind, ?, 'failed', 'rejected', ?, ?, ?, ?
                    FROM project_knowledge_drafts WHERE id = ? AND workspace_id = ?
                    ON CONFLICT(workspace_id, approval_request_key) DO NOTHING
                    """,
                    (
                        request_id,
                        payload.workspace_id,
                        payload.approval_request_key,
                        payload.draft_id,
                        payload.expected_draft_revision_token,
                        request_digest
                        or _digest({"draft": payload.draft_id, "token": payload.expected_draft_revision_token}),
                        failure[0],
                        failure[1],
                        utc_now(),
                        utc_now(),
                        payload.draft_id,
                        payload.workspace_id,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
        raise ProjectKnowledgeError(*failure)
    raise ProjectKnowledgeError("approval_failed", "Approval failed.")


def _revision_from_row(row: sqlite3.Row) -> WorkingRevisionRead:
    return WorkingRevisionRead(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        parent_revision_id=row["parent_revision_id"],
        parent_kind=str(row["parent_kind"]),
        state=str(row["state"]),
        change_set_digest=str(row["change_set_digest"]),
        operations=[ProjectKnowledgeOperation.model_validate(item) for item in json.loads(row["change_set_json"])],
        projected_state_digest=str(row["projected_state_digest"]),
        origin=str(row["origin"]),
        created_at=str(row["created_at"]),
        accepted_at=str(row["accepted_at"]),
        superseded_by_revision_id=row["superseded_by_revision_id"],
        reconciled_snapshot_id=row["reconciled_snapshot_id"],
    )


def get_revision(workspace_id: str, revision_id: str) -> WorkingRevisionRead:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ?",
            (revision_id, workspace_id),
        ).fetchone()
    if row is None:
        raise ProjectKnowledgeError("revision_not_found", "Working revision was not found.")
    return _revision_from_row(row)


def list_revisions(workspace_id: str) -> list[WorkingRevisionRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM project_knowledge_revisions WHERE workspace_id = ? ORDER BY accepted_at DESC, id ASC",
            (workspace_id,),
        ).fetchall()
    return [_revision_from_row(row) for row in rows]


def _extract_scalar_from_run_payload(output_payload: str | None, output_name: str) -> tuple[str, str]:
    if not output_payload:
        raise ProjectKnowledgeError("scalar_source_unavailable", "Run output payload is unavailable for deterministic extraction.")
    try:
        decoded = json.loads(output_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProjectKnowledgeError("scalar_source_invalid", "Run output payload is not valid JSON scalar evidence.") from exc
    if not isinstance(decoded, dict) or output_name not in decoded:
        raise ProjectKnowledgeError("scalar_output_missing", "Requested scalar output is not present in the run payload.")
    raw = decoded[output_name]
    unit = ""
    if isinstance(raw, dict):
        if "value" not in raw:
            raise ProjectKnowledgeError("scalar_source_invalid", "Scalar object evidence must contain a value field.")
        raw_value = raw["value"]
        raw_unit = raw.get("unit", "")
        if not isinstance(raw_unit, str):
            raise ProjectKnowledgeError("scalar_source_invalid", "Scalar object unit evidence must be a string.")
        unit = raw_unit.strip()
    else:
        raw_value = raw
    if isinstance(raw_value, bool):
        raise ProjectKnowledgeError("scalar_non_numeric", "Scalar result value is not numeric.")
    try:
        value = Decimal(str(raw_value))
    except InvalidOperation as exc:
        raise ProjectKnowledgeError("scalar_non_numeric", "Scalar result value is not numeric.") from exc
    if not value.is_finite():
        raise ProjectKnowledgeError("scalar_non_finite", "Scalar result value must be finite.")
    return format(value, "f"), unit


def admit_scalar_result(payload: ScalarAdmissionRequest) -> ScalarResultRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            run = connection.execute(
                "SELECT * FROM simulation_runs WHERE id = ? AND workspace_id = ?",
                (payload.run_id, payload.workspace_id),
            ).fetchone()
            if run is None:
                raise ProjectKnowledgeError("scalar_run_missing", "Simulation run was not found in workspace.")
            actual_digest = hashlib.sha256((run["output_payload"] or "").encode("utf-8")).hexdigest()
            if actual_digest != payload.source_payload_digest:
                raise ProjectKnowledgeError("scalar_source_digest_mismatch", "Run output payload digest does not match admission request.")
            extracted_value, extracted_unit = _extract_scalar_from_run_payload(run["output_payload"], payload.output_name)
            try:
                requested_value = Decimal(payload.value)
            except InvalidOperation as exc:
                raise ProjectKnowledgeError("scalar_non_numeric", "Scalar result value is not numeric.") from exc
            if not requested_value.is_finite():
                raise ProjectKnowledgeError("scalar_non_finite", "Scalar result value must be finite.")
            if format(requested_value, "f") != extracted_value:
                raise ProjectKnowledgeError("scalar_value_unproven", "Scalar value does not match deterministic run-output evidence.")
            if payload.unit.strip() != extracted_unit:
                raise ProjectKnowledgeError("scalar_unit_unproven", "Scalar unit does not match deterministic run-output evidence.")
            scalar_id = str(uuid4())
            now = utc_now()
            connection.execute(
                """
                INSERT INTO simulation_run_scalar_results (
                    id, run_id, output_name, value_text, unit, source_payload_digest,
                    extractor_id, extractor_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scalar_id,
                    payload.run_id,
                    payload.output_name,
                    extracted_value,
                    extracted_unit,
                    payload.source_payload_digest,
                    SCALAR_EXTRACTOR_ID,
                    SCALAR_EXTRACTOR_VERSION,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return ScalarResultRead(
        id=scalar_id,
        run_id=payload.run_id,
        output_name=payload.output_name,
        value=extracted_value,
        unit=extracted_unit,
        source_payload_digest=payload.source_payload_digest,
        extractor_id=SCALAR_EXTRACTOR_ID,
        extractor_version=SCALAR_EXTRACTOR_VERSION,
        created_at=now,
    )


def _comparison(operator: str, observed: Decimal, expected: Decimal) -> bool:
    return {
        "<": observed < expected,
        "<=": observed <= expected,
        ">": observed > expected,
        ">=": observed >= expected,
        "==": observed == expected,
    }[operator]


def _append_validation(
    connection: sqlite3.Connection,
    *,
    payload: ValidationRequest,
    requirement: sqlite3.Row,
    outcome: str,
    reason_code: str,
    source_run_id: str | None,
    source_scalar_id: str | None,
    source_payload_digest: str | None,
    observed: dict[str, object] | None,
    expected: dict[str, object] | None,
) -> ValidationRead:
    slot_values = (
        payload.working_revision_id,
        payload.requirement_id,
        str(requirement["criterion_rule_version"] or "free-text"),
        payload.validated_basis_digest,
    )
    rows = connection.execute(
        """
        SELECT v.* FROM project_knowledge_validation v
        WHERE v.working_revision_id = ? AND v.requirement_id = ? AND v.rule_version = ? AND v.validated_basis_digest = ?
          AND NOT EXISTS (
              SELECT 1 FROM project_knowledge_validation child
              WHERE child.supersedes_validation_id = v.id
          )
        """,
        slot_values,
    ).fetchall()
    if len(rows) > 1:
        raise ProjectKnowledgeError("validation_ambiguous", "Validation slot has multiple current rows.")
    predecessor = rows[0] if rows else None
    predecessor_id = None if predecessor is None else str(predecessor["id"])
    if payload.expected_predecessor_validation_id != predecessor_id:
        if payload.expected_predecessor_validation_id is not None or predecessor_id is not None:
            raise ProjectKnowledgeError("validation_predecessor_stale", "Current validation evidence changed since review.")
    validation_id = str(uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO project_knowledge_validation (
            id, workspace_id, working_revision_id, requirement_id, requirement_updated_at,
            rule_version, validated_basis_digest, applicability_set_digest,
            source_run_id, source_scalar_id, source_payload_digest,
            validator_id, validator_version, outcome, reason_code,
            observed_json, expected_json, supersedes_validation_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            validation_id,
            payload.workspace_id,
            payload.working_revision_id,
            payload.requirement_id,
            payload.expected_requirement_updated_at,
            str(requirement["criterion_rule_version"] or "free-text"),
            payload.validated_basis_digest,
            payload.applicability_set_digest,
            source_run_id,
            source_scalar_id,
            source_payload_digest,
            VALIDATOR_ID,
            VALIDATOR_VERSION,
            outcome,
            reason_code,
            None if observed is None else _canonical_json(observed),
            None if expected is None else _canonical_json(expected),
            predecessor_id,
            now,
        ),
    )
    return ValidationRead(
        id=validation_id,
        working_revision_id=payload.working_revision_id,
        requirement_id=payload.requirement_id,
        outcome=outcome,
        reason_code=reason_code,
        source_run_id=source_run_id,
        source_scalar_id=source_scalar_id,
        supersedes_validation_id=predecessor_id,
        created_at=now,
    )


def _run_binding_matches_revision(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_revision_id: str | None,
    working_revision_id: str,
    working_basis_digest: str,
) -> bool:
    del connection, workspace_id, working_basis_digest
    return bool(run_revision_id) and run_revision_id == working_revision_id


def evaluate_requirement(payload: ValidationRequest) -> ValidationRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            revision = connection.execute(
                "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ? AND state = 'working'",
                (payload.working_revision_id, payload.workspace_id),
            ).fetchone()
            if revision is None:
                raise ProjectKnowledgeError("working_revision_missing", "Working revision is not current.")
            if payload.validated_basis_digest != str(revision["projected_state_digest"]):
                raise ProjectKnowledgeError("validation_basis_stale", "Validation basis does not match the exact working revision.")
            _required, current_applicability_digest, applicability_diagnostics = _applicable_required_requirements(
                connection, payload.workspace_id, payload.working_revision_id
            )
            if applicability_diagnostics:
                raise ProjectKnowledgeError("applicability_ambiguous", "Requirement applicability is ambiguous.")
            if payload.applicability_set_digest != current_applicability_digest:
                raise ProjectKnowledgeError("applicability_stale", "Requirement applicability changed since validation was prepared.")
            requirement = connection.execute(
                "SELECT * FROM requirements WHERE id = ? AND workspace_id = ?",
                (payload.requirement_id, payload.workspace_id),
            ).fetchone()
            if requirement is None:
                raise ProjectKnowledgeError("criterion_missing", "Acceptance criterion was not found.")
            if str(requirement["updated_at"]) != payload.expected_requirement_updated_at:
                raise ProjectKnowledgeError("criterion_stale", "Acceptance criterion changed since review.")
            if requirement["basis_kind"] != "acceptance_criterion":
                result = _append_validation(
                    connection,
                    payload=payload,
                    requirement=requirement,
                    outcome="not_evaluable",
                    reason_code="unsupported_rule",
                    source_run_id=None,
                    source_scalar_id=None,
                    source_payload_digest=None,
                    observed=None,
                    expected=None,
                )
                connection.commit()
                return result
            operator = requirement["criterion_operator"]
            expected_value = requirement["criterion_expected_value"]
            output_name = requirement["criterion_output_name"]
            expected_unit = requirement["criterion_expected_unit"]
            rule_version = requirement["criterion_rule_version"]
            if operator not in {"<", "<=", ">", ">=", "=="} or not output_name or not expected_value or rule_version != CRITERION_RULE_VERSION:
                result = _append_validation(
                    connection,
                    payload=payload,
                    requirement=requirement,
                    outcome="not_evaluable",
                    reason_code="unsupported_rule",
                    source_run_id=None,
                    source_scalar_id=None,
                    source_payload_digest=None,
                    observed=None,
                    expected=None,
                )
                connection.commit()
                return result
            try:
                expected = Decimal(str(expected_value))
            except InvalidOperation:
                expected = Decimal("NaN")
            if not expected.is_finite():
                result = _append_validation(
                    connection,
                    payload=payload,
                    requirement=requirement,
                    outcome="not_evaluable",
                    reason_code="non_finite_or_non_numeric",
                    source_run_id=None,
                    source_scalar_id=None,
                    source_payload_digest=None,
                    observed=None,
                    expected={"operator": operator, "value": str(expected_value), "unit": expected_unit or ""},
                )
                connection.commit()
                return result

            candidates: list[sqlite3.Row]
            if payload.source_run_id:
                candidates = connection.execute(
                    """
                    SELECT s.*, r.workspace_id, r.output_payload, r.project_knowledge_revision_id
                    FROM simulation_run_scalar_results s
                    JOIN simulation_runs r ON r.id = s.run_id
                    WHERE s.run_id = ? AND s.output_name = ? AND r.workspace_id = ?
                    """,
                    (payload.source_run_id, output_name, payload.workspace_id),
                ).fetchall()
            else:
                candidates = connection.execute(
                    """
                    SELECT s.*, r.workspace_id, r.output_payload, r.project_knowledge_revision_id
                    FROM simulation_run_scalar_results s
                    JOIN simulation_runs r ON r.id = s.run_id
                    WHERE s.output_name = ? AND r.workspace_id = ?
                      AND r.project_knowledge_revision_id = ?
                    """,
                    (output_name, payload.workspace_id, payload.working_revision_id),
                ).fetchall()
            if len(candidates) != 1:
                result = _append_validation(
                    connection,
                    payload=payload,
                    requirement=requirement,
                    outcome="recomputation_required",
                    reason_code="missing_target",
                    source_run_id=payload.source_run_id,
                    source_scalar_id=None,
                    source_payload_digest=None,
                    observed=None,
                    expected={
                        "operator": operator,
                        "value": str(expected),
                        "unit": expected_unit or "",
                        "required_domain": "Process",
                        "working_revision_id": payload.working_revision_id,
                        "missing_output": str(output_name),
                    },
                )
                connection.commit()
                return result
            scalar = candidates[0]
            actual_payload_digest = hashlib.sha256((scalar["output_payload"] or "").encode("utf-8")).hexdigest()
            if actual_payload_digest != scalar["source_payload_digest"]:
                reason = "stale_target"
                outcome = "not_evaluable"
                observed = None
            elif not _run_binding_matches_revision(
                connection,
                workspace_id=payload.workspace_id,
                run_revision_id=scalar["project_knowledge_revision_id"],
                working_revision_id=payload.working_revision_id,
                working_basis_digest=str(revision["projected_state_digest"]),
            ):
                reason = "wrong_working_revision"
                outcome = "not_evaluable"
                observed = None
            elif str(scalar["unit"]).strip() != str(expected_unit or "").strip():
                reason = "unit_mismatch"
                outcome = "not_evaluable"
                observed = {"value": scalar["value_text"], "unit": scalar["unit"]}
            else:
                try:
                    observed_value = Decimal(str(scalar["value_text"]))
                except InvalidOperation:
                    observed_value = Decimal("NaN")
                if not observed_value.is_finite():
                    reason = "non_finite_or_non_numeric"
                    outcome = "not_evaluable"
                else:
                    passed = _comparison(str(operator), observed_value, expected)
                    reason = "comparison_true" if passed else "comparison_false"
                    outcome = "pass" if passed else "fail"
                observed = {"value": str(scalar["value_text"]), "unit": scalar["unit"]}
            result = _append_validation(
                connection,
                payload=payload,
                requirement=requirement,
                outcome=outcome,
                reason_code=reason,
                source_run_id=str(scalar["run_id"]),
                source_scalar_id=str(scalar["id"]),
                source_payload_digest=str(scalar["source_payload_digest"]),
                observed=observed,
                expected={"operator": operator, "value": str(expected), "unit": expected_unit or ""},
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def _cumulative_operations(
    connection: sqlite3.Connection,
    workspace_id: str,
    terminal_revision_id: str,
) -> tuple[list[ProjectKnowledgeOperation], list[str], str | None]:
    terminal = connection.execute(
        "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ?",
        (terminal_revision_id, workspace_id),
    ).fetchone()
    if terminal is None or terminal["state"] != "working":
        raise ProjectKnowledgeError("working_revision_missing", "Terminal working revision is not current.")
    chain, reconciled_base = _working_chain(
        connection,
        workspace_id,
        "working",
        terminal_revision_id,
    )
    operations: list[ProjectKnowledgeOperation] = []
    ids: list[str] = []
    for row in chain:
        ids.append(str(row["id"]))
        operations.extend(ProjectKnowledgeOperation.model_validate(item) for item in json.loads(row["change_set_json"]))
    return operations, ids, reconciled_base


def _applicable_required_requirements(
    connection: sqlite3.Connection,
    workspace_id: str,
    working_revision_id: str,
) -> tuple[list[str], str, list[str]]:
    operations, _ids, _base = _cumulative_operations(connection, workspace_id, working_revision_id)
    model_refs = {op.owner_id for op in operations if op.owner_kind == "model_spec" and op.owner_id}
    model_versions = {
        str(row["id"])
        for spec_id in model_refs
        for row in connection.execute(
            "SELECT id FROM model_versions WHERE workspace_id = ? AND model_spec_id = ?",
            (workspace_id, spec_id),
        ).fetchall()
    }
    required_rows = connection.execute(
        """
        SELECT id, updated_at FROM requirements
        WHERE workspace_id = ? AND status = 'active'
          AND basis_kind = 'acceptance_criterion' AND reconciliation_gate = 'required'
        ORDER BY id
        """,
        (workspace_id,),
    ).fetchall()
    applicable: list[str] = []
    relation_basis: list[dict[str, object]] = []
    diagnostics: list[str] = []
    for requirement in required_rows:
        relations = connection.execute(
            """
            SELECT * FROM requirement_applicability
            WHERE workspace_id = ? AND requirement_id = ? AND lifecycle_state = 'active'
            ORDER BY target_kind, target_id, id
            """,
            (workspace_id, requirement["id"]),
        ).fetchall()
        relation_basis.extend(dict(row) for row in relations)
        include = False
        workspace_rows = [row for row in relations if row["target_kind"] == "workspace" and row["target_id"] == workspace_id]
        if len(workspace_rows) > 1:
            diagnostics.append(f"applicability_ambiguous:{requirement['id']}")
            continue
        if workspace_rows:
            include = workspace_rows[0]["effect"] == "include"
        for spec_id in model_refs:
            spec_rows = [row for row in relations if row["target_kind"] == "model_spec" and row["target_id"] == spec_id]
            if len(spec_rows) > 1:
                diagnostics.append(f"applicability_ambiguous:{requirement['id']}:{spec_id}")
                continue
            if spec_rows:
                include = spec_rows[0]["effect"] == "include"
        for version_id in model_versions:
            version_rows = [row for row in relations if row["target_kind"] == "model_version" and row["target_id"] == version_id]
            if len(version_rows) > 1:
                diagnostics.append(f"applicability_ambiguous:{requirement['id']}:{version_id}")
                continue
            if version_rows:
                include = version_rows[0]["effect"] == "include"
        if include:
            applicable.append(str(requirement["id"]))
    return sorted(applicable), _digest(relation_basis), diagnostics


def _current_validations(
    connection: sqlite3.Connection,
    working_revision_id: str,
) -> tuple[list[sqlite3.Row], list[str]]:
    rows = connection.execute(
        "SELECT * FROM project_knowledge_validation WHERE working_revision_id = ? ORDER BY created_at, id",
        (working_revision_id,),
    ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    superseded: set[str] = set()
    diagnostics: list[str] = []
    for row in rows:
        predecessor = row["supersedes_validation_id"]
        if predecessor is None:
            continue
        predecessor = str(predecessor)
        previous = by_id.get(predecessor)
        if previous is None:
            diagnostics.append(f"validation_broken_predecessor:{row['id']}")
            continue
        slot = (row["requirement_id"], row["rule_version"], row["validated_basis_digest"])
        previous_slot = (previous["requirement_id"], previous["rule_version"], previous["validated_basis_digest"])
        if slot != previous_slot:
            diagnostics.append(f"validation_cross_slot:{row['id']}")
            continue
        superseded.add(predecessor)
    current = [row for row in rows if str(row["id"]) not in superseded]
    slot_counts: dict[tuple[str, str, str], int] = {}
    for row in current:
        slot = (str(row["requirement_id"]), str(row["rule_version"]), str(row["validated_basis_digest"]))
        slot_counts[slot] = slot_counts.get(slot, 0) + 1
    diagnostics.extend(f"validation_ambiguous:{slot[0]}" for slot, count in slot_counts.items() if count != 1)
    return current, diagnostics


def _validation_row_admissible(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    revision: sqlite3.Row,
    applicability_digest: str,
    row: sqlite3.Row,
) -> bool:
    requirement = connection.execute(
        "SELECT * FROM requirements WHERE id = ? AND workspace_id = ?",
        (row["requirement_id"], workspace_id),
    ).fetchone()
    if requirement is None:
        return False
    expected_rule_version = str(requirement["criterion_rule_version"] or "free-text")
    if (
        str(row["requirement_updated_at"]) != str(requirement["updated_at"])
        or str(row["rule_version"]) != expected_rule_version
        or str(row["validated_basis_digest"]) != str(revision["projected_state_digest"])
        or str(row["applicability_set_digest"]) != applicability_digest
        or str(row["validator_id"]) != VALIDATOR_ID
        or str(row["validator_version"]) != VALIDATOR_VERSION
    ):
        return False
    if row["source_run_id"] is None:
        return row["outcome"] in {"not_evaluable", "recomputation_required", "no_material_effect"}
    source = connection.execute(
        """
        SELECT r.project_knowledge_revision_id, r.output_payload, s.source_payload_digest
        FROM simulation_runs r
        JOIN simulation_run_scalar_results s ON s.run_id = r.id AND s.id = ?
        WHERE r.id = ? AND r.workspace_id = ?
        """,
        (row["source_scalar_id"], row["source_run_id"], workspace_id),
    ).fetchone()
    if source is None:
        return False
    actual_digest = hashlib.sha256((source["output_payload"] or "").encode("utf-8")).hexdigest()
    if actual_digest != str(row["source_payload_digest"]) or actual_digest != str(source["source_payload_digest"]):
        return False
    return _run_binding_matches_revision(
        connection,
        workspace_id=workspace_id,
        run_revision_id=source["project_knowledge_revision_id"],
        working_revision_id=str(revision["id"]),
        working_basis_digest=str(revision["projected_state_digest"]),
    )


def revalidation_status_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    working_revision_id: str,
) -> RevalidationRead:
    revision = connection.execute(
        "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ? AND state = 'working'",
        (working_revision_id, workspace_id),
    ).fetchone()
    if revision is None:
        raise ProjectKnowledgeError("working_revision_missing", "Working revision is not current.")
    required, applicability_digest, diagnostics = _applicable_required_requirements(connection, workspace_id, working_revision_id)
    current, validation_diagnostics = _current_validations(connection, working_revision_id)
    diagnostics.extend(validation_diagnostics)
    current_by_requirement: dict[str, sqlite3.Row] = {}
    for row in current:
        requirement_id = str(row["requirement_id"])
        if not _validation_row_admissible(
            connection,
            workspace_id=workspace_id,
            revision=revision,
            applicability_digest=applicability_digest,
            row=row,
        ):
            diagnostics.append(f"validation_stale:{requirement_id}:{row['id']}")
            continue
        current_by_requirement[requirement_id] = row
    blocking: list[str] = []
    known_fail: list[str] = []
    recompute: list[str] = []
    selected: list[dict[str, object]] = []
    for requirement_id in required:
        row = current_by_requirement.get(requirement_id)
        if row is None:
            blocking.append(requirement_id)
            continue
        selected.append(
            {
                "id": row["id"],
                "requirement_id": row["requirement_id"],
                "rule_version": row["rule_version"],
                "basis": row["validated_basis_digest"],
                "source_run_id": row["source_run_id"],
                "source_payload_digest": row["source_payload_digest"],
                "outcome": row["outcome"],
                "validator": [row["validator_id"], row["validator_version"]],
                "supersedes": row["supersedes_validation_id"],
            }
        )
        if row["outcome"] == "fail":
            known_fail.append(requirement_id)
        elif row["outcome"] == "recomputation_required":
            recompute.append(requirement_id)
            blocking.append(requirement_id)
        elif row["outcome"] != "pass":
            blocking.append(requirement_id)
    selected_digest = _digest(
        {
            "applicability_digest": applicability_digest,
            "rows": sorted(selected, key=lambda item: str(item["id"])),
        }
    )
    return RevalidationRead(
        working_revision_id=working_revision_id,
        complete=not diagnostics and not blocking,
        mandatory_requirement_ids=required,
        current_validation_ids=sorted(str(row["id"]) for row in current_by_requirement.values() if str(row["requirement_id"]) in required),
        blocking_requirement_ids=sorted(set(blocking)),
        known_fail_requirement_ids=sorted(set(known_fail)),
        recomputation_required=sorted(set(recompute)),
        selected_validation_set_digest=selected_digest,
        diagnostics=sorted(set(diagnostics)),
    )


def revalidation_status(workspace_id: str, working_revision_id: str) -> RevalidationRead:
    with open_sqlite_connection() as connection:
        return revalidation_status_from_connection(connection, workspace_id, working_revision_id)


def _snapshot_owner_manifest(connection: sqlite3.Connection, workspace_id: str) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    tables = {
        "requirement": "requirements",
        "decision": "decisions",
        "assumption": "assumptions",
        "parameter": "parameters",
        "model_spec": "model_specs",
    }
    for kind, table in tables.items():
        rows = connection.execute(f"SELECT * FROM {table} WHERE workspace_id = ? ORDER BY id", (workspace_id,)).fetchall()
        for row in rows:
            data = {key: row[key] for key in row.keys()}
            manifest.append(
                {
                    "kind": kind,
                    "id": row["id"],
                    "owner_revision_token": row["updated_at"],
                    "state": data,
                    "state_digest": _digest(data),
                }
            )
    return manifest


def _snapshot_edges(connection: sqlite3.Connection, workspace_id: str) -> tuple[list[dict[str, object]], bool]:
    graph = build_flowsheet_graph_from_connection(connection, workspace_id)
    manifest = [
        {
            "upstream_ref": edge.upstream_ref,
            "downstream_ref": edge.downstream_ref,
            "relation": edge.relation,
            "edge_class": edge.edge_class,
        }
        for edge in graph.edges
    ]
    complete = graph.is_acyclic and not graph.diagnostics.unresolved_references
    return manifest, complete


def _current_snapshot(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT s.*
        FROM project_knowledge_reconciled_snapshots s
        JOIN project_knowledge_reconciliation_requests r ON r.resulting_snapshot_id = s.id
        WHERE s.workspace_id = ? AND r.state = 'success'
        ORDER BY r.completed_at DESC, r.id DESC
        LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()


def reconcile(payload: ReconcileRequest) -> ReconcileRead:
    request_id = str(uuid4())
    request_digest: str | None = None
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_workspace(connection, payload.workspace_id)
            existing = connection.execute(
                "SELECT * FROM project_knowledge_reconciliation_requests WHERE workspace_id = ? AND idempotency_key = ?",
                (payload.workspace_id, payload.idempotency_key),
            ).fetchone()
            request_digest = _digest(
                {
                    "working_revision_id": payload.working_revision_id,
                    "target_snapshot_id": payload.expected_target_snapshot_id,
                    "target_digest": payload.expected_target_digest,
                    "known_fail_acknowledgement": payload.known_fail_acknowledgement,
                    "policy_identity": payload.policy_identity,
                    "selected_validation_set_digest": payload.expected_selected_validation_set_digest,
                }
            )
            if existing is not None:
                bound = (
                    str(existing["working_revision_id"]),
                    existing["target_snapshot_id"],
                    str(existing["target_digest"]),
                    existing["known_fail_acknowledgement"],
                    existing["policy_identity"],
                    str(existing["request_digest"]),
                    str(existing["selected_validation_set_digest"]),
                )
                requested = (
                    payload.working_revision_id,
                    payload.expected_target_snapshot_id,
                    payload.expected_target_digest,
                    payload.known_fail_acknowledgement,
                    payload.policy_identity,
                    request_digest,
                    payload.expected_selected_validation_set_digest,
                )
                if bound != requested:
                    raise ProjectKnowledgeError("reconciliation_idempotency_conflict", "Retry key is bound to different reconciliation values.")
                connection.rollback()
                return ReconcileRead(
                    request_id=str(existing["id"]),
                    state=str(existing["state"]),
                    outcome=existing["outcome"],
                    resulting_snapshot_id=existing["resulting_snapshot_id"],
                    canonical_id_map={} if not existing["canonical_id_map_json"] else json.loads(existing["canonical_id_map_json"]),
                    failure_code=existing["failure_code"],
                )

            operations, chain_ids, reconciled_base = _cumulative_operations(
                connection,
                payload.workspace_id,
                payload.working_revision_id,
            )
            terminal = connection.execute(
                "SELECT * FROM project_knowledge_revisions WHERE id = ? AND workspace_id = ?",
                (payload.working_revision_id, payload.workspace_id),
            ).fetchone()
            assert terminal is not None
            if str(terminal["projected_state_digest"]) != payload.expected_target_digest:
                raise ProjectKnowledgeError("target_digest_stale", "Working target digest changed since review.")
            current_snapshot = _current_snapshot(connection, payload.workspace_id)
            current_snapshot_id = None if current_snapshot is None else str(current_snapshot["id"])
            if current_snapshot_id != payload.expected_target_snapshot_id:
                raise ProjectKnowledgeError("target_snapshot_stale", "Reconciled target changed since reconciliation was prepared.")
            if reconciled_base is None:
                if current_snapshot_id is not None:
                    raise ProjectKnowledgeError("target_snapshot_stale", "Root working revision no longer targets the current reconciled state.")
            else:
                base_snapshot = connection.execute(
                    "SELECT id FROM project_knowledge_reconciled_snapshots WHERE workspace_id = ? AND reconciled_revision_id = ?",
                    (payload.workspace_id, reconciled_base),
                ).fetchone()
                if base_snapshot is None or str(base_snapshot["id"]) != current_snapshot_id:
                    raise ProjectKnowledgeError("target_snapshot_stale", "Working branch is based on a stale reconciled snapshot.")
            revalidation = revalidation_status_from_connection(connection, payload.workspace_id, payload.working_revision_id)
            if revalidation.selected_validation_set_digest != payload.expected_selected_validation_set_digest:
                raise ProjectKnowledgeError("validation_set_stale", "Current validation evidence changed since reconciliation was prepared.")
            if revalidation.diagnostics:
                raise ProjectKnowledgeError("validation_integrity_invalid", "Validation evidence contains unresolved integrity diagnostics.")
            unresolved = set(revalidation.blocking_requirement_ids)
            known_fail = set(revalidation.known_fail_requirement_ids)
            if unresolved:
                raise ProjectKnowledgeError("validation_incomplete", "Mandatory validation is incomplete.")
            if known_fail and not payload.known_fail_acknowledgement:
                raise ProjectKnowledgeError("known_fail_ack_required", "Known FAIL evidence requires explicit acknowledgement.")

            now = utc_now()
            connection.execute(
                """
                INSERT INTO project_knowledge_reconciliation_requests (
                    id, workspace_id, idempotency_key, working_revision_id,
                    target_snapshot_id, target_digest, known_fail_acknowledgement,
                    policy_identity, request_digest, selected_validation_set_digest,
                    state, outcome, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
                """,
                (
                    request_id,
                    payload.workspace_id,
                    payload.idempotency_key,
                    payload.working_revision_id,
                    payload.expected_target_snapshot_id,
                    payload.expected_target_digest,
                    payload.known_fail_acknowledgement,
                    payload.policy_identity,
                    request_digest,
                    payload.expected_selected_validation_set_digest,
                    now,
                ),
            )
            apply_result = ProjectBasisApplyService().apply(
                connection,
                workspace_id=payload.workspace_id,
                operations=operations,
            )
            owner_manifest = _snapshot_owner_manifest(connection, payload.workspace_id)
            edge_manifest, graph_complete = _snapshot_edges(connection, payload.workspace_id)
            if not graph_complete:
                raise ProjectKnowledgeError("snapshot_graph_incomplete", "Canonical graph is incomplete after proposed reconciliation.")
            snapshot_id = str(uuid4())
            owner_digest = _digest(owner_manifest)
            graph_digest = _digest(edge_manifest)
            connection.execute(
                """
                INSERT INTO project_knowledge_reconciled_snapshots (
                    id, workspace_id, reconciled_revision_id, parent_snapshot_id,
                    manifest_version, owner_manifest_json, owner_manifest_digest,
                    edge_manifest_json, graph_digest, graph_complete,
                    canonical_id_map_json, selected_validation_set_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    payload.workspace_id,
                    payload.working_revision_id,
                    payload.expected_target_snapshot_id,
                    SNAPSHOT_MANIFEST_VERSION,
                    _canonical_json(owner_manifest),
                    owner_digest,
                    _canonical_json(edge_manifest),
                    graph_digest,
                    1,
                    _canonical_json(apply_result.canonical_id_map),
                    payload.expected_selected_validation_set_digest,
                    now,
                ),
            )
            placeholders = ",".join("?" for _ in chain_ids)
            connection.execute(
                f"UPDATE project_knowledge_revisions SET state = 'reconciled', reconciled_snapshot_id = ? WHERE workspace_id = ? AND id IN ({placeholders}) AND state = 'working'",
                (snapshot_id, payload.workspace_id, *chain_ids),
            )
            connection.execute(
                """
                UPDATE project_knowledge_reconciliation_requests
                SET state = 'success', outcome = 'reconciled', resulting_snapshot_id = ?,
                    canonical_id_map_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (snapshot_id, _canonical_json(apply_result.canonical_id_map), now, request_id),
            )
            connection.commit()
            return ReconcileRead(
                request_id=request_id,
                state="success",
                outcome="reconciled",
                resulting_snapshot_id=snapshot_id,
                canonical_id_map=apply_result.canonical_id_map,
            )
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, ProjectKnowledgeError | ProjectBasisApplyError):
                code = exc.code
                message = exc.message
            else:
                code = "reconciliation_failed"
                message = str(exc)[:500]
    with open_sqlite_connection() as failure_connection:
        try:
            failure_connection.execute(
                """
                INSERT INTO project_knowledge_reconciliation_requests (
                    id, workspace_id, idempotency_key, working_revision_id,
                    target_snapshot_id, target_digest, known_fail_acknowledgement,
                    policy_identity, request_digest, selected_validation_set_digest,
                    state, outcome, failure_code, failure_detail, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'failed', 'rejected', ?, ?, ?, ?)
                ON CONFLICT(workspace_id, idempotency_key) DO NOTHING
                """,
                (
                    request_id,
                    payload.workspace_id,
                    payload.idempotency_key,
                    payload.working_revision_id,
                    payload.expected_target_snapshot_id,
                    payload.expected_target_digest,
                    payload.known_fail_acknowledgement,
                    payload.policy_identity,
                    request_digest or _digest({"working_revision_id": payload.working_revision_id, "failed": True}),
                    payload.expected_selected_validation_set_digest,
                    code,
                    message,
                    utc_now(),
                    utc_now(),
                ),
            )
            failure_connection.commit()
        except Exception:
            failure_connection.rollback()
    raise ProjectKnowledgeError(code, message)


def get_snapshot(workspace_id: str, snapshot_id: str) -> SnapshotRead:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT * FROM project_knowledge_reconciled_snapshots WHERE id = ? AND workspace_id = ?",
            (snapshot_id, workspace_id),
        ).fetchone()
    if row is None:
        raise ProjectKnowledgeError("snapshot_missing", "Reconciled snapshot was not found.")
    if str(row["manifest_version"]) != SNAPSHOT_MANIFEST_VERSION:
        raise ProjectKnowledgeError("snapshot_version_unsupported", "Reconciled snapshot manifest version is unsupported.")
    owner_manifest = _decode_snapshot_owner_manifest(row["owner_manifest_json"])
    edge_manifest = _decode_snapshot_edge_manifest(row["edge_manifest_json"])
    if _digest(owner_manifest) != row["owner_manifest_digest"] or _digest(edge_manifest) != row["graph_digest"]:
        raise ProjectKnowledgeError("snapshot_digest_mismatch", "Reconciled snapshot digest is invalid.")
    canonical_id_map = _decode_snapshot_canonical_id_map(row["canonical_id_map_json"])
    return SnapshotRead(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        reconciled_revision_id=str(row["reconciled_revision_id"]),
        parent_snapshot_id=row["parent_snapshot_id"],
        manifest_version=str(row["manifest_version"]),
        owner_manifest=owner_manifest,
        owner_manifest_digest=str(row["owner_manifest_digest"]),
        edge_manifest=edge_manifest,
        graph_digest=str(row["graph_digest"]),
        graph_complete=bool(row["graph_complete"]),
        canonical_id_map=canonical_id_map,
        selected_validation_set_digest=str(row["selected_validation_set_digest"]),
        created_at=str(row["created_at"]),
    )