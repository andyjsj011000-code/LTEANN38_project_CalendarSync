CREATE TABLE IF NOT EXISTS beta_environment (format TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS beta_credentials (
  user_id TEXT PRIMARY KEY REFERENCES web_users(id),
  code_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS beta_login_limits (
  fingerprint TEXT NOT NULL, minute INTEGER NOT NULL, attempts INTEGER NOT NULL,
  PRIMARY KEY(fingerprint, minute)
);
CREATE TABLE IF NOT EXISTS beta_feedback (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES web_users(id),
  body_blob TEXT NOT NULL, created_at INTEGER NOT NULL
);
