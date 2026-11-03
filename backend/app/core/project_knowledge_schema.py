PROJECT_KNOWLEDGE_MIGRATION_RECORD = {
    "migration_id": "0017_project_knowledge_core",
    "name": "Project Knowledge working revisions and reconciliation",
    "checksum": None,
}

PROJECT_KNOWLEDGE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS project_knowledge_drafts (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        parent_revision_id TEXT,
        parent_kind TEXT NOT NULL CHECK (parent_kind IN ('reconciled', 'working')),
        revision_token TEXT NOT NULL,
        operations_json TEXT NOT NULL,
        preview_digest TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_knowledge_revisions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        parent_revision_id TEXT,
        parent_kind TEXT NOT NULL CHECK (parent_kind IN ('reconciled', 'working')),
        state TEXT NOT NULL CHECK (state IN ('working', 'discarded', 'superseded', 'reconciled')),
        change_set_digest TEXT NOT NULL,
        change_set_json TEXT NOT NULL,
        projected_state_digest TEXT NOT NULL,
        origin TEXT NOT NULL,
        created_at TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        superseded_by_revision_id TEXT,
        reconciled_snapshot_id TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (superseded_by_revision_id) REFERENCES project_knowledge_revisions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_knowledge_approval_requests (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        approval_request_key TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        draft_revision_token TEXT NOT NULL,
        parent_revision_id TEXT,
        parent_kind TEXT NOT NULL CHECK (parent_kind IN ('reconciled', 'working')),
        request_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'success', 'failed')),
        outcome TEXT,
        working_revision_id TEXT,
        failure_code TEXT,
        failure_detail TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (draft_id) REFERENCES project_knowledge_drafts(id),
        FOREIGN KEY (working_revision_id) REFERENCES project_knowledge_revisions(id),
        UNIQUE(workspace_id, approval_request_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requirement_applicability (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        target_kind TEXT NOT NULL CHECK (target_kind IN ('workspace', 'model_spec', 'model_version')),
        target_id TEXT NOT NULL,
        effect TEXT NOT NULL CHECK (effect IN ('include', 'exclude')),
        lifecycle_state TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle_state IN ('active', 'retired')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (requirement_id) REFERENCES requirements(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_run_scalar_results (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        output_name TEXT NOT NULL,
        value_text TEXT NOT NULL,
        unit TEXT NOT NULL,
        source_payload_digest TEXT NOT NULL,
        extractor_id TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES simulation_runs(id),
        UNIQUE(run_id, output_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_knowledge_validation (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        working_revision_id TEXT NOT NULL,
        requirement_id TEXT NOT NULL,
        requirement_updated_at TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        validated_basis_digest TEXT NOT NULL,
        applicability_set_digest TEXT NOT NULL,
        source_run_id TEXT,
        source_scalar_id TEXT,
        source_payload_digest TEXT,
        validator_id TEXT NOT NULL,
        validator_version TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('pass', 'fail', 'not_evaluable', 'no_material_effect', 'recomputation_required')),
        reason_code TEXT NOT NULL,
        observed_json TEXT,
        expected_json TEXT,
        supersedes_validation_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (working_revision_id) REFERENCES project_knowledge_revisions(id),
        FOREIGN KEY (requirement_id) REFERENCES requirements(id),
        FOREIGN KEY (source_run_id) REFERENCES simulation_runs(id),
        FOREIGN KEY (source_scalar_id) REFERENCES simulation_run_scalar_results(id),
        FOREIGN KEY (supersedes_validation_id) REFERENCES project_knowledge_validation(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_knowledge_reconciled_snapshots (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        reconciled_revision_id TEXT NOT NULL,
        parent_snapshot_id TEXT,
        manifest_version TEXT NOT NULL,
        owner_manifest_json TEXT NOT NULL,
        owner_manifest_digest TEXT NOT NULL,
        edge_manifest_json TEXT NOT NULL,
        graph_digest TEXT NOT NULL,
        graph_complete INTEGER NOT NULL CHECK (graph_complete IN (0, 1)),
        canonical_id_map_json TEXT NOT NULL,
        selected_validation_set_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (reconciled_revision_id) REFERENCES project_knowledge_revisions(id),
        FOREIGN KEY (parent_snapshot_id) REFERENCES project_knowledge_reconciled_snapshots(id),
        UNIQUE(workspace_id, reconciled_revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_knowledge_reconciliation_requests (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        working_revision_id TEXT NOT NULL,
        target_snapshot_id TEXT,
        target_digest TEXT NOT NULL,
        known_fail_acknowledgement TEXT,
        policy_identity TEXT,
        request_digest TEXT NOT NULL,
        selected_validation_set_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'success', 'failed')),
        outcome TEXT,
        resulting_snapshot_id TEXT,
        canonical_id_map_json TEXT,
        failure_code TEXT,
        failure_detail TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (working_revision_id) REFERENCES project_knowledge_revisions(id),
        FOREIGN KEY (target_snapshot_id) REFERENCES project_knowledge_reconciled_snapshots(id),
        FOREIGN KEY (resulting_snapshot_id) REFERENCES project_knowledge_reconciled_snapshots(id),
        UNIQUE(workspace_id, idempotency_key)
    )
    """,
)

PROJECT_KNOWLEDGE_MIGRATION_STATEMENTS = (
    "ALTER TABLE requirements ADD COLUMN basis_kind TEXT NOT NULL DEFAULT 'requirement'",
    "ALTER TABLE requirements ADD COLUMN reconciliation_gate TEXT NOT NULL DEFAULT 'advisory'",
    "ALTER TABLE requirements ADD COLUMN criterion_output_name TEXT",
    "ALTER TABLE requirements ADD COLUMN criterion_operator TEXT",
    "ALTER TABLE requirements ADD COLUMN criterion_expected_value TEXT",
    "ALTER TABLE requirements ADD COLUMN criterion_expected_unit TEXT",
    "ALTER TABLE requirements ADD COLUMN criterion_rule_version TEXT",
    "ALTER TABLE decisions ADD COLUMN basis_lifecycle_state TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE simulation_runs ADD COLUMN project_knowledge_revision_id TEXT",
)

PROJECT_KNOWLEDGE_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_pk_drafts_workspace ON project_knowledge_drafts(workspace_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_pk_revisions_workspace ON project_knowledge_revisions(workspace_id, accepted_at)",
    "CREATE INDEX IF NOT EXISTS idx_pk_revisions_parent ON project_knowledge_revisions(workspace_id, parent_kind, parent_revision_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_requirement_applicability_active_target ON requirement_applicability(workspace_id, requirement_id, target_kind, target_id) WHERE lifecycle_state = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_requirement_applicability_target ON requirement_applicability(workspace_id, target_kind, target_id, lifecycle_state)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pk_validation_superseded_once ON project_knowledge_validation(supersedes_validation_id) WHERE supersedes_validation_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_pk_validation_slot ON project_knowledge_validation(working_revision_id, requirement_id, rule_version, validated_basis_digest)",
    "CREATE INDEX IF NOT EXISTS idx_pk_reconcile_workspace ON project_knowledge_reconciliation_requests(workspace_id, created_at)",
)
