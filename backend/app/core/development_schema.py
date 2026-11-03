DEVELOPMENT_SCHEMA_MIGRATION_RECORD = {
    "migration_id": "0019_roadmap_calendar",
    "name": "Development roadmap and calendar state",
    "checksum": None,
}

DEVELOPMENT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS roadmap_items (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        item_type TEXT NOT NULL CHECK (item_type IN (
            'Task', 'Work package', 'Milestone', 'Investigation', 'Validation',
            'Decision', 'Procurement', 'Manufacturing', 'Meeting/Review'
        )),
        status TEXT NOT NULL DEFAULT 'Planned' CHECK (status IN (
            'Planned', 'Ready', 'In progress', 'Blocked', 'Done', 'Cancelled'
        )),
        priority TEXT NOT NULL DEFAULT 'Normal' CHECK (priority IN (
            'Critical', 'High', 'Normal', 'Opportunity'
        )),
        window_start_date TEXT,
        window_end_date TEXT,
        domain TEXT,
        owner TEXT,
        effort_estimate TEXT,
        tags_json TEXT NOT NULL DEFAULT '[]',
        notes TEXT,
        cannot_start_before TEXT,
        must_finish_before TEXT,
        done_when TEXT,
        done_when_satisfied INTEGER CHECK (done_when_satisfied IN (0, 1)),
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        CHECK (length(trim(title)) > 0),
        CHECK (
            window_start_date IS NULL OR window_end_date IS NULL
            OR window_end_date >= window_start_date
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS roadmap_dependencies (
        workspace_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        depends_on_item_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, item_id, depends_on_item_id),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (item_id) REFERENCES roadmap_items(id),
        FOREIGN KEY (depends_on_item_id) REFERENCES roadmap_items(id),
        CHECK (item_id <> depends_on_item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS roadmap_object_links (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        ref_type TEXT NOT NULL,
        ref_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (item_id) REFERENCES roadmap_items(id),
        UNIQUE (workspace_id, item_id, ref_type, ref_id),
        CHECK (length(trim(ref_type)) > 0),
        CHECK (length(trim(ref_id)) > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS calendar_allocations (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        roadmap_item_id TEXT,
        title TEXT NOT NULL,
        event_type TEXT NOT NULL,
        start_instant TEXT NOT NULL,
        end_instant TEXT NOT NULL,
        timezone TEXT NOT NULL,
        all_day INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
        deadline INTEGER NOT NULL DEFAULT 0 CHECK (deadline IN (0, 1)),
        description TEXT,
        priority TEXT CHECK (priority IN ('Critical', 'High', 'Normal', 'Opportunity')),
        domain TEXT,
        location TEXT,
        meeting_link TEXT,
        reminder_json TEXT,
        tags_json TEXT NOT NULL DEFAULT '[]',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (roadmap_item_id) REFERENCES roadmap_items(id),
        CHECK (length(trim(title)) > 0),
        CHECK (length(trim(event_type)) > 0),
        CHECK (length(trim(timezone)) > 0),
        CHECK (end_instant > start_instant)
    )
    """,
)

DEVELOPMENT_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_roadmap_items_workspace_status ON roadmap_items(workspace_id, status, priority, updated_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_roadmap_items_workspace_window ON roadmap_items(workspace_id, window_start_date, window_end_date, id)",
    "CREATE INDEX IF NOT EXISTS idx_roadmap_dependencies_item ON roadmap_dependencies(workspace_id, item_id, depends_on_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_roadmap_dependencies_depends_on ON roadmap_dependencies(workspace_id, depends_on_item_id, item_id)",
    "CREATE INDEX IF NOT EXISTS idx_roadmap_object_links_item ON roadmap_object_links(workspace_id, item_id, ref_type, ref_id)",
    "CREATE INDEX IF NOT EXISTS idx_calendar_allocations_workspace_start ON calendar_allocations(workspace_id, start_instant, end_instant, id)",
    "CREATE INDEX IF NOT EXISTS idx_calendar_allocations_roadmap_item ON calendar_allocations(workspace_id, roadmap_item_id, start_instant, id)",
)
