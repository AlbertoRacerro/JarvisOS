SCHEMA_BASELINE_MIGRATION_ID = "0001_foundation_baseline"
SCHEMA_BASELINE_MIGRATION_NAME = "Foundation schema through Python Runner V0"
SCHEMA_DATA_INFRASTRUCTURE_MIGRATION_ID = "0002_data_infrastructure_hardening"
SCHEMA_DATA_INFRASTRUCTURE_MIGRATION_NAME = "0E-B schema tracking, indexes, and artifact readback"
SCHEMA_AI_POLICY_MIGRATION_ID = "0003_ai_policy_mode_foundation"
SCHEMA_AI_POLICY_MIGRATION_NAME = "0E-D3 pragmatic AI policy mode foundation"
SCHEMA_ENGINEERING_RECORD_MIGRATION_ID = "0004_engineering_record_schema_freeze"
SCHEMA_ENGINEERING_RECORD_MIGRATION_NAME = "Parameter, assumption, and requirement schema freeze"
SCHEMA_MEMORYSTORE_MIGRATION_ID = "0005_memorystore_proposal_boundary"
SCHEMA_MEMORYSTORE_MIGRATION_NAME = "MemoryStore proposal provenance boundary"
SCHEMA_RUNNER_IMPLEMENTATION_KIND_MIGRATION_ID = "0006_runner_implementation_kind"
SCHEMA_RUNNER_IMPLEMENTATION_KIND_MIGRATION_NAME = "Runner bluecad_l2_v0 implementation kind dispatch"
SCHEMA_CONTEXT_RECORDS_FTS_MIGRATION_ID = "0007_context_records_fts"
SCHEMA_CONTEXT_RECORDS_FTS_MIGRATION_NAME = "Context pack FTS index and triggers"
SCHEMA_EVIDENCE_RECORDS_MIGRATION_ID = "0008_evidence_records"
SCHEMA_EVIDENCE_RECORDS_MIGRATION_NAME = "Evidence records for BLUECAD outcomes"
SCHEMA_SENSITIVITY_CONTEXT_MIGRATION_ID = "0009_sensitivity_context_foundation"
SCHEMA_SENSITIVITY_CONTEXT_MIGRATION_NAME = "IP egress sensitivity labels and sanitized derivatives"
SCHEMA_MODEL_INPUT_CONTRACT_MIGRATION_ID = "0012_model_input_contract"
SCHEMA_MODEL_INPUT_CONTRACT_MIGRATION_NAME = "Model-version input contracts and scenario DOF metadata"
SCHEMA_FRESHNESS_INVALIDATION_MIGRATION_ID = "0013_freshness_invalidation"
SCHEMA_FRESHNESS_INVALIDATION_MIGRATION_NAME = "Parameter replacement and freshness invalidation overlay"
SCHEMA_DESIGN_PROJECT_VERSIONS_MIGRATION_ID = "0026_design_project_versions"
SCHEMA_DESIGN_PROJECT_VERSIONS_MIGRATION_NAME = "Design project versions and components"
SCHEMA_CONVERSATION_INTAKE_MIGRATION_ID = "0027_conversation_intake"
SCHEMA_CONVERSATION_INTAKE_MIGRATION_NAME = "External conversation intake receipts"
SCHEMA_BUBU_TURN_EXTRAS_MIGRATION_ID = "0028_bubu_turn_extras"
SCHEMA_BUBU_TURN_EXTRAS_MIGRATION_NAME = "Bubu page description and screenshot references per AI thread turn"
CURRENT_SCHEMA_MIGRATION_ID = SCHEMA_BUBU_TURN_EXTRAS_MIGRATION_ID
CURRENT_SCHEMA_MIGRATION_NAME = SCHEMA_BUBU_TURN_EXTRAS_MIGRATION_NAME

