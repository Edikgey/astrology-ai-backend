# Paddle Sandbox integration — setup and validation

Initial local implementation on 2026-09-12. At that stage, no push, deployment, production variables,
existing database migration, live API requests, test purchases or subscription
cancellations were performed. Runtime API calls in tests use mocks.

Deployment preparation on 2026-09-12: all six PostgreSQL migration/concurrency
checks subsequently passed on local PostgreSQL 18.6. With explicit authorization,
`paddle_billing.sql` was then applied once to the existing Railway production DB.
All billing columns and both Paddle tables were verified; users/charts/messages
counts remained 1/17/18. Backend Sandbox API key/price and frontend client token/price
were installed securely; the existing frontend API URL was preserved. The historical
setup notes below describe initial implementation; never rerun the migration on
this Railway database. Real Sandbox purchase E2E remains a manual step.

## Existing catalog and created Sandbox object

Use the existing Astrology AI Premium product `pro_01m2aaqxfxt8yw5fjgbktt7drx`
and monthly price `pri_01m2aaqxhr6prath62z1efsvvn`: USD 9.99, one month, no trial.
No products or prices were created or changed during this integration task.

The Sandbox token listing contained no active client-side tokens. Created exactly
one: **AstrologyAI Sandbox Frontend**, ID `ctkn_01m2abhjsq8d4f79vt0ccw68ws`.
Its publishable value is saved in the frontend's git-ignored `.env.local`, not in
source. Retrieve it from Paddle Sandbox > Developer tools > Authentication for
your eventual frontend environment. No API key was extracted from MCP.

No notification destination was created: the new webhook endpoint has not been
deployed to a public URL. Existing notification destinations were not modified.

## Environment variables

Backend (see `.env.paddle.example` in the backend root):

| Variable | Value / purpose |
| --- | --- |
| `PADDLE_SANDBOX_API_KEY` | Sandbox API key, beginning `pdl_sdbx_apikey_`; permissions `transaction.write` and `customer_portal_session.write` |
| `PADDLE_WEBHOOK_SECRET` | Endpoint secret of the Sandbox notification destination |
| `PADDLE_PREMIUM_PRICE_ID` | `pri_01m2aaqxhr6prath62z1efsvvn` |

Existing `DATABASE_URL`, `SECRET_KEY`, OpenAI and email configuration stay in place.
Paddle credentials are read at runtime; requests fail closed when configuration is
missing. The client explicitly targets `Environment.SANDBOX` and rejects live keys.

Frontend (see `.env.paddle.example` in the frontend root):

| Variable | Value / purpose |
| --- | --- |
| `REACT_APP_PADDLE_CLIENT_TOKEN` | Publishable `test_...` token described above |
| `REACT_APP_PADDLE_PREMIUM_PRICE_ID` | `pri_01m2aaqxhr6prath62z1efsvvn` |
| `REACT_APP_API_URL` | Existing backend base URL, without trailing slash; for local testing `http://localhost:8000` |

CRA embeds `REACT_APP_*` values at build time: restart development server or rebuild
after changing them. Never put an API key or webhook secret in frontend variables.
Paddle explicitly permits publishing client-side tokens; using env keeps the
account-specific token out of tracked source, but it is intentionally visible in
the browser bundle. `.env.local` is git-ignored and was not committed.

## Database migration

Apply `migrations/paddle_billing.sql` **once**, after the existing `plan_usage.sql`
schema is already present. It uses the project's manual transactional PostgreSQL
SQL approach, not a new Alembic chain. Do not rerun `plan_usage.sql` on an already
migrated database. The existing Railway database is now migrated as noted above.

With the intended database selected and backend writers stopped during migration:

```powershell
psql --set=ON_ERROR_STOP=1 --file=migrations/paddle_billing.sql
```

Added to `users`: `payment_provider`, unique nullable `provider_customer_id` and
`provider_subscription_id`, `subscription_status`, `paddle_updated_at`, and
`scheduled_cancel_at`. Added `paddle_checkouts` (ownership binding, reusable
transaction ID and subscription history) and `paddle_events` (event-id ledger).
All timestamps follow the existing naive-UTC convention. No usage, plan or chart
backfill is needed. Duplicate application fails transactionally instead of resetting
data. The app's `create_all` is not a migration for an existing DB.

## Checkout and webhook behavior

