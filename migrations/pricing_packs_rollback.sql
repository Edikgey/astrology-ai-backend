-- PRICING-1 schema rollback. Only AFTER the code is rolled back, and only while nothing was sold:
-- refuses (RAISE, nothing changes) if any pack, pack question, annual period, annual Stripe
-- checkout or annual Lava checkout exists. Payment records are never deleted by this script.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE users, stripe_checkouts, lava_checkouts, gpt_usage, question_packs IN SHARE ROW EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM question_packs) OR EXISTS (SELECT 1 FROM gpt_usage WHERE pack_id IS NOT NULL)
            OR EXISTS (SELECT 1 FROM users WHERE current_period_interval IS NOT NULL)
            OR EXISTS (SELECT 1 FROM stripe_checkouts WHERE price_id IS NOT NULL)
            OR EXISTS (SELECT 1 FROM lava_checkouts WHERE periodicity <> 'MONTHLY') THEN
        RAISE EXCEPTION 'Packs or annual records exist: the schema rollback would lose payment data';
    END IF;
END $$;
ALTER TABLE gpt_usage DROP COLUMN pack_id;
DROP TABLE question_packs;
ALTER TABLE lava_checkouts DROP COLUMN periodicity;
ALTER TABLE stripe_checkouts DROP COLUMN price_id;
ALTER TABLE users DROP COLUMN current_period_interval;
COMMIT;