SCHEMA_MIGRATION_RECORDS = [
    {
        "migration_id": SCHEMA_BASELINE_MIGRATION_ID,
        "name": SCHEMA_BASELINE_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_DATA_INFRASTRUCTURE_MIGRATION_ID,
        "name": SCHEMA_DATA_INFRASTRUCTURE_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_AI_POLICY_MIGRATION_ID,
        "name": SCHEMA_AI_POLICY_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_ENGINEERING_RECORD_MIGRATION_ID,
        "name": SCHEMA_ENGINEERING_RECORD_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_MEMORYSTORE_MIGRATION_ID,
        "name": SCHEMA_MEMORYSTORE_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_RUNNER_IMPLEMENTATION_KIND_MIGRATION_ID,
        "name": SCHEMA_RUNNER_IMPLEMENTATION_KIND_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_CONTEXT_RECORDS_FTS_MIGRATION_ID,
        "name": SCHEMA_CONTEXT_RECORDS_FTS_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_EVIDENCE_RECORDS_MIGRATION_ID,
        "name": SCHEMA_EVIDENCE_RECORDS_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_SENSITIVITY_CONTEXT_MIGRATION_ID,
        "name": SCHEMA_SENSITIVITY_CONTEXT_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_MODEL_INPUT_CONTRACT_MIGRATION_ID,
        "name": SCHEMA_MODEL_INPUT_CONTRACT_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_FRESHNESS_INVALIDATION_MIGRATION_ID,
        "name": SCHEMA_FRESHNESS_INVALIDATION_MIGRATION_NAME,
        "checksum": None,
    },
    {
        "migration_id": SCHEMA_DESIGN_PROJECT_VERSIONS_MIGRATION_ID,
        "name": SCHEMA_DESIGN_PROJECT_VERSIONS_MIGRATION_NAME,
        "checksum": None,
    },
]

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        migration_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        checksum TEXT,
        status TEXT NOT NULL DEFAULT 'applied'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspaces (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS design_project_versions (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        data_workspace_id TEXT NOT NULL UNIQUE,
        parent_version_id TEXT,
        label TEXT NOT NULL,
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL,
        source_mode TEXT,
        FOREIGN KEY (project_id) REFERENCES workspaces(id),
        FOREIGN KEY (data_workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (parent_version_id) REFERENCES design_project_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS design_components (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        version_id TEXT NOT NULL,
        parent_id TEXT,
        name TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (version_id) REFERENCES design_project_versions(id),
        FOREIGN KEY (parent_id) REFERENCES design_components(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS design_component_units (
        component_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        UNIQUE (version_id, draft_id, unit_id),
        FOREIGN KEY (component_id) REFERENCES design_components(id),
        FOREIGN KEY (version_id) REFERENCES design_project_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS design_component_geometry (
        component_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        source_draft_revision TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (component_id) REFERENCES design_components(id),
        FOREIGN KEY (candidate_id) REFERENCES bluecad_candidates(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS design_component_models (
        component_id TEXT NOT NULL,
        model_version_id TEXT NOT NULL,
        node TEXT NOT NULL,
        category TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        attached_at TEXT NOT NULL,
        PRIMARY KEY (component_id, model_version_id),
        FOREIGN KEY (component_id) REFERENCES design_components(id),
        FOREIGN KEY (model_version_id) REFERENCES model_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS design_component_study_templates (
        component_id TEXT NOT NULL,
        regime TEXT NOT NULL,
        contract_json TEXT NOT NULL,
        template_digest TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        PRIMARY KEY (component_id, regime),
        FOREIGN KEY (component_id) REFERENCES design_components(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        workspace_id TEXT,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT,
        payload TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        mime_type TEXT,
        sha256 TEXT,
        source_ref TEXT,
        status TEXT NOT NULL DEFAULT 'registered',
        created_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_specs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        title TEXT NOT NULL,
        engineering_question TEXT NOT NULL,
        scope TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        maturity_status TEXT NOT NULL DEFAULT 'draft',
        assumptions_summary TEXT,
        inputs_summary TEXT,
        outputs_summary TEXT,
        raw_payload TEXT,
        schema_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assumptions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        statement TEXT NOT NULL,
        scope TEXT,
        confidence TEXT,
        status TEXT NOT NULL DEFAULT 'proposed',
        source_ref TEXT,
        origin TEXT NOT NULL DEFAULT 'user',
        source_ai_job_id TEXT,
        promoted_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requirements (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        statement TEXT NOT NULL,
        rationale TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        notes TEXT,
        schema_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parameters (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        name TEXT NOT NULL,
        symbol TEXT,
        value TEXT,
        unit TEXT NOT NULL DEFAULT 'unspecified',
        value_status TEXT NOT NULL DEFAULT 'candidate',
        value_min REAL,
        value_max REAL,
        source_ref TEXT,
        confidence REAL,
        status TEXT NOT NULL DEFAULT 'draft',
        origin TEXT NOT NULL DEFAULT 'user',
        source_ai_job_id TEXT,
        promoted_at TEXT,
        supersedes_parameter_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (supersedes_parameter_id) REFERENCES parameters(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS freshness_invalidations (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        superseded_parameter_id TEXT NOT NULL,
        replacement_parameter_id TEXT NOT NULL,
        source_graph_digest TEXT NOT NULL,
        affected_count INTEGER NOT NULL,
        unresolved_diagnostic_count INTEGER NOT NULL,
        cycle_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (superseded_parameter_id) REFERENCES parameters(id),
        FOREIGN KEY (replacement_parameter_id) REFERENCES parameters(id),
        UNIQUE(workspace_id, superseded_parameter_id),
        UNIQUE(workspace_id, replacement_parameter_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS freshness_marks (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        invalidation_id TEXT NOT NULL,
        record_ref TEXT NOT NULL,
        record_kind TEXT NOT NULL,
        record_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        path_json TEXT NOT NULL,
        path_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (invalidation_id) REFERENCES freshness_invalidations(id),
        UNIQUE(invalidation_id, record_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_versions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        model_spec_id TEXT NOT NULL,
        version_label TEXT NOT NULL,
        implementation_artifact_id TEXT,
        implementation_kind TEXT NOT NULL DEFAULT 'batch_growth_v0',
        status TEXT NOT NULL DEFAULT 'draft',
        changelog TEXT,
        input_contract_payload TEXT,
        input_contract_sha256 TEXT,
        created_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (model_spec_id) REFERENCES model_specs(id),
        FOREIGN KEY (implementation_artifact_id) REFERENCES artifacts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS studio_drafts (
        model_spec_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        contract_json TEXT NOT NULL,
        source TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        annotations TEXT NOT NULL DEFAULT '',
        assumption_notes TEXT NOT NULL DEFAULT '',
        annotations_source_sha256 TEXT,
        test_cases_json TEXT NOT NULL DEFAULT '[]',
        test_report_json TEXT,
        revision_of_version_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (model_spec_id) REFERENCES model_specs(id),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (revision_of_version_id) REFERENCES model_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS studio_published_versions (
        version_id TEXT PRIMARY KEY,
        model_spec_id TEXT NOT NULL,
        contract_json TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (version_id) REFERENCES model_versions(id),
        FOREIGN KEY (model_spec_id) REFERENCES model_specs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS studio_procedure_links (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        schedule_id TEXT NOT NULL,
        scenario_id TEXT NOT NULL,
        model_spec_id TEXT NOT NULL,
        version_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        parameter_values_json TEXT NOT NULL,
        compiled_sha256 TEXT NOT NULL,
        applied_draft_revision TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        UNIQUE (workspace_id, draft_id, schedule_id),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (version_id) REFERENCES model_versions(id),
        FOREIGN KEY (model_spec_id) REFERENCES model_specs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS studio_procedure_runs (
        job_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        draft_id TEXT NOT NULL,
        link_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS simulation_runs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        model_version_id TEXT,
        run_label TEXT,
        status TEXT NOT NULL DEFAULT 'planned',
        input_payload TEXT,
        parameter_payload TEXT,
        output_payload TEXT,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (model_version_id) REFERENCES model_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runner_jobs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        simulation_run_id TEXT NOT NULL UNIQUE,
        runner_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        script_path TEXT NOT NULL,
        script_sha256 TEXT NOT NULL,
        implementation_kind TEXT NOT NULL DEFAULT 'batch_growth_v0',
        command_json TEXT,
        environment_json TEXT,
        working_dir TEXT NOT NULL,
        input_file TEXT,
        output_dir TEXT NOT NULL,
        timeout_seconds INTEGER NOT NULL,
        max_stdout_bytes INTEGER NOT NULL,
        max_stderr_bytes INTEGER NOT NULL,
        max_output_json_bytes INTEGER NOT NULL,
        max_artifact_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (simulation_run_id) REFERENCES simulation_runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_logs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        simulation_run_id TEXT NOT NULL,
        stream TEXT NOT NULL,
        content TEXT NOT NULL,
        truncated INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (simulation_run_id) REFERENCES simulation_runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_artifacts (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        simulation_run_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (simulation_run_id) REFERENCES simulation_runs(id),
        FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        title TEXT NOT NULL,
        decision_text TEXT NOT NULL,
        rationale TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        linked_run_id TEXT,
        origin TEXT NOT NULL DEFAULT 'user',
        source_ai_job_id TEXT,
        promoted_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (linked_run_id) REFERENCES simulation_runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_jobs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,
        task_kind TEXT NOT NULL,
        requested_route_class TEXT,
        selected_route_class TEXT,
        provider_id TEXT,
        model_id TEXT,
        route_reason_json TEXT NOT NULL,
        prompt_digest TEXT,
        context_digest TEXT,
        context_sources_json TEXT,
        output_digest TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_estimate REAL,
        usage_source TEXT CHECK (usage_source IN ('actual', 'estimated', 'mixed')),
        latency_ms INTEGER,
        error_type TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bluecad_candidates (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        brief_text TEXT NOT NULL,
        brief_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        parked_reason TEXT,
        spec_artifact_id TEXT,
        glb_artifact_id TEXT,
        report_artifact_id TEXT,
        promoted_decision_id TEXT,
        origin TEXT NOT NULL DEFAULT 'ai',
        parent_candidate_id TEXT,
        loop_config_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (spec_artifact_id) REFERENCES artifacts(id),
        FOREIGN KEY (glb_artifact_id) REFERENCES artifacts(id),
        FOREIGN KEY (report_artifact_id) REFERENCES artifacts(id),
        FOREIGN KEY (promoted_decision_id) REFERENCES decisions(id),
        FOREIGN KEY (parent_candidate_id) REFERENCES bluecad_candidates(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bluecad_attempts (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        attempt_no INTEGER NOT NULL,
        route_class TEXT NOT NULL,
        proposal_ai_job_id TEXT,
        proposal_outcome TEXT NOT NULL,
        build_outcome TEXT,
        validation_verdict TEXT,
        spec_artifact_id TEXT,
        report_artifact_id TEXT,
        manifest_artifact_id TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error_detail_json TEXT,
        FOREIGN KEY (candidate_id) REFERENCES bluecad_candidates(id),
        FOREIGN KEY (proposal_ai_job_id) REFERENCES ai_jobs(id),
        FOREIGN KEY (spec_artifact_id) REFERENCES artifacts(id),
        FOREIGN KEY (report_artifact_id) REFERENCES artifacts(id),
        FOREIGN KEY (manifest_artifact_id) REFERENCES artifacts(id),
        UNIQUE(candidate_id, attempt_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_records (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        verdict TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        source_run_id TEXT,
        candidate_id TEXT,
        attempt_id TEXT,
        report_artifact_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (source_run_id) REFERENCES simulation_runs(id),
        FOREIGN KEY (candidate_id) REFERENCES bluecad_candidates(id),
        FOREIGN KEY (attempt_id) REFERENCES bluecad_attempts(id),
        FOREIGN KEY (report_artifact_id) REFERENCES artifacts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_settings (
        id TEXT PRIMARY KEY,
        policy_mode TEXT NOT NULL DEFAULT 'STRICT_IP',
        monthly_api_budget_usd REAL NOT NULL DEFAULT 0,
        api_spend_month_to_date_usd REAL NOT NULL DEFAULT 0,
        paid_ai_enabled INTEGER NOT NULL DEFAULT 0,
        default_ai_provider TEXT NOT NULL DEFAULT 'fake',
        default_ai_model TEXT NOT NULL DEFAULT 'fake-modeling-draft-v1',
        provider_mode TEXT NOT NULL DEFAULT 'fake',
        use_fake_provider_when_budget_zero INTEGER NOT NULL DEFAULT 1,
        scaleway_enabled INTEGER NOT NULL DEFAULT 0,
        scaleway_token_cap INTEGER NOT NULL DEFAULT 0,
        scaleway_tokens_month_to_date INTEGER NOT NULL DEFAULT 0,
        scaleway_smoke_test_enabled INTEGER NOT NULL DEFAULT 0,
        scaleway_live_smoke_test_enabled INTEGER NOT NULL DEFAULT 0,
        scaleway_monthly_token_cap INTEGER NOT NULL DEFAULT 500000,
        scaleway_hard_stop_token_cap INTEGER NOT NULL DEFAULT 800000,
        scaleway_free_tier_reference_tokens INTEGER NOT NULL DEFAULT 1000000,
        scaleway_input_tokens_month_to_date INTEGER NOT NULL DEFAULT 0,
        scaleway_output_tokens_month_to_date INTEGER NOT NULL DEFAULT 0,
        smoke_test_mode_enabled INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
]

SCHEMA_MIGRATION_STATEMENTS = [
    "ALTER TABLE ai_settings ADD COLUMN policy_mode TEXT NOT NULL DEFAULT 'STRICT_IP'",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_smoke_test_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_live_smoke_test_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_monthly_token_cap INTEGER NOT NULL DEFAULT 500000",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_hard_stop_token_cap INTEGER NOT NULL DEFAULT 800000",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_free_tier_reference_tokens INTEGER NOT NULL DEFAULT 1000000",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_input_tokens_month_to_date INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_output_tokens_month_to_date INTEGER NOT NULL DEFAULT 0",
    # Repair (Stage 3-fix): these two columns shipped in CREATE TABLE ai_settings
    # but were omitted from the catch-up migrations above, so a legacy DB created
    # before they existed could never gain them on upgrade. Definitions mirror the
    # current CREATE exactly. Idempotent on fresh DBs via duplicate-column swallow.
    "ALTER TABLE ai_settings ADD COLUMN scaleway_token_cap INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ai_settings ADD COLUMN scaleway_tokens_month_to_date INTEGER NOT NULL DEFAULT 0",
    # POS-2: source manifest for context blocks. Legacy DBs created at POS-1 have
    # ai_jobs without this column; this upgrades them in place.
    "ALTER TABLE ai_jobs ADD COLUMN context_sources_json TEXT",
    "ALTER TABLE parameters ADD COLUMN unit TEXT NOT NULL DEFAULT 'unspecified'",
    "ALTER TABLE parameters ADD COLUMN value_status TEXT NOT NULL DEFAULT 'candidate'",
    "ALTER TABLE parameters ADD COLUMN value_min REAL",
    "ALTER TABLE parameters ADD COLUMN value_max REAL",
    "ALTER TABLE parameters ADD COLUMN source_ref TEXT",
    "ALTER TABLE assumptions ADD COLUMN status TEXT NOT NULL DEFAULT 'proposed'",
    "ALTER TABLE assumptions ADD COLUMN confidence TEXT",
    "ALTER TABLE assumptions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'",
    "ALTER TABLE assumptions ADD COLUMN source_ai_job_id TEXT",
    "ALTER TABLE assumptions ADD COLUMN promoted_at TEXT",
    "ALTER TABLE parameters ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'",
    "ALTER TABLE parameters ADD COLUMN source_ai_job_id TEXT",
    "ALTER TABLE parameters ADD COLUMN promoted_at TEXT",
    "ALTER TABLE decisions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'",
    "ALTER TABLE decisions ADD COLUMN source_ai_job_id TEXT",
    "ALTER TABLE decisions ADD COLUMN promoted_at TEXT",
    # RUNNER-EXT-1 (spec 016): dispatch key for the scoped bluecad_l2_v0 runner kind.
    "ALTER TABLE model_versions ADD COLUMN implementation_kind TEXT NOT NULL DEFAULT 'batch_growth_v0'",
    "ALTER TABLE runner_jobs ADD COLUMN implementation_kind TEXT NOT NULL DEFAULT 'batch_growth_v0'",
    "ALTER TABLE model_versions ADD COLUMN input_contract_payload TEXT",
    "ALTER TABLE model_versions ADD COLUMN input_contract_sha256 TEXT",
    "ALTER TABLE parameters ADD COLUMN supersedes_parameter_id TEXT",
    "ALTER TABLE design_project_versions ADD COLUMN source_mode TEXT",
]

SCHEMA_FTS_STATEMENTS = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS context_records_fts USING fts5(
        kind UNINDEXED,
        record_id UNINDEXED,
        workspace_id UNINDEXED,
        title,
        body,
        notes
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_decisions_context_fts_insert AFTER INSERT ON decisions BEGIN
        INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes)
        VALUES ('decision', new.id, new.workspace_id, new.title, COALESCE(new.decision_text, '') || ' ' || COALESCE(new.rationale, ''), new.notes);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_decisions_context_fts_update AFTER UPDATE ON decisions BEGIN
        DELETE FROM context_records_fts WHERE kind = 'decision' AND record_id = old.id;
        INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes)
        VALUES ('decision', new.id, new.workspace_id, new.title, COALESCE(new.decision_text, '') || ' ' || COALESCE(new.rationale, ''), new.notes);
    END
    """,
    "CREATE TRIGGER IF NOT EXISTS trg_decisions_context_fts_delete AFTER DELETE ON decisions BEGIN DELETE FROM context_records_fts WHERE kind = 'decision' AND record_id = old.id; END",
    "CREATE TRIGGER IF NOT EXISTS trg_assumptions_context_fts_insert AFTER INSERT ON assumptions BEGIN INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes) VALUES ('assumption', new.id, new.workspace_id, '', new.statement, new.notes); END",
    "CREATE TRIGGER IF NOT EXISTS trg_assumptions_context_fts_update AFTER UPDATE ON assumptions BEGIN DELETE FROM context_records_fts WHERE kind = 'assumption' AND record_id = old.id; INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes) VALUES ('assumption', new.id, new.workspace_id, '', new.statement, new.notes); END",
    "CREATE TRIGGER IF NOT EXISTS trg_assumptions_context_fts_delete AFTER DELETE ON assumptions BEGIN DELETE FROM context_records_fts WHERE kind = 'assumption' AND record_id = old.id; END",
    "CREATE TRIGGER IF NOT EXISTS trg_parameters_context_fts_insert AFTER INSERT ON parameters BEGIN INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes) VALUES ('parameter', new.id, new.workspace_id, new.name, COALESCE(new.symbol, ''), new.notes); END",
    "CREATE TRIGGER IF NOT EXISTS trg_parameters_context_fts_update AFTER UPDATE ON parameters BEGIN DELETE FROM context_records_fts WHERE kind = 'parameter' AND record_id = old.id; INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes) VALUES ('parameter', new.id, new.workspace_id, new.name, COALESCE(new.symbol, ''), new.notes); END",
    "CREATE TRIGGER IF NOT EXISTS trg_parameters_context_fts_delete AFTER DELETE ON parameters BEGIN DELETE FROM context_records_fts WHERE kind = 'parameter' AND record_id = old.id; END",
    "CREATE TRIGGER IF NOT EXISTS trg_requirements_context_fts_insert AFTER INSERT ON requirements BEGIN INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes) VALUES ('requirement', new.id, new.workspace_id, '', COALESCE(new.statement, '') || ' ' || COALESCE(new.rationale, ''), new.notes); END",
    "CREATE TRIGGER IF NOT EXISTS trg_requirements_context_fts_update AFTER UPDATE ON requirements BEGIN DELETE FROM context_records_fts WHERE kind = 'requirement' AND record_id = old.id; INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes) VALUES ('requirement', new.id, new.workspace_id, '', COALESCE(new.statement, '') || ' ' || COALESCE(new.rationale, ''), new.notes); END",
    "CREATE TRIGGER IF NOT EXISTS trg_requirements_context_fts_delete AFTER DELETE ON requirements BEGIN DELETE FROM context_records_fts WHERE kind = 'requirement' AND record_id = old.id; END",
]

CONTEXT_RECORDS_FTS_BACKFILL_STATEMENT = """
INSERT INTO context_records_fts(kind, record_id, workspace_id, title, body, notes)
SELECT 'decision', id, workspace_id, title, COALESCE(decision_text, '') || ' ' || COALESCE(rationale, ''), notes FROM decisions
UNION ALL SELECT 'assumption', id, workspace_id, '', statement, notes FROM assumptions
UNION ALL SELECT 'parameter', id, workspace_id, name, COALESCE(symbol, ''), notes FROM parameters
UNION ALL SELECT 'requirement', id, workspace_id, '', COALESCE(statement, '') || ' ' || COALESCE(rationale, ''), notes FROM requirements
"""

SCHEMA_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_design_components_version_parent "
    "ON design_components(version_id, parent_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_design_project_one_active "
    "ON design_project_versions(project_id) WHERE active = 1",
    "CREATE INDEX IF NOT EXISTS idx_design_project_versions_project "
    "ON design_project_versions(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_studio_procedure_runs_draft ON studio_procedure_runs(workspace_id, draft_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_studio_procedure_links_draft ON studio_procedure_links(workspace_id, draft_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_workspace_created_at ON events(workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_workspace_created_at ON artifacts(workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_run_artifacts_workspace_run ON run_artifacts(workspace_id, simulation_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_simulation_runs_workspace_created_at ON simulation_runs(workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_simulation_runs_workspace_status ON simulation_runs(workspace_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_runner_jobs_workspace_status ON runner_jobs(workspace_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_model_versions_workspace_model_spec ON model_versions(workspace_id, model_spec_id)",
    "CREATE INDEX IF NOT EXISTS idx_bluecad_candidates_workspace_created ON bluecad_candidates(workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_bluecad_attempts_candidate_attempt ON bluecad_attempts(candidate_id, attempt_no)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_records_workspace_kind ON evidence_records(workspace_id, kind, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_parameters_workspace_supersedes ON parameters(workspace_id, supersedes_parameter_id)",
    "CREATE INDEX IF NOT EXISTS idx_freshness_marks_workspace_ref ON freshness_marks(workspace_id, record_ref)",
    "CREATE INDEX IF NOT EXISTS idx_freshness_marks_invalidation_ref ON freshness_marks(invalidation_id, record_ref)",
    "CREATE INDEX IF NOT EXISTS idx_freshness_invalidations_workspace_created ON freshness_invalidations(workspace_id, created_at)",
]