`POST /payments/paddle/checkout` uses existing JWT auth. The backend creates an
automatic-collection transaction containing exactly the configured price and
quantity 1. It sends server-owned `custom_data.user_id` plus a random persisted
`checkout_binding`. The browser opens the returned transaction ID through the
official `@paddle/paddle-js` wrapper, always in Sandbox. Email is only prefill;
it is never used to assign ownership. A user cannot request a different owner,
price, quantity, or plan in the request body.

An unfinished transaction is reused on repeated clicks. A durable intent precedes
the API call; if creation times out or fails ambiguously, automatic retries are
blocked to avoid multiple chargeable checkouts. See recovery below.

`POST /payments/paddle/webhook` does not require JWT. It verifies the exact raw
bytes with official `paddle-python-sdk` 1.15.0 and the destination secret before
parsing JSON. Timestamp tolerance is five seconds, also rejecting future signatures.
Invalid signatures return 401; malformed/mismatched events return 400. DB failures
return non-2xx, so Paddle can retry. Do not disable timestamp checks or enable
sensitive request/response logging in infrastructure.

Subscription updates and event ledger insertion share one database transaction.
Unique `event_id` conflict handling deduplicates concurrent deliveries; a per-user
row lock serializes entitlement changes with existing chart/GPT operations.
Older or equal `occurred_at` values are ignored. Ownership requires a matching
server-created checkout binding initially, then saved subscription/customer IDs.
Unknown owners and conflicting IDs cannot fall back to email or another user.
Late events for an older subscription cannot replace a later subscription.

| Paddle state | Entitlement behavior |
| --- | --- |
| `active`, expected monthly price, quantity 1 | Premium with Paddle `current_billing_period`; renewal selects a new existing usage period |
| Active with scheduled cancel | Premium until `scheduled_change.effective_at`; no immediate downgrade |
| `past_due` | Keep last confirmed Premium period; never extend/reset it from the failed-payment event; show recovery warning |
| `canceled` or `paused` | Free; preserve provider IDs/history and existing charts |
| Unexpected price or trial | Do not grant Premium |

At a scheduled cancellation deadline the existing access paths enforce Free even
if the final webhook is late. The stored downgrade is persisted on the next
successful user-locked account operation; there is no timer writing idle users.
Ordinary expired/missing Premium periods still use the existing GPT fail-safe:
GPT is blocked, while the stored plan/chart limit stays Premium until a confirmed
end/cancellation. This also applies during payment recovery after the paid period.

Limits remain Free 3 charts / 10 lifetime GPT messages and Premium 10 charts /
300 GPT messages per billing period. No second quota ledger exists. Overlapping
distinct period starts are rejected by the existing plan helper. Downgrade never
deletes charts, messages or usage. A downgraded account above 3 charts cannot add
more, but can view its existing charts.

`GET /account/usage` keeps existing fields and adds subscription status, scheduled
cancel date, cancel-at-period-end flag and portal availability. Checkout success
only polls this endpoint for about a minute; it never sets Premium locally. Modal
and pending question state survive checkout. Auth changes invalidate old callbacks.

`POST /payments/paddle/portal` creates a fresh Customer Portal session for the JWT
user's saved customer/subscription IDs. Responses use `Cache-Control: no-store`;
the frontend opens Paddle in a new tab, without an iframe or stored portal token.
Focus/visibility refreshes usage on return. PricePreview displays Paddle's formatted
localized total using IP location; preview failure does not block checkout. Paddle
alone handles exchange rates and final location/tax calculation.

## Manual setup and remaining Sandbox E2E

1. Install updated backend requirements and frontend dependencies.
2. Apply the migration to the intended database; set the three backend variables.
3. Make this backend endpoint publicly reachable by an explicitly approved deploy
   or a local HTTPS tunnel: `https://<backend>/payments/paddle/webhook`. The frontend
   must point to this backend, and its origin must be allowed by existing CORS.
4. In **Sandbox > Developer tools > Notifications**, create a URL destination with
   **`subscription.created` and `subscription.updated`** (the documented minimum,
   covering renewals and status changes). Handler also accepts `subscription.activated`,
   `subscription.resumed`, `subscription.past_due`, `subscription.paused`, and
   `subscription.canceled` if enabled. Use platform traffic for actual sandbox
   checkout events; choose all only if also testing simulations.
5. Put that destination's secret into backend `PADDLE_WEBHOOK_SECRET`. Configure
   Paddle Sandbox's default payment link and permitted website/domain for the
   actual frontend; localhost can be used in Sandbox. No domain settings were
   changed automatically.
