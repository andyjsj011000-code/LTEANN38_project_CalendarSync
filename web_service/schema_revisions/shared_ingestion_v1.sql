CREATE TABLE IF NOT EXISTS web_channel_ingestion (
            channel_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, team_id TEXT NOT NULL,
            checked_at BIGINT NOT NULL DEFAULT 0, lease_token TEXT,
            lease_until BIGINT NOT NULL DEFAULT 0);
