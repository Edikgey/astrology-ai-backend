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
CREATE TABLE relationship_messages (
    id SERIAL PRIMARY KEY,
    relationship_id INTEGER NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
    role VARCHAR NOT NULL CONSTRAINT ck_relationship_messages_role CHECK (role IN ('user', 'gpt', 'assistant')),
    content VARCHAR NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX ix_relationship_messages_subject_id ON relationship_messages(relationship_id, id);
CREATE TABLE relationship_conversations (
    relationship_id INTEGER PRIMARY KEY REFERENCES relationships(id) ON DELETE CASCADE,
    summary VARCHAR NOT NULL,
    through_message_id INTEGER NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

-- Existing natal rows already satisfy XOR; no history backfill or data rewrite.
-- Keep both subject IDs without FKs so deleting content never restores quota.
ALTER TABLE gpt_usage ALTER COLUMN chart_id DROP NOT NULL;
ALTER TABLE gpt_usage ADD COLUMN relationship_id INTEGER,
    ADD COLUMN source_relationship_message_id INTEGER UNIQUE REFERENCES relationship_messages(id) ON DELETE SET NULL,
    ADD CONSTRAINT ck_gpt_usage_subject CHECK (
        (chart_id IS NOT NULL AND relationship_id IS NULL) OR
        (chart_id IS NULL AND relationship_id IS NOT NULL)),
    ADD CONSTRAINT ck_gpt_usage_source_subject CHECK (
        (source_message_id IS NULL OR chart_id IS NOT NULL) AND
        (source_relationship_message_id IS NULL OR relationship_id IS NOT NULL));
CREATE INDEX ix_gpt_usage_relationship ON gpt_usage(user_id, relationship_id, status);
COMMIT;
