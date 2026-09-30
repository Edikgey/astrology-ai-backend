-- Apply once AFTER paddle_billing.sql. No provider IDs, plans or quota rewritten.
BEGIN;
SET LOCAL lock_timeout = '5s';
-- Existing IDs must already have an explicit owner; fail rather than guess.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM users WHERE payment_provider IS NULL AND
               (provider_customer_id IS NOT NULL OR provider_subscription_id IS NOT NULL)) THEN
        RAISE EXCEPTION 'Billing IDs without provider require review before migration';
    END IF;
END $$;
ALTER TABLE users
    ADD CONSTRAINT ck_users_billing_provider CHECK (payment_provider IS NOT NULL OR (provider_customer_id IS NULL AND provider_subscription_id IS NULL)),
    ADD CONSTRAINT uq_users_provider_customer UNIQUE (payment_provider, provider_customer_id),
    ADD CONSTRAINT uq_users_provider_subscription UNIQUE (payment_provider, provider_subscription_id);
ALTER TABLE users DROP CONSTRAINT users_provider_customer_id_key,
                  DROP CONSTRAINT users_provider_subscription_id_key;
-- Reuse the existing event ledger and preserve all Paddle event IDs/outcomes.
ALTER TABLE paddle_events ADD COLUMN provider VARCHAR NOT NULL DEFAULT 'paddle';
ALTER TABLE paddle_events ADD COLUMN details JSON;
-- Keep the old primary key / ON CONFLICT(event_id) compatible during rollout.
-- Lava stores event_id as 'lava:' + stable external ID/hash. Paddle evt_* stays intact.
CREATE TABLE lava_checkouts (
    id VARCHAR PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    contract_id VARCHAR UNIQUE,
    offer_id VARCHAR NOT NULL,
    product_id VARCHAR NOT NULL,
    buyer_email VARCHAR NOT NULL,
    payment_url VARCHAR,
    state VARCHAR NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE,
    paid_at TIMESTAMP WITHOUT TIME ZONE,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ix_lava_checkouts_user_id ON lava_checkouts(user_id);
COMMIT;
