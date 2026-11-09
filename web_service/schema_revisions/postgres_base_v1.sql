-- DRAFT ONLY: not wired or validated against PostgreSQL; do not deploy.
-- Existing SQLite data are not migrated by this script.
-- Development web database only. Never runs against the legacy Oracle tables.
CREATE TABLE IF NOT EXISTS web_schema_version (version BIGINT PRIMARY KEY);
INSERT INTO web_schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS web_users (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, object_id TEXT NOT NULL,
    profile_blob TEXT NOT NULL, active BIGINT NOT NULL DEFAULT 1,
    UNIQUE(tenant_id, object_id)
);
CREATE TABLE IF NOT EXISTS web_auth_flows (
    id_hash TEXT PRIMARY KEY, flow_blob TEXT NOT NULL, expires_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_sessions (
    id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
    csrf_token TEXT NOT NULL, expires_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS web_session_expiry ON web_sessions(expires_at);
CREATE TABLE IF NOT EXISTS web_channels (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, label_blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_channel_access (
    user_id TEXT NOT NULL REFERENCES web_users(id),
    channel_id TEXT NOT NULL REFERENCES web_channels(id),
    confirmed_at BIGINT NOT NULL, valid_until BIGINT NOT NULL,
    PRIMARY KEY(user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS web_sources (
    id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES web_channels(id),
    message_id TEXT NOT NULL, version TEXT NOT NULL, body_blob TEXT NOT NULL,
    parent_message_id TEXT, deleted_at BIGINT,
    UNIQUE(channel_id, message_id, version)
);
CREATE TABLE IF NOT EXISTS web_events (
    id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES web_channels(id),
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, kind TEXT NOT NULL,
    payload_blob TEXT NOT NULL, cancelled BIGINT NOT NULL DEFAULT 0,
    revision BIGINT NOT NULL DEFAULT 1, CHECK(start_date <= end_date)
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
    hidden BIGINT NOT NULL DEFAULT 0, completed BIGINT NOT NULL DEFAULT 0,
    override_blob TEXT, base_revision BIGINT,
    PRIMARY KEY(user_id, event_id)
);
CREATE TABLE IF NOT EXISTS web_personal_tasks (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, payload_blob TEXT NOT NULL,
    completed BIGINT NOT NULL DEFAULT 0, hidden BIGINT NOT NULL DEFAULT 0,
    version BIGINT NOT NULL DEFAULT 1, CHECK(start_date <= end_date)
);
CREATE INDEX IF NOT EXISTS web_personal_dates ON web_personal_tasks(user_id, start_date, end_date);
CREATE TABLE IF NOT EXISTS web_preferences (
    user_id TEXT PRIMARY KEY REFERENCES web_users(id), payload_blob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_channel_preferences (
    user_id TEXT NOT NULL REFERENCES web_users(id),
    channel_id TEXT NOT NULL REFERENCES web_channels(id), enabled BIGINT NOT NULL,
    PRIMARY KEY(user_id, channel_id)
);
CREATE TABLE IF NOT EXISTS web_calendar_links (
    user_id TEXT NOT NULL REFERENCES web_users(id), event_id TEXT NOT NULL,
    external_id_blob TEXT NOT NULL, writer TEXT NOT NULL DEFAULT 'legacy',
    recorded_revision BIGINT,
    PRIMARY KEY(user_id, event_id)
);


CREATE TABLE IF NOT EXISTS web_school_tokens (
 session_hash TEXT PRIMARY KEY REFERENCES web_sessions(id_hash) ON DELETE CASCADE,
 token_blob TEXT NOT NULL, expires_at BIGINT NOT NULL, last_seen BIGINT NOT NULL);
-- lease_token is separate from expiry to fence retries started in one second.
-- CREATE TABLE IF NOT EXISTS does not upgrade a database from an older draft.
CREATE TABLE IF NOT EXISTS web_school_sync (
 user_id TEXT PRIMARY KEY REFERENCES web_users(id), checked_at BIGINT NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'not_connected', lease_until BIGINT NOT NULL DEFAULT 0,
 lease_token TEXT);
CREATE TABLE IF NOT EXISTS web_security_limits (
 key_hash TEXT NOT NULL, bucket BIGINT NOT NULL, attempts BIGINT NOT NULL, PRIMARY KEY(key_hash,bucket));
CREATE TABLE IF NOT EXISTS web_security_audit (at BIGINT NOT NULL, event TEXT NOT NULL, principal_hash TEXT NOT NULL);


CREATE TABLE IF NOT EXISTS web_connections (
 id_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),credential_blob TEXT NOT NULL,
 token_expires BIGINT NOT NULL,expires_at BIGINT NOT NULL,background BIGINT NOT NULL,active BIGINT NOT NULL DEFAULT 1,
 version BIGINT NOT NULL DEFAULT 1,refresh_lease TEXT,refresh_until BIGINT NOT NULL DEFAULT 0,next_sync BIGINT NOT NULL DEFAULT 0,ingest_at BIGINT NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'connected');
CREATE TABLE IF NOT EXISTS web_session_links (
 session_hash TEXT PRIMARY KEY REFERENCES web_sessions(id_hash) ON DELETE CASCADE,
 device_hash TEXT NOT NULL REFERENCES web_connections(id_hash),last_seen BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS web_source_cache (
 cache_key TEXT PRIMARY KEY,channel_id TEXT NOT NULL,message_id TEXT NOT NULL,version TEXT NOT NULL,
 payload_blob TEXT NOT NULL,updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS web_user_source_versions (
 user_id TEXT NOT NULL REFERENCES web_users(id),event_id TEXT NOT NULL REFERENCES web_events(id),seen_hash TEXT NOT NULL,PRIMARY KEY(user_id,event_id));
CREATE TABLE IF NOT EXISTS web_changes (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),event_id TEXT NOT NULL REFERENCES web_events(id),
 source_hash TEXT NOT NULL,payload_blob TEXT NOT NULL,created_at BIGINT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',UNIQUE(user_id,event_id,source_hash));
CREATE TABLE IF NOT EXISTS web_push_subscriptions (
 id TEXT PRIMARY KEY,device_hash TEXT NOT NULL REFERENCES web_connections(id_hash),endpoint_hash TEXT NOT NULL UNIQUE,
 subscription_blob TEXT NOT NULL,created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS web_reminder_rules (
 user_id TEXT NOT NULL REFERENCES web_users(id),item_id TEXT NOT NULL,source TEXT NOT NULL,payload_blob TEXT NOT NULL,PRIMARY KEY(user_id,item_id,source));
CREATE TABLE IF NOT EXISTS web_deliveries (
 id TEXT PRIMARY KEY,subscription_id TEXT NOT NULL REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending',attempts BIGINT NOT NULL DEFAULT 0,next_attempt BIGINT NOT NULL DEFAULT 0,lease_until BIGINT NOT NULL DEFAULT 0,created_at BIGINT NOT NULL,
 lease_token TEXT);
CREATE TABLE IF NOT EXISTS web_feedback (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES web_users(id),payload_blob TEXT NOT NULL,created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS web_ingest_cursor (user_id TEXT PRIMARY KEY,cursor BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_worker_lock (id BIGINT PRIMARY KEY,owner TEXT NOT NULL,expires_at BIGINT NOT NULL);

-- The notification worker and registry are also experimental and not wired.
CREATE TABLE IF NOT EXISTS graph_subscriptions (
 subscription_id TEXT PRIMARY KEY, state_hash BYTEA NOT NULL,
 tenant_id TEXT NOT NULL, team_id TEXT NOT NULL, channel_id TEXT NOT NULL,
 active BIGINT NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS graph_one_active_per_channel
 ON graph_subscriptions(tenant_id,team_id,channel_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS graph_notification_jobs (
 subscription_id TEXT PRIMARY KEY REFERENCES graph_subscriptions(subscription_id),
 revision BIGINT NOT NULL, lease TEXT, leased_until BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS graph_subscription_alerts (
 subscription_id TEXT NOT NULL, event TEXT NOT NULL, observed_at BIGINT NOT NULL,
 PRIMARY KEY(subscription_id,event));
CREATE TABLE IF NOT EXISTS graph_channel_registry (
 tenant TEXT NOT NULL,team TEXT NOT NULL,channel TEXT NOT NULL,
 owner_device TEXT NOT NULL,state_blob TEXT NOT NULL,phase TEXT NOT NULL,
 subscription_id TEXT,expires_at BIGINT,last_error TEXT,updated_at BIGINT NOT NULL,
 PRIMARY KEY(tenant,team,channel));
CREATE TABLE IF NOT EXISTS graph_candidate_hints (
 tenant_id TEXT NOT NULL,team_id TEXT NOT NULL,channel_id TEXT NOT NULL,
 user_id TEXT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
 PRIMARY KEY(tenant_id,team_id,channel_id,user_id));
CREATE TABLE IF NOT EXISTS graph_notification_targets (
 subscription_id TEXT NOT NULL REFERENCES graph_subscriptions(subscription_id),
 message_id TEXT NOT NULL,reply_id TEXT NOT NULL,revision BIGINT NOT NULL,
 PRIMARY KEY(subscription_id,message_id,reply_id));
