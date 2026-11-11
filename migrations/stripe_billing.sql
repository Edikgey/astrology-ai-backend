-- Apply once AFTER all existing billing migrations. Stop/drain backend workers
-- before this metadata rename, then start the new binary. Webhook senders retry.
-- No users, entitlement periods, quota, Lava rows or historical rows are deleted.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE paddle_events RENAME TO billing_events;
ALTER TABLE billing_events ALTER COLUMN provider DROP DEFAULT;
ALTER TABLE billing_conflicts DROP CONSTRAINT ck_billing_conflict_provider;
ALTER TABLE billing_conflicts ADD CONSTRAINT ck_billing_conflict_provider
    CHECK (provider IN ('lava', 'stripe'));
CREATE TABLE billing_customers (
    id VARCHAR PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    provider VARCHAR NOT NULL, account_id VARCHAR NOT NULL, livemode BOOLEAN NOT NULL,
    customer_id VARCHAR, created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT uq_billing_customer_user UNIQUE(provider, account_id, livemode, user_id),
    CONSTRAINT uq_billing_customer_external UNIQUE(provider, account_id, livemode, customer_id)
);
CREATE TABLE stripe_checkouts (
    id VARCHAR PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    account_id VARCHAR NOT NULL, livemode BOOLEAN NOT NULL, customer_id VARCHAR,
    session_id VARCHAR UNIQUE, subscription_id VARCHAR UNIQUE, state VARCHAR NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, expires_at TIMESTAMP WITHOUT TIME ZONE,
    revision INTEGER NOT NULL DEFAULT 0, subscription_status VARCHAR,
    ended_at TIMESTAMP WITHOUT TIME ZONE
);
CREATE INDEX ix_stripe_checkouts_user_id ON stripe_checkouts(user_id);
CREATE TABLE billing_periods (
    id VARCHAR PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    provider VARCHAR NOT NULL, account_id VARCHAR NOT NULL, livemode BOOLEAN NOT NULL,
    subscription_id VARCHAR NOT NULL, invoice_id VARCHAR NOT NULL,
    period_start TIMESTAMP WITHOUT TIME ZONE NOT NULL, period_end TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    applied BOOLEAN NOT NULL,
    CONSTRAINT uq_billing_period_invoice UNIQUE(provider, account_id, livemode, invoice_id),
    CONSTRAINT uq_billing_period_start UNIQUE(provider, account_id, livemode, subscription_id, period_start),
    CONSTRAINT ck_billing_period_dates CHECK(period_start < period_end)
);
-- paddle_checkouts and users.paddle_updated_at stay as inert test-history storage.
-- No application ORM maps them. Retiring storage requires separate data approval.
COMMIT;
