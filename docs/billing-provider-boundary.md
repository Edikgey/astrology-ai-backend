# Paddle + Lava billing

## Verified contract (2026-09-29)

Sources: [official docs](https://developers.lava.top/en),
[Swagger](https://gate.lava.top/docs),
[OpenAPI](https://gate.lava.top/docs/documentation.yaml).

Authenticated read-only catalog verification found subscription product
`915df4a1-b764-490a-8437-c6acaf3e02f7`, offer **Lunaria Premium**
`ef8fea7a-31fc-453a-ae6f-900abf979a2c`, fixed **799 RUB / MONTHLY**.
The creator corrected the initial USD-denominated price. These are deployment
values, not hardcoded application IDs. Live catalog items are flat; Swagger also
describes an `items[].data` wrapper. Both formats are supported.

Checkout needs `offerId`; product ID is derived from its catalog entry.
Before creating an invoice, the backend verifies price, currency, interval and
subscription type. No browser amount, quantity, offer or identity is accepted.
Lava does not have a quantity field in this request: one invoice buys one offer.
Paddle continues to send exactly one item with quantity 1.

Direct HTTP uses existing httpx. Current SDK wrappers lack return-URL fields.
The API host is fixed, redirects disabled, timeouts explicit, and errors sanitized.
There are no automatic invoice/cancellation retries; 429 returns a retry-later error.

A controlled real invoice POST returned **HTTP 201**, without payment. The prior
example.invalid attempt failed; read-only search found no technical invoice
before the example.com attempt. The accepted unpaid invoice was not returned by
the subsequent list query. Its response details and paid lifecycle still require
the controlled E2E procedure below. Do not create duplicates simply to obtain
another response. No live refund, cancellation or product edit was performed.

## Architecture

Paddle remains the default international flow (the existing repository uses
Sandbox); Lava adds the RUB option for the **same Premium entitlement**.

Both adapters call `apply_subscription_access` under the User-row lock. Its
grant/revoke/retain decisions use the existing `apply_plan`, preserving period
overlap safeguards, account-wide quota, reservations and chart limits. No second
Premium/quota system exists. Scheduled cancellation is provider-independent.
Unscheduled expiry keeps existing behavior: Premium chart limits remain, while
GPT fails closed without a valid paid period.

User has one billing owner. Customer/subscription uniqueness is now
`(payment_provider, provider_customer_id)` and
`(payment_provider, provider_subscription_id)`; non-null IDs require a provider.
Lava uses the parent contract ID as subscription identity. No stable customer ID
is exposed by these APIs, so Lava's customer ID remains NULL. Email is an immutable
checkout cross-check, never an account identity lookup.

`lava_checkouts` persists User FK, local intent ID, unique returned contract ID,
verified offer/product, buyer email, URL, lifecycle watermark, last paid timestamp
and cancellation-request flag. Completed bindings stay for late-event protection.
Paddle/Lava transaction IDs are in separate provider-exclusive tables.

## Migration and rollout compatibility

Apply `migrations/lava_billing.sql` once after `paddle_billing.sql`. It uses one
transaction and a 5-second lock timeout. Preflight rejects unowned billing IDs.
Composite uniqueness is added before old global constraints are dropped.
The migration adds Lava checkout storage and extends the existing event ledger.
It does not rewrite Paddle IDs, plans, periods, users, charts, messages or quota.

The physical `paddle_events` table, `event_id` PK and all existing Paddle IDs
remain intact. Old `ON CONFLICT(event_id)` works after migration. Added
`provider` defaults to paddle; nullable `details` stores minimal financial audit.
New Lava IDs use `lava:` + stable external event ID or legacy-event hash.
Paddle validates `evt_*`, which cannot collide with this namespace.
There is one shared event ledger, not a competing Lava ledger.

Safe operator sequence: migrate, deploy **all backend workers** and frontend,
then configure/enable Lava. Keep Lava configuration absent until that completes:
old binaries' provider-unscoped queries must not process a mixed-provider
population. Reapplication fails atomically. This project has no SQL downgrade
convention; transactional failure rollback is tested. After mixed-provider data
exists, do not restore global uniqueness or roll back to the old billing binary.
No production migration/deployment was performed in this task.

## Checkout and returns

`POST /payments/lava/checkout` requires the existing app JWT. Under the User lock
it rejects another provider, active subscription or pending checkout. Catalog
HTTP happens outside a transaction; state is rechecked under lock before saving
an intent. Then `POST /api/v3/invoice` receives server-owned email/offer, RUB,
MONTHLY and return URLs. The result must contain a UUID contract, HTTPS payment URL
and 799 RUB amount. Only the URL is returned to React.

Pending verified URLs are reused. Ambiguous HTTP/storage failures preserve the
intent and block automatic second creation; support must reconcile the invoice.
A webhook arriving before the contract binding commits returns 503 for retry.
There is no email-based fallback or client-provided custom identity.

The existing upgrade modal retains Paddle and adds Lava 799 RUB/month when backend
configuration is present. A separate tab preserves the current question/draft.
No unverified MIR/SBP claims are made. Return navigation uses existing
`/my-charts?billing=lava`. Query `status`/`invoiceId` never grants access;
bounded polling reads authoritative `/account/usage`.

## Webhook and event semantics

`POST /payments/lava/webhook` checks `X-Api-Key` in constant time against
`LAVA_WEBHOOK_SECRET`. Missing/invalid authentication -> 401; unconfigured -> 503.
Flat camelCase and envelope snake_case payloads have separate validated parsers.
Malformed known events -> 400 without access changes; unknown future types -> 200.
Successful acknowledgement follows the atomic ledger/access commit.

Legacy idempotency hashes type, contract, parent contract and normalized provider
timestamp; financial events use stable `event_id`. All Lava keys are namespaced.
A retried renewal never adds duration relative to local time or resets quota.

HTTP reads precede row locking. `GET /api/v1/subscriptions/{parentContractId}`
supplies exact expiry, completed payments, cancellation and termination dates.
A cancellation with a child contract is resolved using
`GET /api/v2/invoices/{id}`. No 30-day approximation is used. A success event
must appear in the API's completed payments before acknowledgement; API lag -> 503.
Older events cannot overwrite newer lifecycle state. Monotonic paid timestamps
and cancellation guards also reject stale API snapshots fetched concurrently.

| Event | Access effect |
| --- | --- |
| payment.success | Verify bound contract and completed API payment; grant shared Premium period. |
| payment.failed | No grant from the event, no unpaid extension; reconcile authenticated provider state. |
| subscription.recurring.payment.success | Reconcile latest completed payment and exact expiry once. |
| subscription.recurring.payment.failed | Preserve paid period; past_due when API reports failure, GPT closes after expiry. |
| subscription.cancelled | Schedule confirmed paid expiry, or revoke for confirmed termination. |
| refund.success | Audit full/partial amount, reference and cancellation flag; false does not revoke. |
| chargeback.initiated | Audit disputed amount/reference independently of refunds; no invented final outcome. |

Financial envelopes have product/tier/email but **no contract ID**. When
subscription_cancelled=true, a single known local binding may trigger an API read
of that contract. Access changes only on confirmed cancellation/termination.
Ambiguous/unmatched events become `recorded_unmatched` for operator review;
the separately selected subscription.cancelled event remains authoritative.
Refunds never call cancel. Partial refunds remain separate audit events.
Separate cancellation delivery is safe and cannot reset quota.

## Ownership and management

Neither provider can mutate the other's billing state, even with colliding IDs.
Provider-specific queries use provider scope or provider-exclusive checkout tables.
Pending invoices block concurrent cross-provider checkout. Switching, even from
an old canceled provider, is blocked; no automatic takeover/cancellation.
Same-provider repurchase is permitted after confirmed ended/failed access, while
completed old contracts cannot reclaim a newer subscription.

Paddle portal remains Paddle-only. Lava users get explicit cancellation confirmation
before `POST /payments/lava/cancel`. It calls documented
`DELETE /api/v1/subscriptions?contractId=...&email=...` with saved server values.
The intent is persisted before HTTP to suppress concurrent/ambiguous repeats.
The webhook supplies the effective end; HTTP success does not guess expiry.
An ambiguous/failed cancellation needs support verification and, if appropriate,
resetting the intent after checking provider state. It is not confirmed cancellation.

## Configuration and manual setup

Backend only; see `.env.lava.example`. No new frontend env variables.

- `LAVA_API_KEY`: creator API secret.
- `LAVA_WEBHOOK_SECRET`: separate random webhook secret, 32–80 characters.
- `LAVA_OFFER_ID`: verified offer UUID above, public configuration.
- `LAVA_SUCCESS_RETURN_URL`, `LAVA_FAILURE_RETURN_URL`, `LAVA_CANCEL_RETURN_URL`:
  all may use `https://astrology-ai-frontend-production.up.railway.app/my-charts?billing=lava`.
  Each must be absolute HTTPS, at most 512 characters.

Generate the webhook secret with a secure password manager or locally with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Copy directly to Railway and Lava, never chat/Git/logs. It is not the creator key.
Existing Paddle variables/configuration stay unchanged.

After separate rollout authorization:

1. Verify the existing offer remains fixed at 799 RUB monthly. No new product or
   Paddle price is needed. Verify hosted payment methods before advertising them.
2. Apply migration once. Deploy backend/frontend with Lava still unconfigured;
   wait for every backend worker to run the new version and verify Paddle.
3. Add the six backend variables in Railway, then redeploy backend. Never prefix
   secrets with REACT_APP_; leave frontend API URL/Paddle variables unchanged.
4. Lava **Integrations -> API**: choose the same creator key and **Add Webhook**.
   URL: `https://astrology-ai-backend-production.up.railway.app/payments/lava/webhook`.
5. Authentication: **Your service's API key**, value = LAVA_WEBHOOK_SECRET.
   Select all seven events from the table above. Current docs support one
   destination with all events, replacing the old two-destination instructions.
6. Inspect **Integrations -> API -> Webhook history**. Endpoint must be reachable
   over HTTPS; docs list sender 158.160.60.174. Retain header authentication even
   if adding an IP allowlist. Monitor recorded_unmatched and failed deliveries.

## Controlled verification after deployment

Use a dedicated Free account without a provider/pending checkout. Open RUB checkout
once and verify **799 RUB/month on the actual hosted page before paying**.
Closing/returning without payment must leave Free. Only an explicitly authorized,
human-completed payment should activate Premium. Verify one bound subscription,
exact API period and shared 300-per-period quota; replay its webhook and check that
usage does not reset. Only with separate authorization, cancel that test subscription
and verify paid-period access followed by expiry. Refunds/chargebacks stay mocked
unless real-funds testing is separately authorized. Verify Paddle quantity 1/portal.

Tests use fake credentials and isolated SQLite/PostgreSQL; the existing disposable
PostgreSQL runner is outside both repositories under .pg-rehearsal-20260912.
Use PYTHONIOENCODING=utf-8 for Windows astrology test output. This task does not
commit, push, deploy, apply production SQL or perform real payments.
