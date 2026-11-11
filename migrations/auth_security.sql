-- Apply before the backend release. Additive; does not replace passwords/data.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0;
CREATE TABLE security_rate_limits (
    bucket_key VARCHAR(64) PRIMARY KEY,
    hits INTEGER NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX ix_security_rate_limits_expires_at ON security_rate_limits(expires_at);
CREATE TABLE password_resets (
    token_hash VARCHAR(64) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    auth_version INTEGER NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX ix_password_resets_user_id ON password_resets(user_id);
CREATE INDEX ix_password_resets_expires_at ON password_resets(expires_at);
COMMIT;