6. Set frontend env, rebuild, and sign in as a Free test user. Open Upgrade; confirm
   monthly Premium quantity 1 and no trial. Complete checkout using Paddle's
   Sandbox test card `4242 4242 4242 4242`, a future expiry and a valid test CVC.
7. Confirm notification delivery is 200, user becomes Premium and period dates
   match Paddle. Repeat delivery of the same event to verify no quota reset.
8. Open Manage subscription and schedule cancellation. Verify Premium before the
   paid deadline, Free after the effective end, with charts preserved. Check renewal
   and failed-payment scenarios against the existing test user's real subscription
   or a properly bound Sandbox simulation. Generic simulator fixtures have no local
   checkout binding and intentionally cannot grant access to a random user.

These steps have not yet been exercised against the real Sandbox checkout. Complete
deployment and notification-secret setup before the first manual test purchase.

## Recovery and MVP boundaries

If a checkout is stuck in `creating`, inspect Sandbox transactions using its
`custom_data.checkout_binding` and the local `paddle_checkouts.id`. If found,
reconcile its transaction ID and state after verifying the owner. If Paddle
definitively rejected creation and no transaction exists, mark the intent failed
under the user lock before retrying. Do not clear the intent or issue another
transaction when the provider outcome is unknown. There is no automatic cancellation
or destructive cleanup.

Monitor non-2xx webhook responses in Paddle's notification logs. After missed
deliveries/retry exhaustion, replay the relevant authentic notifications through
Paddle; the ledger/order checks make this safe. No scheduled reconciliation worker
or background payment retry system was added. A changed billing cycle/overlapping
period or mismatched owner returns an error for manual investigation rather than
guessing entitlements. This release intentionally supports one current monthly
Premium subscription per user; paused/past-due accounts manage the existing one.

## Tests and changed files

Backend: `.venv/Scripts/python -m unittest discover -s tests -v` — 61 discovered,
55 passed, 6 opt-in PostgreSQL tests initially skipped (no `USAGE_TEST_POSTGRES_URL`).
The subsequent dedicated PostgreSQL run passed all 6/6 checks, including the exact
Paddle migration and concurrency/locking behavior. Includes
17 new Paddle tests covering actual SDK signature verification, malformed/stale
signatures, activation, duplicate and stale events, renewal, cancel, past_due,
ownership conflicts, transaction serialization, portal ownership and rollback.
SQLite tests do not prove PostgreSQL row-lock behavior or migration execution.
The existing opt-in PostgreSQL suite now also applies/tests the exact Paddle SQL.

Frontend: `npm test -- --watchAll=false --runInBand` — 6 suites, 70 tests passed.
`npm run build` — compiled successfully. Existing Browserslist age warning remains;
backend emits the existing Pydantic `orm_mode` deprecation warning.

Backend files: `.gitignore`, `.env.paddle.example`, `database/queries.py`, `main.py`,
`modules/plans.py`, `modules/usage.py`, `modules/migration.py`, `requirements.txt`,
`api/payments.py`, `modules/paddle_billing.py`, `migrations/paddle_billing.sql`,
`migrations/PADDLE_SANDBOX.md`, `tests/test_paddle.py`, `tests/test_postgres_usage.py`.

Frontend files: `package.json`, `package-lock.json`, `.env.paddle.example`,
`src/api/paddle.js`, `src/context/BillingContext.js`, `src/context/UsageContext.js`,
`src/components/PremiumPrice.js`, `src/components/UsageModal.js`,
`src/components/UsageSummary.js`, `src/components/UsageFlow.test.js`,
`src/components/PaddleCheckout.test.js`, `src/pages/PricingPage.js`.
Local-only configuration: frontend `.env.local` (ignored).

## Documentation checked through paddle-docs MCP

- [Paddle.js and publishable client-side tokens](https://developer.paddle.com/paddle-js/about/)
- [Checkout.open and transactionId](https://developer.paddle.com/paddle-js/methods/paddle-checkout-open/)
- [PricePreview](https://developer.paddle.com/paddle-js/methods/paddle-pricepreview/)
- [Custom data propagation](https://developer.paddle.com/build/transactions/custom-data/)
- [Subscription provisioning and events](https://developer.paddle.com/build/subscriptions/provision-access-webhooks/)
- [Signature verification](https://developer.paddle.com/webhooks/about/signature-verification/)
- [Official Python SDK](https://github.com/PaddleHQ/paddle-python-sdk)
- [Customer Portal sessions](https://developer.paddle.com/api-reference/customer-portals/create-customer-portal-session/)
