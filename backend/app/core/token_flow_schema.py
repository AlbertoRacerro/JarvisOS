TOKEN_FLOW_SCHEMA_MIGRATION_ID = "0011_token_flow_0"
TOKEN_FLOW_SCHEMA_MIGRATION_NAME = "Token flow identity, evidence, and durable continuation state"
TOKEN_FLOW_SCHEMA_MIGRATION_RECORD = {
    "migration_id": TOKEN_FLOW_SCHEMA_MIGRATION_ID,
    "name": TOKEN_FLOW_SCHEMA_MIGRATION_NAME,
    "checksum": None,
}

TOKEN_FLOW_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS ai_flows (
        id TEXT PRIMARY KEY,
        workspace_id TEXT,
        task_kind TEXT NOT NULL,
        requested_route_class TEXT,
        state TEXT NOT NULL CHECK (
            state IN (
                'running',
                'confirmation_required',
                'complete',
                'partial_terminal',
                'failed_terminal',
                'cancelled_terminal'
            )
        ),
        terminal_reason TEXT,
        terminal_attempt_id TEXT,
        max_direct_continuations_snapshot INTEGER NOT NULL DEFAULT 8
            CHECK (max_direct_continuations_snapshot BETWEEN 0 AND 16),
        continuation_count INTEGER NOT NULL DEFAULT 0 CHECK (continuation_count >= 0),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        ordered_attempt_ids_json TEXT NOT NULL DEFAULT '[]',
        execution_class_counts_json TEXT NOT NULL DEFAULT '{}',
        external_dispatch_counts_json TEXT NOT NULL DEFAULT '{}',
        usage_totals_json TEXT NOT NULL DEFAULT '{}',
        accounting_basis_counts_json TEXT NOT NULL DEFAULT '{}',
        external_provider_spend_usd_decimal TEXT NOT NULL DEFAULT '0',
        local_compute_cost_unpriced INTEGER NOT NULL DEFAULT 0
            CHECK (local_compute_cost_unpriced IN (0, 1)),
        synthetic_evidence_present INTEGER NOT NULL DEFAULT 0
            CHECK (synthetic_evidence_present IN (0, 1)),
        config_version TEXT,
        final_accounting_digest TEXT,
        final_output_digest TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        cancelled_at TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (terminal_attempt_id) REFERENCES ai_jobs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_flow_record_captures (
        flow_id TEXT PRIMARY KEY,
        terminal_attempt_id TEXT NOT NULL,
        final_output_digest TEXT NOT NULL,
        proposal_ids_json TEXT NOT NULL DEFAULT '[]',
        parse_error TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (flow_id) REFERENCES ai_flows(id),
        FOREIGN KEY (terminal_attempt_id) REFERENCES ai_jobs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_flow_segments (
        id TEXT PRIMARY KEY,
        flow_id TEXT NOT NULL,
        segment_index INTEGER NOT NULL CHECK (segment_index >= 0),
        originating_attempt_id TEXT NOT NULL,
        body_text TEXT NOT NULL,
        body_digest TEXT NOT NULL,
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        token_count INTEGER CHECK (token_count IS NULL OR token_count >= 0),
        sensitivity_level TEXT NOT NULL,
        policy_binding_digest TEXT NOT NULL,
        continuation_guard_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY (flow_id) REFERENCES ai_flows(id),
        FOREIGN KEY (originating_attempt_id) REFERENCES ai_jobs(id),
        UNIQUE(flow_id, segment_index)
    )
    """,
]

TOKEN_FLOW_SCHEMA_MIGRATION_STATEMENTS = [
    "ALTER TABLE ai_jobs ADD COLUMN flow_id TEXT",
    "ALTER TABLE ai_jobs ADD COLUMN flow_attempt_index INTEGER CHECK (flow_attempt_index >= 0)",
    "ALTER TABLE ai_jobs ADD COLUMN parent_attempt_id TEXT",
    "ALTER TABLE ai_jobs ADD COLUMN fallback_index INTEGER CHECK (fallback_index >= 0)",
    "ALTER TABLE ai_jobs ADD COLUMN continuation_index INTEGER CHECK (continuation_index >= 0)",
    """
    ALTER TABLE ai_jobs ADD COLUMN execution_class TEXT CHECK (
        execution_class IN (
            'none', 'synthetic', 'local_compute', 'external_provider', 'legacy_unknown'
        )
    )
    """,
    """
    ALTER TABLE ai_jobs ADD COLUMN adapter_invoked INTEGER CHECK (
        adapter_invoked IS NULL OR adapter_invoked IN (0, 1)
    )
    """,
    """
    ALTER TABLE ai_jobs ADD COLUMN external_dispatch_state TEXT CHECK (
        external_dispatch_state IN ('not_applicable', 'not_started', 'started', 'unknown')
    )
    """,
    "ALTER TABLE ai_jobs ADD COLUMN requested_output_ceiling INTEGER CHECK (requested_output_ceiling > 0)",
    "ALTER TABLE ai_jobs ADD COLUMN effective_output_ceiling INTEGER CHECK (effective_output_ceiling > 0)",
    """
    ALTER TABLE ai_jobs ADD COLUMN normalized_finish_reason TEXT CHECK (
        normalized_finish_reason IN ('stop', 'length', 'content_filter', 'tool_call', 'error', 'unknown')
    )
    """,
    """
    ALTER TABLE ai_jobs ADD COLUMN normalized_usage_source TEXT CHECK (
        normalized_usage_source IN ('actual', 'mixed', 'estimated', 'none')
    )
    """,
    "ALTER TABLE ai_jobs ADD COLUMN cache_read_tokens INTEGER CHECK (cache_read_tokens >= 0)",
    "ALTER TABLE ai_jobs ADD COLUMN reasoning_tokens INTEGER CHECK (reasoning_tokens >= 0)",
    """
    ALTER TABLE ai_jobs ADD COLUMN accounting_basis TEXT CHECK (
        accounting_basis IN (
            'no_execution',
            'synthetic_not_economic',
            'local_compute_unpriced',
            'external_not_sent',
            'provider_exact',
            'conservative_standard_input',
            'conservative_estimated_usage',
            'legacy_unknown'
        )
    )
    """,
    "ALTER TABLE ai_jobs ADD COLUMN accounted_provider_spend_usd_decimal TEXT",
    "ALTER TABLE ai_jobs ADD COLUMN outcome_reason TEXT",
    "ALTER TABLE ai_jobs ADD COLUMN capability_version TEXT",
    "ALTER TABLE ai_jobs ADD COLUMN pricing_version TEXT",
    "ALTER TABLE ai_jobs ADD COLUMN accounting_version TEXT",
    """
    ALTER TABLE ai_settings ADD COLUMN max_direct_continuations INTEGER NOT NULL DEFAULT 8
        CHECK (max_direct_continuations BETWEEN 0 AND 16)
    """,
    """
    ALTER TABLE ai_settings ADD COLUMN direct_continuation_policy_version TEXT NOT NULL
        DEFAULT 'token-flow-v0'
    """,
]

TOKEN_FLOW_SCHEMA_INDEX_STATEMENTS = [
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_jobs_flow_attempt_index
    ON ai_jobs(flow_id, flow_attempt_index)
    WHERE flow_id IS NOT NULL AND flow_attempt_index IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS idx_ai_jobs_flow_id ON ai_jobs(flow_id)",
    "CREATE INDEX IF NOT EXISTS idx_ai_flows_state ON ai_flows(state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_flows_workspace ON ai_flows(workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_flow_segments_expiry ON ai_flow_segments(expires_at)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_flow_record_captures_terminal
    ON ai_flow_record_captures(terminal_attempt_id)
    """,
]
