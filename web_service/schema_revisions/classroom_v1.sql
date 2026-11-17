CREATE TABLE IF NOT EXISTS web_classroom_connections (
    user_id TEXT PRIMARY KEY NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    captured_at BIGINT,
    payload_blob TEXT
);
