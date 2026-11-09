CREATE TABLE IF NOT EXISTS web_connections (
 id_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),credential_blob TEXT NOT NULL,
 token_expires INTEGER NOT NULL,expires_at INTEGER NOT NULL,background INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,
 version INTEGER NOT NULL DEFAULT 1,refresh_lease TEXT,refresh_until INTEGER NOT NULL DEFAULT 0,next_sync INTEGER NOT NULL DEFAULT 0,ingest_at INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'connected');
CREATE TABLE IF NOT EXISTS web_session_links (
 session_hash TEXT PRIMARY KEY REFERENCES web_sessions(id_hash) ON DELETE CASCADE,
 device_hash TEXT NOT NULL REFERENCES web_connections(id_hash),last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_source_cache (
 cache_key TEXT PRIMARY KEY,channel_id TEXT NOT NULL,message_id TEXT NOT NULL,version TEXT NOT NULL,
 payload_blob TEXT NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_user_source_versions (
 user_id TEXT NOT NULL REFERENCES web_users(id),event_id TEXT NOT NULL REFERENCES web_events(id),seen_hash TEXT NOT NULL,PRIMARY KEY(user_id,event_id));
CREATE TABLE IF NOT EXISTS web_changes (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),event_id TEXT NOT NULL REFERENCES web_events(id),
 source_hash TEXT NOT NULL,payload_blob TEXT NOT NULL,created_at INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',UNIQUE(user_id,event_id,source_hash));
CREATE TABLE IF NOT EXISTS web_push_subscriptions (
 id TEXT PRIMARY KEY,device_hash TEXT NOT NULL REFERENCES web_connections(id_hash),endpoint_hash TEXT NOT NULL UNIQUE,
 subscription_blob TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_reminder_rules (
 user_id TEXT NOT NULL REFERENCES web_users(id),item_id TEXT NOT NULL,source TEXT NOT NULL,payload_blob TEXT NOT NULL,PRIMARY KEY(user_id,item_id,source));
CREATE TABLE IF NOT EXISTS web_deliveries (
 id TEXT PRIMARY KEY,subscription_id TEXT NOT NULL REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0,lease_until INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_feedback (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),payload_blob TEXT NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS web_ingest_cursor (user_id TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_worker_lock (id INTEGER PRIMARY KEY,owner TEXT NOT NULL,expires_at INTEGER NOT NULL);
