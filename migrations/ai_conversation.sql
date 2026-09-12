-- Apply ONCE after plan_usage.sql. Additive; no message/plan/usage backfill.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE gpt_conversations (
    chart_id INTEGER PRIMARY KEY REFERENCES natal_charts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    summary VARCHAR NOT NULL,
    through_message_id INTEGER NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX ix_gpt_messages_chart_id_id ON gpt_messages(chart_id, id);
ALTER TABLE gpt_usage
    ADD COLUMN input_tokens INTEGER,
    ADD COLUMN output_tokens INTEGER,
    ADD COLUMN total_tokens INTEGER,
    ADD COLUMN model VARCHAR,
    ADD COLUMN summary_usage JSON;
COMMIT;
