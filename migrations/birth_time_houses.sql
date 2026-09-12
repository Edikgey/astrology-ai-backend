-- Apply once before running the corrected chart backend. No inferred legacy backfill.
BEGIN;
SET LOCAL lock_timeout = '10s';
ALTER TABLE natal_charts ADD COLUMN timezone VARCHAR,
    ADD COLUMN birth_utc TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE chart_data ADD COLUMN houses JSON,
    ADD COLUMN house_system VARCHAR;
COMMIT;
