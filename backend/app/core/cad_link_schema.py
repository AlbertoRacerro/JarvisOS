"""Additive schema for deterministic process-to-CAD links."""

CAD_LINK_SCHEMA_MIGRATION_RECORD = {
    "migration_id": "0015_cad_link_0",
    "name": "Deterministic bundled-047 M0 cylinder links to BLUECAD",
    "checksum": None,
}

CAD_LINK_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS bluecad_cad_links (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        source_simulation_run_id TEXT NOT NULL,
        source_runner_job_id TEXT NOT NULL,
        child_candidate_id TEXT NOT NULL UNIQUE,
        transformation_version TEXT NOT NULL,
        source_snapshot_json TEXT NOT NULL,
        source_snapshot_digest TEXT NOT NULL,
        source_model_identity_json TEXT NOT NULL,
        source_model_identity_digest TEXT NOT NULL,
        analysis_contract_digest TEXT,
        preview_digest TEXT NOT NULL,
        resolved_spec_digest TEXT NOT NULL,
        reconciliation_json TEXT NOT NULL,
        reconciliation_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (source_simulation_run_id) REFERENCES simulation_runs(id),
        FOREIGN KEY (source_runner_job_id) REFERENCES runner_jobs(id),
        FOREIGN KEY (child_candidate_id) REFERENCES bluecad_candidates(id),
        UNIQUE(workspace_id, preview_digest)
    )
    """,
]

CAD_LINK_SCHEMA_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_bluecad_cad_links_workspace_run ON bluecad_cad_links(workspace_id, source_simulation_run_id)",
]
