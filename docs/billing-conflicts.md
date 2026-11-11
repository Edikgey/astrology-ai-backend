# Multiple paid subscriptions

Pending Stripe and Lava attempts may coexist. First verified paid subscription
committed under the shared user-row lock becomes primary. The other subscription
is recorded in billing_conflicts with provider, subscription, local checkout,
payment and event references; it never changes the primary plan, period or quota.

The unique provider/subscription constraint collapses repeat deliveries into one
case. billing_events records event outcomes and minimal reconciliation references.
No full payment payloads or card data are stored. A resolved case cannot later
become primary merely because another event arrives. Cancellation, refund or
dispute events for a conflicting subscription do not damage primary access.

Stripe additionally scopes checkout/customer/period state to LIVE account ID,
checks the current remote subscription and settled invoice, and retries stale
snapshots using the local revision. Duplicate deliveries are safe on PostgreSQL.
No automatic cancellation or refund is attempted for either provider.

Support must independently verify both provider subscriptions and the user's
requested resolution, perform separately authorized provider actions, confirm
their resulting lifecycle events, and mark the local case resolved. Do not clear
primary ownership or delete the case to switch providers. The Stripe portal API
supports a user's own bound Stripe subscription even when Lava is primary; the
standard usage UI exposes management for primary Stripe accounts. For cross-provider
conflicts support can guide the user through authorized management.

The migration accepts only Stripe/Lava conflict rows and fails transactionally
if an unexpected legacy provider row exists. Recheck production before deployment.
