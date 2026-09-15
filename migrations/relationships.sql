-- Apply ONCE to the current schema, before deploying Relationship endpoints.
-- Standalone additive migration: no existing data or quota changes.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE relationships (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    chart_a_id INTEGER NOT NULL REFERENCES natal_charts(id) ON DELETE RESTRICT,
    chart_b_id INTEGER NOT NULL REFERENCES natal_charts(id) ON DELETE RESTRICT,
    person_a_label VARCHAR(100) NOT NULL,
    person_b_label VARCHAR(100) NOT NULL,
    speaker_person VARCHAR(1),
    calculation JSONB NOT NULL,
    ruleset_version VARCHAR(50) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_relationships_distinct_charts CHECK (chart_a_id <> chart_b_id),
    CONSTRAINT ck_relationships_speaker CHECK (speaker_person IN ('A', 'B')),
    CONSTRAINT ck_relationships_labels CHECK (
        length(trim(person_a_label)) BETWEEN 1 AND 100 AND length(trim(person_b_label)) BETWEEN 1 AND 100)
);
CREATE INDEX ix_relationships_owner ON relationships(user_id, id);
CREATE INDEX ix_relationships_chart_a ON relationships(chart_a_id);
CREATE INDEX ix_relationships_chart_b ON relationships(chart_b_id);
CREATE UNIQUE INDEX uq_relationships_owner_pair ON relationships (
    user_id,
    (CASE WHEN chart_a_id < chart_b_id THEN chart_a_id ELSE chart_b_id END),
    (CASE WHEN chart_a_id < chart_b_id THEN chart_b_id ELSE chart_a_id END)
);
COMMIT;
