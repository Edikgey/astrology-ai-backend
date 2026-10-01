"""Conflict recording inside the caller's user-locked webhook transaction.

No financial side effects. A resolved case never authorizes the extra subscription
to replace the primary. External IDs remain available for manual reconciliation.
"""
from datetime import datetime
from database.queries import BillingConflict, LavaCheckout, User


def locked_billing_user(db, user_id):
    # Same row lock as quota, without usage's expiry normalization. A conflict
    # must leave even an expired primary's persisted plan/period untouched.
    return db.query(User).filter_by(id=user_id).populate_existing().with_for_update().one()


def release_legacy_unpaid_owner(db, user):
    """Old Lava releases claimed ownership on failed first payment; no paid IDs cleared."""
    if (user.payment_provider == "lava" and user.plan == "free" and user.subscription_status == "failed"
            and user.current_period_start is None and user.current_period_end is None):
        checkout = db.query(LavaCheckout).filter_by(user_id=user.id, contract_id=user.provider_subscription_id).first()
        if checkout and checkout.state == "failed" and checkout.paid_at is None:
            user.payment_provider = user.provider_customer_id = user.provider_subscription_id = None
            user.subscription_status = None


def conflict_for(db, provider, subscription_id):
    return db.query(BillingConflict).filter_by(provider=provider, subscription_id=subscription_id).first()


def record_conflict(db, user, provider, subscription_id, checkout_id, payment_id, event_id):
    conflict = conflict_for(db, provider, subscription_id)
    if conflict is None:
        conflict = BillingConflict(user_id=user.id, provider=provider,
            subscription_id=subscription_id, checkout_id=checkout_id, payment_id=payment_id,
            primary_provider=user.payment_provider, primary_subscription_id=user.provider_subscription_id,
            first_event_id=event_id, last_event_id=event_id)
        db.add(conflict)
    elif conflict.user_id != user.id or conflict.checkout_id != checkout_id:
        raise ValueError("Conflict ownership mismatch")
    conflict.last_event_id = event_id
    conflict.last_seen_at = datetime.utcnow()
    return "billing_conflict"
