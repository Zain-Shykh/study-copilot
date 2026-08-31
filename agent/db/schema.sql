-- App-specific tables. LangGraph's own checkpoint tables (via
-- langgraph-checkpoint-postgres) are created separately by its setup routine.

CREATE TABLE IF NOT EXISTS pending_items (
    whatsapp_message_id TEXT PRIMARY KEY,
    thread_id            TEXT NOT NULL,
    item_type             TEXT NOT NULL CHECK (item_type IN ('assignment', 'email')),
    display_name          TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS email_checkpoint (
    id                TEXT PRIMARY KEY DEFAULT 'singleton',
    last_history_id    TEXT NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notified_milestones (
    course_id       TEXT NOT NULL,
    coursework_id    TEXT NOT NULL,
    milestone_type    TEXT NOT NULL,
    notified_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (coursework_id, milestone_type)
);

CREATE TABLE IF NOT EXISTS claude_sessions (
    assignment_thread_id TEXT PRIMARY KEY,
    claude_session_id     TEXT NOT NULL,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
