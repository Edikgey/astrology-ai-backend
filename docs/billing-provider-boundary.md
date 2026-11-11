# Stripe and Lava billing

Stripe is the international provider: MyLunaria AI Premium costs USD 9.99 per
calendar billing month, quantity one, with 300 successful AI questions and ten
saved natal charts. Free remains three charts and ten lifetime successful AI
questions. Questions across natal and relationship conversations share the
account quota. There is no trial, metered usage, promotion code or plan switching.

Lava remains RUB 799/month. Its offer verification, hosted checkout, signed
events, reconciliation, refund/chargeback handling, cancellation, ownership
checks and access policy are unchanged. Only its event model/table has been
renamed to BillingEvent/billing_events. Both adapters use the same user-row lock
and the existing subscription-access and usage implementation.

Stripe routes are POST /payments/stripe/checkout, GET
/payments/stripe/checkout/{session_id}, POST /payments/stripe/portal and POST
/payments/stripe/webhook. The first three require the account JWT. Browser-supplied
prices, customer IDs, subscription IDs and user IDs cannot select another account.

Checkout persists an intent and a user/account/live-scoped customer binding before
external writes. Stable idempotency keys recover ambiguous requests. Intents older
than 23 hours without a confirmed response require support reconciliation before
a new attempt (Stripe idempotency retention is not indefinite). A confirmed
expired session permits a new attempt. Pending attempts at both providers can
coexist; paid ownership is chosen only by the webhook transaction.

The pinned stack is stripe-python 16.0.0 / API 2026-09-30.endive, stripe-js 10.0.0
and react-stripe-js 7.0.0. Checkout uses ui_mode=embedded_page and flexible
subscription billing. Only card is requested; supported wallets are displayed by
Stripe according to account, domain, device and browser eligibility. The browser
passes the server-returned client secret directly to EmbeddedCheckoutProvider.
Raw card data never traverses Lunaria's backend. Browser completion and return
URLs only start polling authenticated /account/usage.

STRIPE_CHECKOUT_ENABLED defaults false and stops new checkout creation without
stopping signed webhooks or the authenticated portal. Backend credentials must
be LIVE keys for the configured account; /v1/account is checked before each
operation. Account, environment, API version and fixed price are verified.
Stripe API reads run outside the user lock with a 10-second transport timeout.
The checkout revision rejects concurrent stale snapshots with retryable 503.

Webhook bodies are bounded to 256 KiB. The official SDK verifies the exact raw
body and endpoint secret; signatures outside a five-minute window are rejected.
The endpoint retrieves authoritative current subscription/invoice/payment
objects. A paid, bound, USD invoice for the configured recurring item, with a
successful matching PaymentIntent, is required for allocation. Manual,
out-of-band, zero-value, unrelated, prorated or unexpected line items cannot
grant Premium. Future periods are not granted early.

A billing_periods row uniquely identifies a paid invoice and subscription period.
Within one transaction the user lock arbitrates primary ownership, records the
event/period/conflict and applies the entitlement. Repeated events or invoices
do not reset usage. Old periods cannot replace newer periods; overlapping periods
fail closed. Recovery from unpaid/paused to a verified active paid period restores
access without resetting counters. Terminal subscriptions cannot resurrect.

Failed first payment grants nothing. Failed renewal retains the existing paid
period; AI usage is blocked when it expires. Scheduled cancellation retains access
until the confirmed end. Current canceled/unpaid/paused terminal access is revoked.
Archived prices still allow lifecycle reconciliation; new sales require an active price.

Refund and dispute events record minimal references/status/amounts after provider
verification. They do not automatically refund, cancel, grant quota or revoke
access: subscription lifecycle controls access. Ambiguous financial ownership
is recorded as unmatched without guessing by email. Support must resolve refund
or dispute access policy and concurrent paid subscriptions manually. See
[billing conflicts](billing-conflicts.md).

The customer portal only uses the current user's scoped Stripe customer and a
locally bound subscription. The portal configuration permits payment-method
updates, invoices and end-of-period cancellation, with plan/quantity changes disabled.
Its return URL is server-configured and Stripe-hosted destination is checked.

Apply migrations/stripe_billing.sql after the existing billing migrations while
all old backend workers are drained. This is a coordinated cutover, not a rolling
rename. It preserves every user, usage record, Lava record and event ID. See
[implementation report and approval gates](STRIPE_MIGRATION_REPORT.md) for the
LIVE inventory, exact configuration, rollout and rollback.

Reproduce: backend .venv/Scripts/python -m unittest discover -s tests -q.
Set USAGE_TEST_POSTGRES_URL only to a disposable localhost test_* database to
include PostgreSQL suites; they create/drop random schemas. The workspace runner
.pg-rehearsal-20260912/run-stripe-postgres-check.ps1 starts/stops a disposable
cluster on 127.0.0.1:55487 and runs all tests. Frontend: CI=true npm test --
--watchAll=false --runInBand, followed by npm run build. SDK tests use an in-memory
transport with fake credentials; no Stripe objects or charges are created.
