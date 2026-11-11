-- Additive analytics-only consent/outbox. Existing application data is untouched.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
CREATE TABLE analytics_monthly_budget (
    month VARCHAR(7) PRIMARY KEY,
    events INTEGER NOT NULL
);
CREATE TABLE analytics_account_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    accepted BOOLEAN NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE TABLE analytics_consents (
    visitor_key VARCHAR(64) PRIMARY KEY,
    revision VARCHAR(36) NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    accepted BOOLEAN NOT NULL,
    policy_version VARCHAR(32) NOT NULL,
    accepted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX ix_analytics_consents_user_id ON analytics_consents(user_id);
CREATE INDEX ix_analytics_consents_expires_at ON analytics_consents(expires_at);
CREATE TABLE analytics_outbox (
    event_id VARCHAR(36) PRIMARY KEY,
    visitor_key VARCHAR(64) NOT NULL REFERENCES analytics_consents(visitor_key) ON DELETE CASCADE,
    revision VARCHAR(36) NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    distinct_id VARCHAR(80) NOT NULL,
    event_name VARCHAR(48) NOT NULL,
    properties JSON NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    delivered_at TIMESTAMP WITHOUT TIME ZONE
);
CREATE INDEX ix_analytics_outbox_visitor_key ON analytics_outbox(visitor_key);
CREATE INDEX ix_analytics_outbox_user_id ON analytics_outbox(user_id);
CREATE INDEX ix_analytics_outbox_created_at ON analytics_outbox(created_at);
CREATE INDEX ix_analytics_outbox_next_attempt_at ON analytics_outbox(next_attempt_at);
COMMIT;
