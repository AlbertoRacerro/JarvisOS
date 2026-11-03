from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from app.modules.memory.project_knowledge_owner import (
    promote_parameter_replacement_in_transaction,
    transition_proposal_in_transaction,
)
from app.modules.modeling.models import (
    AssumptionProjectUpdate,
    DecisionCreate,
    DecisionProjectUpdate,
    ModelSpecProjectUpdate,
    ParameterUpdate,
    RequirementCreate,
    RequirementProjectUpdate,
)
from app.modules.modeling.parameter_lifecycle import update_parameter_in_transaction
from app.modules.modeling.project_knowledge_owner import (
    create_decision_in_transaction,
    create_requirement_in_transaction,
    retire_decision_in_transaction,
    retire_requirement_in_transaction,
    update_assumption_in_transaction,
    update_decision_in_transaction,
    update_model_spec_in_transaction,
    update_requirement_in_transaction,
)
from app.modules.project_knowledge.models import ProjectKnowledgeOperation


class ProjectBasisApplyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ApplyResult:
    canonical_id_map: dict[str, str]
    touched_refs: tuple[str, ...]


_ALLOWED_PARAMETER_FIELDS = {
    "name",
    "symbol",
    "value",
    "unit",
    "value_status",
    "value_min",
    "value_max",
    "source_ref",
    "confidence",
    "notes",
}

_OWNER_TABLES = {
    "requirement": "requirements",
    "decision": "decisions",
    "assumption": "assumptions",
    "parameter": "parameters",
    "model_spec": "model_specs",
    "requirement_applicability": "requirement_applicability",
}


