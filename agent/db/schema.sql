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

CREATE TABLE IF NOT EXISTS oauth_credentials (
    provider          TEXT PRIMARY KEY,       -- always 'google' in v1 (single provider)
    credentials_json  TEXT NOT NULL,           -- google.oauth2.credentials.Credentials.to_json()
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dedups inbound WhatsApp webhook deliveries: Meta redelivers a webhook
-- event if the endpoint doesn't ack fast enough, which would otherwise
-- re-run a slow tool-calling turn and send a second, independently-worded
-- reply for the same message.
CREATE TABLE IF NOT EXISTS processed_messages (
    whatsapp_message_id TEXT PRIMARY KEY,
    processed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
