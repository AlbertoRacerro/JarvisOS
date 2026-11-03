EGRESS_SCHEMA_MIGRATION_ID = "0010_ip_egress_policy_autopilot"
EGRESS_SCHEMA_MIGRATION_NAME = "IP egress packets, decisions, reservations, tickets, and audit"
EGRESS_SCHEMA_MIGRATION_RECORD = {
    "migration_id": EGRESS_SCHEMA_MIGRATION_ID,
    "name": EGRESS_SCHEMA_MIGRATION_NAME,
    "checksum": None,
}

EGRESS_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS egress_prompt_derivatives (
        id TEXT PRIMARY KEY,
        workspace_id TEXT,
        raw_prompt_digest TEXT NOT NULL,
        derivative_content TEXT NOT NULL,
        derivative_digest TEXT NOT NULL,
        final_level TEXT NOT NULL CHECK (final_level IN ('S0', 'S1')),
        transformations_json TEXT NOT NULL,
        sanitizer_kind TEXT NOT NULL,
        sanitizer_version TEXT NOT NULL,
        sanitizer_config_digest TEXT NOT NULL,
        sanitizer_ai_job_id TEXT,
        policy_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('approved', 'revoked')),
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        revocation_reason TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (sanitizer_ai_job_id) REFERENCES ai_jobs(id),
        UNIQUE(workspace_id, raw_prompt_digest, derivative_digest, policy_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS egress_packets (
        id TEXT PRIMARY KEY,
        workspace_id TEXT,
        packet_digest TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL CHECK (operation = 'external_provider_call'),
        task_kind TEXT NOT NULL,
        route_class TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        fallback_index INTEGER NOT NULL CHECK (fallback_index >= 0),
        prompt_digest TEXT NOT NULL,
        prompt_derivative_id TEXT,
        packet_json TEXT NOT NULL,
        included_manifest_json TEXT NOT NULL,
        withheld_manifest_json TEXT NOT NULL,
        sanitizer_failed_manifest_json TEXT NOT NULL,
        policy_capped_manifest_json TEXT NOT NULL,
        budget_dropped_manifest_json TEXT NOT NULL,
        source_digests_json TEXT NOT NULL DEFAULT '{}',
        final_level TEXT NOT NULL CHECK (final_level IN ('S0', 'S1')),
        max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
        policy_version TEXT NOT NULL,
        trigger_version TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (prompt_derivative_id) REFERENCES egress_prompt_derivatives(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS egress_decisions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT,
        created_at TEXT NOT NULL,
        result TEXT NOT NULL CHECK (result IN ('allow', 'deny', 'pause')),
        reason_code TEXT NOT NULL,
        operation TEXT NOT NULL,
        route_class TEXT,
        provider_id TEXT,
        model_id TEXT,
        fallback_index INTEGER NOT NULL DEFAULT 0 CHECK (fallback_index >= 0),
        packet_id TEXT,
        packet_digest TEXT,
        safe_input_digest TEXT NOT NULL,
        prompt_level TEXT NOT NULL,
        context_level TEXT NOT NULL,
        final_level TEXT NOT NULL,
        source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
        included_count INTEGER NOT NULL DEFAULT 0 CHECK (included_count >= 0),
        withheld_count INTEGER NOT NULL DEFAULT 0 CHECK (withheld_count >= 0),
        trigger_ids_json TEXT NOT NULL,
        confirmation_required INTEGER NOT NULL CHECK (confirmation_required IN (0, 1)),
        projected_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (projected_input_tokens >= 0),
        projected_output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (projected_output_tokens >= 0),
        projected_cost_upper_usd REAL NOT NULL DEFAULT 0 CHECK (projected_cost_upper_usd >= 0),
        pricing_version TEXT,
        pricing_effective_at TEXT,
        reservation_id TEXT,
        ticket_id TEXT,
        policy_version TEXT NOT NULL,
        trigger_version TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (packet_id) REFERENCES egress_packets(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS egress_budget_reservations (
        id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE,
        packet_digest TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        projected_input_tokens INTEGER NOT NULL CHECK (projected_input_tokens >= 0),
        projected_output_tokens INTEGER NOT NULL CHECK (projected_output_tokens >= 0),
        projected_cost_upper_usd REAL NOT NULL CHECK (projected_cost_upper_usd >= 0),
        state TEXT NOT NULL CHECK (state IN ('active', 'in_flight', 'reconciled', 'released', 'expired')),
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempt_started_at TEXT,
        reconciled_at TEXT,
        egress_attempt_id TEXT,
        ai_job_id TEXT,
        actual_input_tokens INTEGER,
        actual_output_tokens INTEGER,
        actual_cost_usd REAL,
        reconciliation_status TEXT,
        FOREIGN KEY (decision_id) REFERENCES egress_decisions(id),
        FOREIGN KEY (ai_job_id) REFERENCES ai_jobs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS egress_confirmation_tickets (
        id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE,
        packet_id TEXT NOT NULL,
        packet_digest TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        trigger_ids_json TEXT NOT NULL,
        source_digests_json TEXT NOT NULL,
        continuation_authority_json TEXT,
        policy_version TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'consumed', 'expired', 'revoked')),
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        revoked_at TEXT,
        revocation_reason TEXT,
        FOREIGN KEY (decision_id) REFERENCES egress_decisions(id),
        FOREIGN KEY (packet_id) REFERENCES egress_packets(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS egress_attempts (
        id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL,
        packet_id TEXT NOT NULL,
        ai_job_id TEXT NOT NULL UNIQUE,
        reservation_id TEXT NOT NULL UNIQUE,
        route_class TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        fallback_index INTEGER NOT NULL CHECK (fallback_index >= 0),
        network_attempt INTEGER NOT NULL CHECK (network_attempt IN (0, 1)),
        reconciliation_status TEXT NOT NULL,
        projected_input_tokens INTEGER NOT NULL CHECK (projected_input_tokens >= 0),
        projected_output_tokens INTEGER NOT NULL CHECK (projected_output_tokens >= 0),
        projected_cost_upper_usd REAL NOT NULL CHECK (projected_cost_upper_usd >= 0),
        actual_input_tokens INTEGER,
        actual_output_tokens INTEGER,
        actual_cost_usd REAL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (decision_id) REFERENCES egress_decisions(id),
        FOREIGN KEY (packet_id) REFERENCES egress_packets(id),
        FOREIGN KEY (ai_job_id) REFERENCES ai_jobs(id),
        FOREIGN KEY (reservation_id) REFERENCES egress_budget_reservations(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sanitizer_audit_items (
        id TEXT PRIMARY KEY,
        workspace_id TEXT,
        derivative_kind TEXT NOT NULL CHECK (derivative_kind IN ('canonical', 'prompt')),
        derivative_id TEXT NOT NULL,
        derivative_digest TEXT NOT NULL,
        iso_week TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        selection_value INTEGER NOT NULL CHECK (selection_value BETWEEN 0 AND 9999),
        sample_rate_bps INTEGER NOT NULL CHECK (sample_rate_bps BETWEEN 500 AND 10000),
        state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'rejected')),
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewer TEXT,
        notes TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        UNIQUE(derivative_kind, derivative_id, derivative_digest, iso_week, policy_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspace_egress_policy (
        workspace_id TEXT PRIMARY KEY,
        ask_me INTEGER NOT NULL DEFAULT 0 CHECK (ask_me IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    "DROP TRIGGER IF EXISTS trg_egress_attempt_sync_ai_job_usage",
    "DROP TRIGGER IF EXISTS trg_egress_reservation_finalize_usage",
    """
    CREATE TRIGGER trg_egress_reservation_finalize_usage
    AFTER UPDATE OF state ON egress_budget_reservations
    WHEN NEW.state IN ('reconciled', 'released')
      AND NEW.egress_attempt_id IS NOT NULL
      AND NEW.ai_job_id IS NOT NULL
    BEGIN
        UPDATE egress_attempts
        SET actual_input_tokens = projected_input_tokens,
            actual_output_tokens = projected_output_tokens,
            actual_cost_usd = projected_cost_upper_usd,
            reconciliation_status = 'conservative_unverified_usage'
        WHERE id = NEW.egress_attempt_id
          AND NEW.state = 'reconciled'
          AND reconciliation_status IN ('actual', 'mixed_usage', 'estimated_usage')
          AND EXISTS (
              SELECT 1
              FROM ai_jobs
              WHERE id = NEW.ai_job_id
                AND status <> 'queued'
                AND cost_estimate IS NULL
          );

        UPDATE egress_budget_reservations
        SET actual_input_tokens = (
                SELECT actual_input_tokens
                FROM egress_attempts
                WHERE id = NEW.egress_attempt_id
            ),
            actual_output_tokens = (
                SELECT actual_output_tokens
                FROM egress_attempts
                WHERE id = NEW.egress_attempt_id
            ),
            actual_cost_usd = (
                SELECT actual_cost_usd
                FROM egress_attempts
                WHERE id = NEW.egress_attempt_id
            ),
            reconciliation_status = (
                SELECT reconciliation_status
                FROM egress_attempts
                WHERE id = NEW.egress_attempt_id
            )
        WHERE id = NEW.id;

        UPDATE ai_jobs
        SET input_tokens = COALESCE(
                (
                    SELECT actual_input_tokens
                    FROM egress_attempts
                    WHERE id = NEW.egress_attempt_id
                ),
                0
            ),
            output_tokens = COALESCE(
                (
                    SELECT actual_output_tokens
                    FROM egress_attempts
                    WHERE id = NEW.egress_attempt_id
                ),
                0
            ),
            cost_estimate = COALESCE(
                (
                    SELECT actual_cost_usd
                    FROM egress_attempts
                    WHERE id = NEW.egress_attempt_id
                ),
                (
                    SELECT projected_cost_upper_usd
                    FROM egress_attempts
                    WHERE id = NEW.egress_attempt_id
                ),
                0
            )
        WHERE id = NEW.ai_job_id;
    END
    """,
    """
    UPDATE egress_budget_reservations
    SET actual_input_tokens = (
            SELECT attempt.actual_input_tokens
            FROM egress_attempts AS attempt
            WHERE attempt.id = egress_budget_reservations.egress_attempt_id
        ),
        actual_output_tokens = (
            SELECT attempt.actual_output_tokens
            FROM egress_attempts AS attempt
            WHERE attempt.id = egress_budget_reservations.egress_attempt_id
        ),
        actual_cost_usd = (
            SELECT attempt.actual_cost_usd
            FROM egress_attempts AS attempt
            WHERE attempt.id = egress_budget_reservations.egress_attempt_id
        ),
        reconciliation_status = (
            SELECT attempt.reconciliation_status
            FROM egress_attempts AS attempt
            WHERE attempt.id = egress_budget_reservations.egress_attempt_id
        )
    WHERE egress_attempt_id IS NOT NULL
      AND EXISTS (
          SELECT 1
          FROM egress_attempts AS attempt
          WHERE attempt.id = egress_budget_reservations.egress_attempt_id
      )
    """,
    """
    UPDATE ai_jobs
    SET input_tokens = COALESCE(
            (
                SELECT attempt.actual_input_tokens
                FROM egress_attempts AS attempt
                WHERE attempt.ai_job_id = ai_jobs.id
            ),
            0
        ),
        output_tokens = COALESCE(
            (
                SELECT attempt.actual_output_tokens
                FROM egress_attempts AS attempt
                WHERE attempt.ai_job_id = ai_jobs.id
            ),
            0
        ),
        cost_estimate = COALESCE(
            (
                SELECT attempt.actual_cost_usd
                FROM egress_attempts AS attempt
                WHERE attempt.ai_job_id = ai_jobs.id
            ),
            (
                SELECT attempt.projected_cost_upper_usd
                FROM egress_attempts AS attempt
                WHERE attempt.ai_job_id = ai_jobs.id
            ),
            0
        )
    WHERE EXISTS (
        SELECT 1
        FROM egress_attempts AS attempt
        WHERE attempt.ai_job_id = ai_jobs.id
    )
    """,
]

EGRESS_SCHEMA_MIGRATION_STATEMENTS = [
    "ALTER TABLE sanitized_derivatives ADD COLUMN sanitizer_kind TEXT",
    "ALTER TABLE sanitized_derivatives ADD COLUMN sanitizer_version TEXT",
    "ALTER TABLE sanitized_derivatives ADD COLUMN sanitizer_config_digest TEXT",
    "ALTER TABLE sanitized_derivatives ADD COLUMN sanitizer_ai_job_id TEXT",
    "ALTER TABLE sanitized_derivatives ADD COLUMN approval_source TEXT",
    "ALTER TABLE sanitized_derivatives ADD COLUMN auto_approved INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ai_jobs ADD COLUMN usage_source TEXT "
    "CHECK (usage_source IN ('actual', 'estimated', 'mixed'))",
    "ALTER TABLE egress_packets ADD COLUMN source_digests_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE egress_confirmation_tickets ADD COLUMN continuation_authority_json TEXT",
]

EGRESS_SCHEMA_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_egress_prompt_derivatives_workspace_status ON egress_prompt_derivatives(workspace_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_egress_packets_binding_created ON egress_packets(provider_id, model_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_egress_decisions_workspace_created ON egress_decisions(workspace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_egress_decisions_binding_created ON egress_decisions(provider_id, model_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_egress_reservations_state_expiry ON egress_budget_reservations(state, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_egress_reservations_binding_state ON egress_budget_reservations(provider_id, model_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_egress_tickets_state_expiry ON egress_confirmation_tickets(state, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_egress_attempts_binding_created ON egress_attempts(provider_id, model_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sanitizer_audit_state_week ON sanitizer_audit_items(state, iso_week)",
]
