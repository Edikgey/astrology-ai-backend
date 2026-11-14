-- PRICING-1: annual Premium and one-time question packs. Apply ONCE after stripe_billing.sql,
-- BEFORE deploying the backend that maps these columns. Additive only: no existing row is
-- rewritten or deleted; old rows read as today (interval NULL, price_id NULL = monthly,
-- periodicity MONTHLY, pack_id NULL = a question of the plan). The backend at 393158f keeps
-- working on this schema. A second or partial application fails and rolls back entirely.
-- Rehearse on a restored copy (tests/test_pricing_postgres.py). Rollback: pricing_packs_rollback.sql.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE users ADD COLUMN current_period_interval VARCHAR;
ALTER TABLE users ADD CONSTRAINT ck_users_period_interval CHECK (current_period_interval IN ('month', 'year'));

ALTER TABLE stripe_checkouts ADD COLUMN price_id VARCHAR;

ALTER TABLE lava_checkouts ADD COLUMN periodicity VARCHAR NOT NULL DEFAULT 'MONTHLY';
ALTER TABLE lava_checkouts ADD CONSTRAINT ck_lava_checkouts_periodicity CHECK (periodicity IN ('MONTHLY', 'PERIOD_YEAR'));

CREATE TABLE question_packs (
    id VARCHAR PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider VARCHAR NOT NULL CONSTRAINT ck_question_packs_provider CHECK (provider IN ('stripe', 'lava')),
    account_id VARCHAR,
    livemode BOOLEAN,
    quantity INTEGER NOT NULL CONSTRAINT ck_question_packs_quantity CHECK (quantity > 0),
    state VARCHAR NOT NULL CONSTRAINT ck_question_packs_state
        CHECK (state IN ('creating', 'ready', 'paid', 'failed', 'expired', 'rejected', 'revoked')),
    customer_id VARCHAR,
    session_id VARCHAR UNIQUE,
    payment_intent_id VARCHAR UNIQUE,
    contract_id VARCHAR UNIQUE,
    offer_id VARCHAR,
    product_id VARCHAR,
    buyer_email VARCHAR,
    payment_url VARCHAR,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE,
    paid_at TIMESTAMP WITHOUT TIME ZONE,
    revoked_at TIMESTAMP WITHOUT TIME ZONE,
    revoke_reason VARCHAR CONSTRAINT ck_question_packs_revoke_reason CHECK (revoke_reason IN ('refund', 'dispute'))
);
CREATE INDEX ix_question_packs_user_state ON question_packs (user_id, state);

ALTER TABLE gpt_usage ADD COLUMN pack_id VARCHAR REFERENCES question_packs(id);
CREATE INDEX ix_gpt_usage_pack ON gpt_usage (pack_id, status);
COMMIT;
