CREATE TABLE IF NOT EXISTS web_school_tokens (
 session_hash TEXT PRIMARY KEY REFERENCES web_sessions(id_hash) ON DELETE CASCADE,
 token_blob TEXT NOT NULL, expires_at INTEGER NOT NULL, last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_school_sync (
 user_id TEXT PRIMARY KEY REFERENCES web_users(id), checked_at INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'not_connected', lease_until INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_security_limits (
 key_hash TEXT NOT NULL, bucket INTEGER NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(key_hash,bucket));
CREATE TABLE IF NOT EXISTS web_security_audit (at INTEGER NOT NULL, event TEXT NOT NULL, principal_hash TEXT NOT NULL);
