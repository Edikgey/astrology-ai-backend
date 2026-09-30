"""Internal entitlement contract, not a simulation of any future provider API."""
import unittest
from datetime import datetime, timedelta, timezone

import test_my_charts as fixtures
from database.queries import User, GPTUsage
from modules import usage
from modules.subscription_access import apply_subscription_access


class SubscriptionAccessTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown

    def test_grant_retain_renew_revoke_share_existing_quota_ledger(self):
        start = datetime.utcnow() - timedelta(days=1)
        end = start + timedelta(days=30)
        with self.sessions() as db:
            user = usage.locked_user(db, 1)
            user.payment_provider = "test-provider"
            apply_subscription_access(db, user, "grant", period_start=start, period_end=end)
            db.add(GPTUsage(user_id=1, chart_id=1000, status="succeeded", plan="premium",
                            period_start=start, period_end=end))
            db.commit()
        for decision in ("grant", "retain"):
            with self.sessions() as db:
                user = usage.locked_user(db, 1)
                kwargs = dict(period_start=start, period_end=end) if decision == "grant" else {}
                apply_subscription_access(db, user, decision, **kwargs)
                self.assertEqual((user.current_period_start, user.current_period_end), (start, end))
                db.commit()
            state = self.client.get("/account/usage", headers=self.owner).json()
            self.assertEqual(state["plan"], "premium")
            self.assertEqual(state["gpt_messages_used"], 1)
            self.assertEqual(state["gpt_messages_available"], 299)
            self.assertFalse(state["can_manage_subscription"])
        with self.sessions() as db:
            user = usage.locked_user(db, 1)
            with self.assertRaises(ValueError):
                apply_subscription_access(db, user, "grant", period_start=start + timedelta(days=1),
                                          period_end=end + timedelta(days=1))
            apply_subscription_access(db, user, "grant", period_start=end,
                                      period_end=end + timedelta(days=30))
            apply_subscription_access(db, user, "revoke")
            db.commit()
            self.assertEqual(user.plan, "free")
            self.assertIsNone(user.current_period_start)
            self.assertEqual(db.query(GPTUsage).filter_by(status="succeeded").count(), 1)
        state = self.client.get("/account/usage", headers=self.owner).json()
        self.assertEqual(state["gpt_messages_used"], 1)
        self.assertEqual(state["gpt_messages_available"], 9)

    def test_confirmed_scheduled_end_is_provider_independent_and_utc(self):
        now = datetime.utcnow()
        with self.sessions() as db:
            user = usage.locked_user(db, 1)
            user.payment_provider = "test-provider"
            apply_subscription_access(db, user, "grant", period_start=now - timedelta(days=2),
                                      period_end=now + timedelta(days=28),
                                      scheduled_cancel_at=(now - timedelta(seconds=1)).replace(tzinfo=timezone.utc))
            db.commit()
        state = self.client.get("/account/usage", headers=self.owner).json()
        self.assertEqual(state["plan"], "free")
        with self.sessions() as db:
            self.assertIsNone(db.get(User, 1).current_period_end)

    def test_retain_cannot_activate_or_extend_and_invalid_decision_is_rejected(self):
        with self.sessions() as db:
            user = usage.locked_user(db, 1)
            apply_subscription_access(db, user, "retain")
            self.assertEqual(user.plan, "free")
            for decision, kwargs in (("unknown", {}), ("grant", {}),
                                     ("retain", {"period_end": datetime.utcnow()})):
                with self.assertRaises(ValueError):
                    apply_subscription_access(db, user, decision, **kwargs)
            self.assertEqual(user.plan, "free")

    def test_unscheduled_expiry_preserves_existing_fail_closed_quota_behavior(self):
        now = datetime.utcnow()
        with self.sessions() as db:
            user = usage.locked_user(db, 1)
            user.payment_provider = "test-provider"
            apply_subscription_access(db, user, "grant", period_start=now - timedelta(days=31),
                                      period_end=now - timedelta(days=1))
            db.commit()
        state = self.client.get("/account/usage", headers=self.owner).json()
        self.assertEqual(state["plan"], "premium")
        self.assertFalse(state["gpt_period_valid"])

    def test_adapter_transaction_rollback_does_not_leave_access_or_schedule(self):
        with self.sessions() as db:
            user = usage.locked_user(db, 1)
            start = datetime.utcnow()
            apply_subscription_access(db, user, "grant", period_start=start,
                                      period_end=start + timedelta(days=30), scheduled_cancel_at=start)
            db.flush()
            db.rollback()
            user = db.get(User, 1)
            self.assertEqual(user.plan, "free")
            self.assertIsNone(user.scheduled_cancel_at)
