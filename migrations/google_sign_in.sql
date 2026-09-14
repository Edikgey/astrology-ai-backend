BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE users ADD COLUMN google_sub VARCHAR(255);
ALTER TABLE users ADD CONSTRAINT uq_users_google_sub UNIQUE (google_sub);
COMMIT;
