-- Apply once after lava_billing.sql. Additive: no plans, usage or checkout rewrites.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE billing_conflicts (
    id VARCHAR PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider VARCHAR NOT NULL,
    subscription_id VARCHAR NOT NULL,
    checkout_id VARCHAR NOT NULL,
    payment_id VARCHAR,
    primary_provider VARCHAR NOT NULL,
    primary_subscription_id VARCHAR NOT NULL,
    first_event_id VARCHAR NOT NULL,
    last_event_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'open',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    last_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT uq_billing_conflict_subscription UNIQUE (provider, subscription_id),
    CONSTRAINT ck_billing_conflict_provider CHECK (provider IN ('lava', 'paddle')),
    CONSTRAINT ck_billing_conflict_status CHECK (status IN ('open', 'resolved'))
);
CREATE INDEX ix_billing_conflicts_user_id ON billing_conflicts(user_id);
COMMIT;
