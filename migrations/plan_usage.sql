-- Standalone additive PostgreSQL migration. Do NOT run the old Alembic chain.
-- Stop all backend writers first; back up and rehearse on a restored copy.
-- Run with psql ON_ERROR_STOP. No extension or uuid generation is needed.
BEGIN;
SET LOCAL lock_timeout = '10s';
LOCK TABLE users, natal_charts, gpt_messages IN SHARE ROW EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM gpt_messages WHERE role IS NULL OR role NOT IN ('user', 'gpt', 'assistant') OR id <= 0) THEN
        RAISE EXCEPTION 'Unexpected legacy message role or ID: inspect data before backfill';
    END IF;
END $$;

-- These statements intentionally fail on a second/partial application.
-- A failure rolls back the entire migration; inspect rather than silently skip.
ALTER TABLE users ADD COLUMN plan VARCHAR NOT NULL DEFAULT 'free';
ALTER TABLE users ADD COLUMN current_period_start TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE users ADD COLUMN current_period_end TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE users ADD CONSTRAINT ck_users_plan CHECK (plan IN ('free', 'premium'));

CREATE TABLE gpt_usage (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    chart_id INTEGER NOT NULL,
    source_message_id INTEGER UNIQUE REFERENCES gpt_messages(id) ON DELETE SET NULL,
    status VARCHAR NOT NULL CONSTRAINT ck_gpt_usage_status CHECK (status IN ('reserved', 'succeeded', 'released')),
    plan VARCHAR NOT NULL CONSTRAINT ck_gpt_usage_plan CHECK (plan IN ('free', 'premium')),
    period_start TIMESTAMP WITHOUT TIME ZONE,
    period_end TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE
);
CREATE INDEX ix_gpt_usage_account ON gpt_usage (user_id, status, period_start);

-- BEGIN BACKFILL
INSERT INTO gpt_usage (id, user_id, chart_id, source_message_id, status, plan, created_at)
SELECT ('00000000-0000-0000-0000-' || lpad(m.id::text, 12, '0'))::uuid,
       c.user_id, m.chart_id, m.id, 'succeeded', 'free', COALESCE(m.created_at, CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
FROM gpt_messages m JOIN natal_charts c ON c.id = m.chart_id
WHERE m.role = 'user' AND c.user_id IS NOT NULL;
-- END BACKFILL

COMMIT;