class ProjectBasisApplyService:
    """Dispatch a prevalidated cumulative mutation plan to canonical owners.

    This boundary deliberately owns sequencing only. Canonical mutation and audit
    rules remain in the modeling/MemoryStore owner primitives and all calls share
    the caller-owned SQLite transaction.
    """

    def apply(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        operations: list[ProjectKnowledgeOperation],
    ) -> ApplyResult:
        # Every operation against an already-canonical owner must still be bound to
        # the exact owner token that existed when the working chain was approved.
        # Validate all of those base tokens before the first mutation. Once a prior
        # operation in this same BEGIN IMMEDIATE transaction advances updated_at,
        # later cumulative operations may safely rebind only to that transaction-
        # local token; no external writer can intervene between the two checks.
        self._preflight_existing_tokens(connection, workspace_id, operations)

        canonical_id_map: dict[str, str] = {}
        touched: list[str] = []
        for operation in operations:
            resolved = self._resolve_operation(operation, canonical_id_map)
            resolved = self._rebind_transaction_token(
                connection,
                workspace_id=workspace_id,
                original=operation,
                resolved=resolved,
            )
            if resolved.operation_kind == "create":
                owner_id = self._create(connection, workspace_id, resolved)
                provisional = operation.provisional_ref
                if provisional is None:
                    raise ProjectBasisApplyError("provisional_ref_missing", "Create operation lacks provisional identity.")
                canonical_id_map[provisional] = owner_id
                touched.append(f"{resolved.owner_kind}:{owner_id}")
            elif resolved.operation_kind == "update":
                self._update(connection, workspace_id, resolved)
                touched.append(f"{resolved.owner_kind}:{resolved.owner_id}")
            elif resolved.operation_kind == "retire":
                self._retire(connection, workspace_id, resolved)
                touched.append(f"{resolved.owner_kind}:{resolved.owner_id}")
            elif resolved.operation_kind in {"set_applicability", "retire_applicability"}:
                self._applicability(connection, workspace_id, resolved, canonical_id_map)
                touched.append(f"requirement_applicability:{resolved.owner_id or resolved.operation_id}")
            else:
                raise ProjectBasisApplyError("operation_unsupported", "Operation kind is not supported by 112 V0.")
        return ApplyResult(canonical_id_map=canonical_id_map, touched_refs=tuple(touched))

    def _preflight_existing_tokens(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        operations: list[ProjectKnowledgeOperation],
    ) -> None:
        checked: dict[tuple[str, str], str] = {}
        for operation in operations:
            if operation.operation_kind not in {"update", "retire", "retire_applicability"}:
                continue
            owner_id = operation.owner_id
            expected = operation.expected_updated_at
            if not owner_id or owner_id.startswith("draft:"):
                continue
            if expected is None:
                raise ProjectBasisApplyError("owner_identity_missing", "Existing-owner operation has no revision token.")
            table = _OWNER_TABLES.get(operation.owner_kind)
            if table is None:
                raise ProjectBasisApplyError("owner_kind_unsupported", "Owner kind has no canonical revision token.")
            key = (operation.owner_kind, owner_id)
            prior_expected = checked.get(key)
            if prior_expected is not None and prior_expected != expected:
                raise ProjectBasisApplyError(
                    "owner_token_inconsistent",
                    "Cumulative operations for one owner are bound to different base revision tokens.",
                )
            row = connection.execute(
                f"SELECT updated_at FROM {table} WHERE id = ? AND workspace_id = ?",
                (owner_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ProjectBasisApplyError("owner_not_found", "Canonical owner was not found in workspace.")
            if str(row["updated_at"]) != expected:
                raise ProjectBasisApplyError("owner_stale", "Canonical owner changed since the working chain was approved.")
            checked[key] = expected

    def _current_owner_token(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        owner_kind: str,
        owner_id: str,
    ) -> str:
        table = _OWNER_TABLES.get(owner_kind)
        if table is None:
            raise ProjectBasisApplyError("owner_kind_unsupported", "Owner kind has no canonical revision token.")
        row = connection.execute(
            f"SELECT updated_at FROM {table} WHERE id = ? AND workspace_id = ?",
            (owner_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ProjectBasisApplyError("owner_not_found", "Canonical owner was not found in workspace.")
        return str(row["updated_at"])

    def _rebind_transaction_token(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        original: ProjectKnowledgeOperation,
        resolved: ProjectKnowledgeOperation,
    ) -> ProjectKnowledgeOperation:
        if resolved.operation_kind not in {"update", "retire", "retire_applicability"}:
            return resolved
        owner_id = resolved.owner_id
        if owner_id is None:
            return resolved
        current = self._current_owner_token(
            connection,
            workspace_id=workspace_id,
            owner_kind=resolved.owner_kind,
            owner_id=owner_id,
        )
        # A provisional owner did not exist in canonical truth at approval time;
        # its immutable ancestor create operation is the authority. Once created in
        # this same transaction, bind its child operation to the generated token.
        # For canonical owners, preflight above already proved the original base
        # token. A changed token here can therefore only be from an earlier local
        # operation in this transaction.
        if original.owner_id and original.owner_id.startswith("draft:") or resolved.expected_updated_at != current:
            data = resolved.model_dump()
            data["expected_updated_at"] = current
            return ProjectKnowledgeOperation.model_validate(data)
        return resolved

    def _resolve_operation(
        self,
        operation: ProjectKnowledgeOperation,
        canonical_id_map: dict[str, str],
    ) -> ProjectKnowledgeOperation:
        data = operation.model_dump()
        owner_id = data.get("owner_id")
        if isinstance(owner_id, str) and owner_id.startswith("draft:"):
            resolved = canonical_id_map.get(owner_id)
            if resolved is None:
                raise ProjectBasisApplyError("provisional_ref_unresolved", "Provisional owner reference is unresolved.")
            data["owner_id"] = resolved
        fields = dict(data.get("fields") or {})
        for key, value in list(fields.items()):
            if isinstance(value, str) and value.startswith("draft:"):
                if value not in canonical_id_map:
                    raise ProjectBasisApplyError("provisional_ref_unresolved", "Provisional field reference is unresolved.")
                fields[key] = canonical_id_map[value]
        data["fields"] = fields
        return ProjectKnowledgeOperation.model_validate(data)

    def _create(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        operation: ProjectKnowledgeOperation,
    ) -> str:
        if operation.owner_kind == "requirement":
            payload = RequirementCreate.model_validate(operation.fields)
            row = create_requirement_in_transaction(connection, workspace_id, payload)
            return str(row["id"])
        if operation.owner_kind == "decision":
            payload = DecisionCreate.model_validate(operation.fields)
            row = create_decision_in_transaction(connection, workspace_id, payload)
            return str(row["id"])
        raise ProjectBasisApplyError("create_owner_unsupported", "V0 create owner is unsupported.")

    def _update(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        operation: ProjectKnowledgeOperation,
    ) -> None:
        owner_id = operation.owner_id
        expected = operation.expected_updated_at
        if owner_id is None or expected is None:
            raise ProjectBasisApplyError("owner_identity_missing", "Update owner identity is incomplete.")
        if operation.owner_kind == "requirement":
            payload = RequirementProjectUpdate(
                workspace_id=workspace_id,
                expected_updated_at=expected,
                **operation.fields,
            )
            update_requirement_in_transaction(connection, owner_id, payload)
        elif operation.owner_kind == "decision":
            payload = DecisionProjectUpdate(
                workspace_id=workspace_id,
                expected_updated_at=expected,
                **operation.fields,
            )
            update_decision_in_transaction(connection, owner_id, payload)
        elif operation.owner_kind == "assumption":
            payload = AssumptionProjectUpdate(
                workspace_id=workspace_id,
                expected_updated_at=expected,
                **operation.fields,
            )
            update_assumption_in_transaction(connection, owner_id, payload)
        elif operation.owner_kind == "model_spec":
            payload = ModelSpecProjectUpdate(
                workspace_id=workspace_id,
                expected_updated_at=expected,
                **operation.fields,
            )
            update_model_spec_in_transaction(connection, owner_id, payload)
        elif operation.owner_kind == "parameter":
            unsupported = sorted(set(operation.fields) - _ALLOWED_PARAMETER_FIELDS)
            if unsupported:
                raise ProjectBasisApplyError("parameter_field_unsupported", f"Unsupported Parameter fields: {unsupported}")
            row = connection.execute(
                "SELECT supersedes_parameter_id, status, origin, updated_at FROM parameters WHERE id = ? AND workspace_id = ?",
                (owner_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ProjectBasisApplyError("owner_not_found", "Parameter not found in workspace.")
            if str(row["updated_at"]) != expected:
                raise ProjectBasisApplyError("owner_stale", "Parameter changed since the working revision was approved.")
            if operation.proposal_id:
                if operation.proposal_id != owner_id:
                    raise ProjectBasisApplyError("proposal_owner_mismatch", "Proposal identity does not match Parameter owner.")
                if row["supersedes_parameter_id"] is not None:
                    promote_parameter_replacement_in_transaction(
                        connection,
                        record_id=owner_id,
                        workspace_id=workspace_id,
                    )
                    return
                transition_proposal_in_transaction(
                    connection,
                    kind="parameter",
                    record_id=owner_id,
                    workspace_id=workspace_id,
                )
                return
            payload = ParameterUpdate(
                workspace_id=workspace_id,
                expected_updated_at=expected,
                **operation.fields,
            )
            # Preserve the canonical 098 dependency protection. A dependency-bearing
            # authority change must use an explicit replacement/freshness path rather
            # than silently bypassing the owner invariant during 112 reconciliation.
            update_parameter_in_transaction(connection, owner_id, payload, allow_dependency_change=False)
        else:
            raise ProjectBasisApplyError("update_owner_unsupported", "V0 update owner is unsupported.")

        if operation.proposal_id:
            if operation.proposal_id != owner_id or operation.owner_kind not in {"assumption", "decision"}:
                raise ProjectBasisApplyError("proposal_owner_mismatch", "Proposal identity does not match canonical owner.")
            transition_proposal_in_transaction(
                connection,
                kind=operation.owner_kind,
                record_id=owner_id,
                workspace_id=workspace_id,
            )

    def _retire(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        operation: ProjectKnowledgeOperation,
    ) -> None:
        if operation.owner_id is None or operation.expected_updated_at is None:
            raise ProjectBasisApplyError("owner_identity_missing", "Retirement owner identity is incomplete.")
        reason = operation.fields.get("reason")
        if operation.owner_kind == "requirement":
            retire_requirement_in_transaction(
                connection,
                operation.owner_id,
                workspace_id=workspace_id,
                expected_updated_at=operation.expected_updated_at,
                reason=None if reason is None else str(reason),
            )
            return
        if operation.owner_kind == "decision":
            retire_decision_in_transaction(
                connection,
                operation.owner_id,
                workspace_id=workspace_id,
                expected_updated_at=operation.expected_updated_at,
            )
            return
        raise ProjectBasisApplyError("retire_owner_unsupported", "Only Requirement and Global Decision retirement is supported.")

    def _applicability(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        operation: ProjectKnowledgeOperation,
        canonical_id_map: dict[str, str],
    ) -> None:
        fields = operation.fields
        requirement_id = fields.get("requirement_id")
        if isinstance(requirement_id, str) and requirement_id.startswith("draft:"):
            requirement_id = canonical_id_map.get(requirement_id)
        if not isinstance(requirement_id, str) or not requirement_id:
            raise ProjectBasisApplyError("applicability_requirement_missing", "Applicability requires an exact Requirement identity.")
        requirement = connection.execute(
            "SELECT id FROM requirements WHERE id = ? AND workspace_id = ?",
            (requirement_id, workspace_id),
        ).fetchone()
        if requirement is None:
            raise ProjectBasisApplyError("applicability_requirement_missing", "Applicability Requirement does not exist in workspace.")
        if operation.operation_kind == "set_applicability":
            target_kind = str(fields.get("target_kind") or "")
            target_id = str(fields.get("target_id") or "")
            effect = str(fields.get("effect") or "")
            if target_kind not in {"workspace", "model_spec", "model_version"} or effect not in {"include", "exclude"}:
                raise ProjectBasisApplyError("applicability_invalid", "Applicability target/effect is invalid.")
            if target_kind == "workspace":
                if target_id != workspace_id:
                    raise ProjectBasisApplyError("applicability_wrong_workspace", "Workspace applicability must target the same workspace.")
            else:
                table = "model_specs" if target_kind == "model_spec" else "model_versions"
                if connection.execute(
                    f"SELECT id FROM {table} WHERE id = ? AND workspace_id = ?",
                    (target_id, workspace_id),
                ).fetchone() is None:
                    raise ProjectBasisApplyError("applicability_target_missing", "Applicability target was not found in workspace.")
            active = connection.execute(
                """
                SELECT id FROM requirement_applicability
                WHERE workspace_id = ? AND requirement_id = ? AND target_kind = ? AND target_id = ?
                  AND lifecycle_state = 'active'
                """,
                (workspace_id, requirement_id, target_kind, target_id),
            ).fetchone()
            if active is not None:
                raise ProjectBasisApplyError("applicability_conflict", "An active applicability relation already exists at this specificity.")
            from app.modules.events.service import utc_now

            now = utc_now()
            connection.execute(
                """
                INSERT INTO requirement_applicability (
                    id, workspace_id, requirement_id, target_kind, target_id, effect,
                    lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (str(uuid4()), workspace_id, requirement_id, target_kind, target_id, effect, now, now),
            )
            return

        relation_id = operation.owner_id
        expected = operation.expected_updated_at
        if not isinstance(relation_id, str) or not isinstance(expected, str):
            raise ProjectBasisApplyError("applicability_identity_missing", "Retiring applicability requires relation id and revision token.")
        row = connection.execute(
            "SELECT * FROM requirement_applicability WHERE id = ? AND workspace_id = ?",
            (relation_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ProjectBasisApplyError("applicability_missing", "Applicability relation was not found.")
        if row["updated_at"] != expected or row["lifecycle_state"] != "active":
            raise ProjectBasisApplyError("applicability_stale", "Applicability relation changed since review.")
        from app.modules.events.service import utc_now

        now = utc_now()
        cursor = connection.execute(
            "UPDATE requirement_applicability SET lifecycle_state = 'retired', updated_at = ? WHERE id = ? AND workspace_id = ? AND updated_at = ? AND lifecycle_state = 'active'",
            (now, relation_id, workspace_id, expected),
        )
        if cursor.rowcount != 1:
            raise ProjectBasisApplyError("applicability_stale", "Applicability relation changed before commit.")
