-- Development web database only. Never runs against the legacy Oracle tables.
CREATE TABLE IF NOT EXISTS web_schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO web_schema_version(version) VALUES (1);

CREATE TABLE IF NOT EXISTS web_users (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, object_id TEXT NOT NULL,
    profile_blob TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(tenant_id, object_id)
);
CREATE TABLE IF NOT EXISTS web_auth_flows (
    id_hash TEXT PRIMARY KEY, flow_blob TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS web_sessions (
    id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
    csrf_token TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS web_session_expiry ON web_sessions(expires_at);
CREATE TABLE IF NOT EXISTS web_channels (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, label_blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_channel_access (
    user_id TEXT NOT NULL REFERENCES web_users(id),
    channel_id TEXT NOT NULL REFERENCES web_channels(id),
    confirmed_at INTEGER NOT NULL, valid_until INTEGER NOT NULL,
    PRIMARY KEY(user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS web_sources (
    id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES web_channels(id),
    message_id TEXT NOT NULL, version TEXT NOT NULL, body_blob TEXT NOT NULL,
    parent_message_id TEXT, deleted_at INTEGER,
    UNIQUE(channel_id, message_id, version)
);
CREATE TABLE IF NOT EXISTS web_events (
    id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES web_channels(id),
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, kind TEXT NOT NULL,
    payload_blob TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1, CHECK(start_date <= end_date)
);
CREATE TABLE IF NOT EXISTS web_event_sources (
    event_id TEXT NOT NULL REFERENCES web_events(id),
    source_id TEXT NOT NULL REFERENCES web_sources(id),
    PRIMARY KEY(event_id, source_id)
);
CREATE INDEX IF NOT EXISTS web_event_dates ON web_events(channel_id, start_date, end_date);
CREATE TABLE IF NOT EXISTS web_event_states (
    user_id TEXT NOT NULL REFERENCES web_users(id),
    event_id TEXT NOT NULL REFERENCES web_events(id),
    hidden INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
    override_blob TEXT, base_revision INTEGER,
    PRIMARY KEY(user_id, event_id)
);
CREATE TABLE IF NOT EXISTS web_personal_tasks (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, payload_blob TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1, CHECK(start_date <= end_date)
);
CREATE INDEX IF NOT EXISTS web_personal_dates ON web_personal_tasks(user_id, start_date, end_date);
CREATE TABLE IF NOT EXISTS web_preferences (
    user_id TEXT PRIMARY KEY REFERENCES web_users(id), payload_blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_channel_preferences (
    user_id TEXT NOT NULL REFERENCES web_users(id),
    channel_id TEXT NOT NULL REFERENCES web_channels(id), enabled INTEGER NOT NULL,
    PRIMARY KEY(user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS web_calendar_links (
    user_id TEXT NOT NULL REFERENCES web_users(id), event_id TEXT NOT NULL,
    external_id_blob TEXT NOT NULL, writer TEXT NOT NULL DEFAULT 'legacy',
    recorded_revision INTEGER,
    PRIMARY KEY(user_id, event_id)
);
