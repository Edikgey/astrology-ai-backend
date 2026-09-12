-- Apply once after plan_usage.sql, using the project's manual PostgreSQL approach.
-- Additive migration: no changes to existing plans, usage, charts or JWT data.
BEGIN;
ALTER TABLE users
    ADD COLUMN payment_provider VARCHAR,
    ADD COLUMN provider_customer_id VARCHAR UNIQUE,
    ADD COLUMN provider_subscription_id VARCHAR UNIQUE,
    ADD COLUMN subscription_status VARCHAR,
    ADD COLUMN paddle_updated_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN scheduled_cancel_at TIMESTAMP WITHOUT TIME ZONE;
CREATE TABLE paddle_checkouts (
    id VARCHAR PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    transaction_id VARCHAR UNIQUE,
    subscription_id VARCHAR UNIQUE,
    state VARCHAR NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX ix_paddle_checkouts_user_id ON paddle_checkouts(user_id);
CREATE TABLE paddle_events (
    event_id VARCHAR PRIMARY KEY,
    event_type VARCHAR NOT NULL,
    occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    processed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    outcome VARCHAR NOT NULL
);
COMMIT;
