CREATE TABLE IF NOT EXISTS web_classroom_google (
    user_id TEXT PRIMARY KEY NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    connection_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'connected', 'reauth')),
    credential_blob TEXT,
    captured_at BIGINT,
    snapshot_blob TEXT,
    next_sync_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS web_classroom_google_due
    ON web_classroom_google (state, next_sync_at);
