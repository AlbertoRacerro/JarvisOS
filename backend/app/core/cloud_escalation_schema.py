CLOUD_ESCALATION_MIGRATION_RECORD = {
    "migration_id": "0021_governed_cloud_escalation",
    "name": "One-step governed cloud escalation and session spend holds",
    "checksum": None,
}

CLOUD_ESCALATION_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS cloud_escalations (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        source_interaction_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        derivative_id TEXT NOT NULL,
        derivative_digest TEXT NOT NULL,
        task_family TEXT NOT NULL,
        quality_floor INTEGER NOT NULL,
        quality_tier INTEGER NOT NULL,
        qualification TEXT NOT NULL,
        qualification_evidence_ref TEXT NOT NULL,
        route_class TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        pricing_version TEXT NOT NULL,
        pricing_reviewed_on TEXT NOT NULL,
        pricing_source_url TEXT NOT NULL,
        pricing_effective_at TEXT NOT NULL,
        eur_usd_rate TEXT NOT NULL,
        fx_date TEXT NOT NULL,
        fx_source TEXT NOT NULL,
        projected_cost_usd TEXT NOT NULL,
        accounted_cost_usd TEXT NOT NULL,
        cost_basis TEXT NOT NULL CHECK (cost_basis IN ('hold', 'actual_priced', 'zero_before_network', 'upper_unknown')),
        actual_input_tokens INTEGER,
        actual_output_tokens INTEGER,
        context_digest TEXT,
        state TEXT NOT NULL CHECK (state IN ('held', 'confirmation_required', 'complete', 'partial', 'failed')),
        flow_id TEXT,
        ai_job_id TEXT,
        ticket_id TEXT,
        egress_packet_digest TEXT,
        reason_code TEXT,
        response_text TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (thread_id) REFERENCES ai_threads(id),
        FOREIGN KEY (source_interaction_id) REFERENCES ai_thread_interactions(id),
        FOREIGN KEY (derivative_id) REFERENCES sanitized_derivatives(id),
        UNIQUE(thread_id, request_id)
    )
    """,
]

CLOUD_ESCALATION_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_cloud_escalations_thread ON cloud_escalations(thread_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cloud_escalations_ticket ON cloud_escalations(ticket_id) WHERE ticket_id IS NOT NULL",
]
