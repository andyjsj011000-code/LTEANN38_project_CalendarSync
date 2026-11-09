CREATE TABLE IF NOT EXISTS web_task_drafts (
 user_id TEXT PRIMARY KEY REFERENCES web_users(id) ON DELETE CASCADE,
 payload_blob TEXT, version BIGINT NOT NULL CHECK(version>=1),
 updated_at BIGINT NOT NULL, expires_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS web_task_drafts_expiry ON web_task_drafts(expires_at);
CREATE TABLE IF NOT EXISTS web_task_trash (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
        task_id TEXT NOT NULL,
        payload_blob TEXT NOT NULL,
        deleted_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        UNIQUE(user_id,task_id));
CREATE INDEX IF NOT EXISTS web_task_trash_expiry
        ON web_task_trash(expires_at,id);
CREATE TABLE IF NOT EXISTS web_ingestion_status (
    device_hash TEXT PRIMARY KEY REFERENCES web_connections(id_hash) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','partial','error')),
    requested_at BIGINT NOT NULL DEFAULT 0, started_at BIGINT NOT NULL DEFAULT 0,
    finished_at BIGINT NOT NULL DEFAULT 0, last_success_at BIGINT NOT NULL DEFAULT 0,
    attempt_token TEXT, attempt_until BIGINT NOT NULL DEFAULT 0,
    report_blob TEXT, error_code TEXT
);
