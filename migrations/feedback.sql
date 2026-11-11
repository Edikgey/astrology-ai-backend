-- Additive; apply before deploying the feedback API. No existing data changes.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
CREATE TABLE feedback (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id VARCHAR(36) NOT NULL,
    category VARCHAR(16) NOT NULL,
    message TEXT NOT NULL,
    page_path VARCHAR(200),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT uq_feedback_request UNIQUE(user_id, request_id),
    CONSTRAINT ck_feedback_category CHECK (category IN ('idea', 'bug', 'feedback')),
    CONSTRAINT ck_feedback_message CHECK (length(trim(message)) BETWEEN 1 AND 2000)
);
CREATE INDEX ix_feedback_user_created ON feedback(user_id, created_at);
CREATE INDEX ix_feedback_created_at ON feedback(created_at);
COMMIT;
