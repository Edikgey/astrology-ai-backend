# Plan / usage rollout (manual, PostgreSQL)

No production database or Alembic state was queried or changed during development.
The repository chain ends at `97a3d969495f`, but its initial revision alters an
existing `natal_charts` table; it does not create a fresh schema. `migrations/env.py`
also uses `alembic.ini` rather than the app's DATABASE_URL. Do not run `alembic
upgrade head`, autogenerate, or stamp blindly for this feature.

## Existing production database: exact path

1. Back up PostgreSQL and restore it to an isolated staging database. Record
   `SELECT version_num FROM alembic_version` if that table exists. Compare tables
   with the current pre-feature models: users, natal_charts (UUID session_token,
   nullable user_id), chart_data, chart_interpretation_data, gpt_messages,
   email_verification_codes. Stop if the actual schema differs. Verify message
   roles: `SELECT role, count(*) FROM gpt_messages GROUP BY role`; only `user`
   contributes, `gpt`/assistant does not. Investigate unexpected role values first.
2. Rehearse **migrations/plan_usage.sql**, then the backend regression/smoke tests
   on that restored database. Inspect the resulting DDL and retain backup counts.
3. In a maintenance window, stop ALL old backend workers/writers (including jobs).
   Execute `psql --set=ON_ERROR_STOP=1 --file=migrations/plan_usage.sql` against the
   explicitly selected target connection. This is the only required schema path
   for this change; the SQL is additive, transactional, and does not alter old rows
   except adding the FREE default on users. No `create_all` or Alembic stamp.
4. Verify all existing users are free, original row counts/content are unchanged,
   and succeeded ledger rows equal retained USER messages joined to owned charts.
   Check uniqueness of source_message_id. Start ONLY the new backend version and
   smoke test auth, guest view, both migration flows, chart limits, JWT-only GPT
   with mock OpenAI, usage denial, and deletion without quota reset.
5. Record this standalone migration in deployment history. Before future Alembic
   upgrades, reconcile/baseline the actual schema explicitly. The legacy Alembic
   version is intentionally unchanged; do not pretend its chain covers this schema.

The SQL has no IF NOT EXISTS: rerunning or applying on a partial schema fails rather
than resetting usage. A failing transaction must be rolled back. Keep the new
columns/table on rollback; reverting to old GPT writers would invalidate accounting,
so keep GPT traffic stopped until recovery. Do not delete accounting data.

For a fresh isolated DB, tests use metadata.create_all. That is NOT a migration for
an existing DB. A complete fresh-production baseline is outside this change.

## Accounting and concurrency

* UTC timestamps follow existing naive-UTC database convention. New users default
  to free in both ORM and SQL; auth input schemas expose no entitlement fields.
* One succeeded ledger row per successful user request. Free lifetime counts all
  successes, including Premium successes after downgrade; assistant rows never count.
  Deleting charts/messages does not delete ledger rows. Already-deleted historical
  messages cannot be reconstructed; backfill counts all retained USER messages.
* SQL backfills existing owned history. A user-locked repair import on usage reads,
  reservation and chart deletion imports legacy USER messages exactly once, also
  covering old guest history after selected migration. GPTMessage is never edited.
* Premium counts entries admitted for its current period_start. Extending the end
  with the same start does not reset quota. A trusted `modules.usage.set_plan` helper
  sets/renews/downgrades plans and rejects overlapping distinct periods. No public
  plan mutation endpoint, scheduler, provider or webhook is added.
* Premium without start <= now < end gets GPT_PERIOD_INVALID (409), available=0.
  Its chart limit still follows its stored plan until a trusted downgrade occurs.
  Downgrade keeps all charts; create/migrate stay blocked above the Free limit.
* Reserve takes the user lock, backfills, counts succeeded + live reservations,
  inserts one lease and commits. OpenAI runs after the prompt cache transaction
  also commits. FastAPI runs the sync endpoint/SQL in a worker, not the event loop.
* Finalize re-locks the user, validates the lease and chart, and atomically commits
  two messages plus succeeded usage. Failure rolls back and releases the lease.
  Commit ambiguity never releases a succeeded entry. If the process dies or release
  cannot reach the DB, a five-minute lease expires. Expired work CANNOT finalize or
  write history. The SDK timeout is 60 seconds with automatic retries disabled.
* In-flight requests keep their admitted plan/period when plans change. Free counts
  all active reservations; a new Premium period excludes prior-period work. No
  automatic POST retries or guaranteed delivery after a disconnected client.

## API

GET /account/usage requires existing JWT auth. Returns plan, saved_charts_used,
saved_charts_limit, gpt_messages_used, gpt_messages_limit, gpt_limit_type,
current_period_start/end, plus gpt_messages_reserved, gpt_messages_available,
gpt_period_valid. All are server-derived; no fake plan/usage data.

409 detail contracts: CHART_LIMIT_REACHED (existing code retained), GPT_LIMIT_REACHED
(plan, used, reserved, limit, message), GPT_PERIOD_INVALID and
GPT_RESERVATION_EXPIRED. Authentication, ownership and migration statuses are unchanged.

Default tests run against isolated SQLite with mock OpenAI. SQLite does not validate
PostgreSQL FOR UPDATE, lock contention, PostgreSQL DDL, or production backfill time.
Before rollout, run concurrent requests at 9/10 and 299/300 in PostgreSQL with separate
connections, failure/release, process-crash expiry, delete during GPT, and plan renewal
during GPT; only one request may reserve the final slot. Check pg_stat_activity while
mock OpenAI waits: no GPT transaction/row lock should remain open.

An opt-in `tests/test_postgres_usage.py` runs the exact SQL against a reconstructed
pre-feature schema and races independent PostgreSQL connections for the final
Free/Premium slot. It only accepts USAGE_TEST_POSTGRES_URL pointing to localhost
and a database named test_* or *_test; it uses a unique temporary schema. It is
skipped when that explicit test URL is absent and never reads DATABASE_URL.

## Local validation completed 2026-09-12

Validated on portable PostgreSQL 17.11 (EDB Windows binaries), listening only on
127.0.0.1:55483, in a freshly initialized `test_usage` database with generated test
credentials. No Windows service, production credentials, or production data were used.

* The exact migration ran against the reconstructed pre-feature schema. Snapshots
  of every original table/column matched after migration, including guest charts,
  both users, six GPT messages, chart data, cached interpretation and verification code.
* Both existing users became FREE with null billing periods. Only the two owned
  USER messages were backfilled; assistant and guest messages did not count.
* Reapplication failed with duplicate-column error 42701 and preserved data/usage.
  Injected failure after backfill (22012) rolled back all new DDL and ledger rows;
  the unchanged script then applied successfully to the recovered schema.
* PostgreSQL pg_blocking_pids confirmed a reservation actually waited on the held
  user FOR UPDATE lock. Releasing the lock unblocked it; no transaction remained
  open afterward. Independent concurrent connections admitted exactly one final
  slot at Free 9/10 and Premium 299/300.
* PostgreSQL-specific suite: 5/5 passed. Full backend discovery with the test URL:
  43/43 passed, zero skips, with mocked OpenAI. All generated test schemas were removed.

This validates the repository baseline and synthetic data, not the unseen actual
production schema or data volume. Backup, schema comparison, restored-copy rehearsal
and the controlled migration window described above remain required before deployment.
