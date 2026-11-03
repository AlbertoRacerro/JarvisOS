"""BLUECAD-owned visual references to immutable Studio geometry generations."""

BLUECAD_STUDIO_SCHEMA_MIGRATION_RECORD = {
    "migration_id": "0024_bluecad_studio_visual_references",
    "name": "BLUECAD Studio visual references",
    "checksum": None,
}

BLUECAD_STUDIO_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS bluecad_studio_references (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        simulation_run_id TEXT NOT NULL,
        model_spec_id TEXT NOT NULL,
        model_version_id TEXT NOT NULL,
        version_label TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        input_payload TEXT NOT NULL,
        glb_sha256 TEXT NOT NULL,
        step_sha256 TEXT NOT NULL,
        stl_sha256 TEXT NOT NULL,
        translation_mm_json TEXT NOT NULL,
        rotation_deg_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (candidate_id) REFERENCES bluecad_candidates(id),
        FOREIGN KEY (simulation_run_id) REFERENCES simulation_runs(id),
        FOREIGN KEY (model_version_id) REFERENCES model_versions(id),
        UNIQUE(candidate_id, simulation_run_id)
    )
    """,
)

BLUECAD_STUDIO_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_bluecad_studio_references_candidate "
    "ON bluecad_studio_references(workspace_id, candidate_id, created_at)",
)
