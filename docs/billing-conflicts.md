# Pending checkout switching and manual billing conflicts

Apply `migrations/billing_conflicts.sql` once before deploying this code. It only
creates the conflict table/index; existing users, plans, usage and attempts stay intact.

Pending Lava and Paddle attempts may coexist. Switching does not cancel an external
invoice or erase its identifiers. Each provider still reuses its own pending checkout
and blocks a second ambiguous create request. New checkout creation is blocked by an
existing paid subscription. Provider requests already in flight cannot be recalled;
their identifiers are retained and any later successful payment is arbitrated normally.

The first verified paid subscription committed under the user row lock becomes the
primary. Paddle verifies the original server-bound transaction is completed before
initial activation; Lava verifies its saved contract through the subscription API.
Unpaid events cannot claim the primary slot. Existing same-provider resubscribe after
ended access remains supported. A recorded conflicting subscription never takes over.

An additional confirmed subscription creates one `billing_conflicts` row per
`(provider, subscription_id)`, atomically with the provider event. It never grants
extra quota, changes the primary's paid period, or replaces its provider identifiers.
The event ledger retains subsequent payment/lifecycle events and external references.
Duplicate deliveries do not duplicate cases. A failed DB write leaves the webhook
retryable. A valid recorded conflict returns HTTP 200, not an artificial retry loop.

## Manual review (no automated financial actions)

Use restricted database access to list `billing_conflicts WHERE status = 'open'`.
Each case contains user ID, conflicting provider/subscription/checkout/payment IDs,
primary provider/subscription IDs, and first/last success event IDs. No credentials
or payment bodies are stored. Join `paddle_events` by these event IDs for outcomes;
other related events carry subscription/checkout references in `details`.

Inspect both external subscriptions and payments in the provider dashboards. Decide
and carry out cancellation/refund manually, then verify the provider's result before
marking the case `resolved`. Cancellation is not a refund. Keep the record and IDs.
Neither an open nor resolved conflict is allowed to modify the primary subscription.
Ambiguous Lava refund envelopes without a unique contract match stay recorded for
manual reconciliation; never guess ownership from email alone.

There is no automatic refund, cancellation, outbox, worker, notification or dashboard
in this MVP. Open cases require an operator to check the table. External recurring
charges can continue until an operator explicitly cancels the conflicting subscription.
