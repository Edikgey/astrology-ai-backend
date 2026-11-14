"""PAY-REFUND-EVENT regression, backend. Written before the code; red on c31feeb except the GUARD parts.

Source: tasks/PAY-REFUND-EVENT/DESIGN.md §2-§5. A real Stripe Refund has NO `livemode` (read on the Stripe test
stand), so `checked(refund)` turns every refund.created|updated|failed into a permanent 400. Here every Refund is
built by sf.refund_object() (no `livemode`); the mode is proven by the Refund's charge (`livemode` present).

Real API, real billing and quota modules, isolated SQLite. Fakes only: the Stripe Mock client of test_pricing
(no network). No .env, no .secrets, no real keys, no environment dumps.
GUARD = passes on the base by design (the refusal parts of test 5).
"""
import json
import os
import unittest
from unittest.mock import patch

import stripe
from sqlalchemy import text

import test_pricing as pf
import test_stripe as sf
from database.queries import BillingEvent, BillingPeriod, User

LOGGER = 'modules.stripe_billing'
LINE = 'Stripe webhook rejected '


class RefundBase(pf.PricingBase):
    """Pack and subscription refunds, delivered as the refund.* events Stripe really sends."""

    def granted_pack(self, pi='pi_pack'):
        session_id, pack_id = self.buy_stripe_pack()
        self.assertEqual(self.send(self.pay_stripe_pack(session_id, pi=pi)).status_code, 200)
        return pack_id

    def put_refund(self, pi, refund_id='re_pack1', status='succeeded', amount=pf.STRIPE_PACK_CENTS):
        """Register a real-shaped Refund (no livemode) and its charge; return the refund id."""
        charge = 'ch_' + pi[3:]
        self.charges[charge]['amount_refunded'] = amount
        self.refunds[refund_id] = sf.refund_object(id=refund_id, charge=charge, amount=amount, payment_intent=pi, status=status)
        self.assertNotIn('livemode', self.refunds[refund_id])
        return refund_id

    def deliver(self, kind, object_id, event_id=None):
        response = self.send(self.event(kind, object_id, event_id))
        self.assertEqual(response.status_code, 200, f'{kind}: {response.text}')
        return response.json()['outcome']

    def event_count(self):
        with self.sessions() as db:
            return db.query(BillingEvent).count()


class RefundEventsTests(RefundBase):
    # ---- 1. three events of one refund revoke once (contour 2)
    def three_events(self, order):
        pack_id = self.granted_pack()
        refund_id = self.put_refund('pi_pack')
        objects = {'refund.created': refund_id, 'refund.updated': refund_id, 'charge.refunded': 'ch_pack'}
        ids = {kind: 'evt_pre1' + kind.replace('.', '') for kind in objects}
        outcomes, revoked_at = [], None
        for kind in order:
            outcomes.append(self.deliver(kind, objects[kind], ids[kind]))
            row = self.pack(pack_id)
            self.assertEqual((row['state'], row['revoke_reason']), ('revoked', 'refund'))
            revoked_at = revoked_at or row['revoked_at']
            self.assertEqual(row['revoked_at'], revoked_at, 'a later event of the same refund moved revoked_at')
        self.assertEqual(outcomes, ['pack_revoked', 'pack_financial_recorded', 'pack_financial_recorded'])
        for kind in order:  # redelivery: duplicate, before any Stripe read changes anything
            self.assertEqual(self.deliver(kind, objects[kind], ids[kind]), 'duplicate')
        self.assertEqual(self.pack(pack_id)['revoked_at'], revoked_at)
        self.assertEqual(self.event_count(), 4)  # the grant event + three refund events

    def test_pre1_three_events_of_one_pack_refund_revoke_once(self):
        self.three_events(['refund.created', 'charge.refunded', 'refund.updated'])

    def test_pre1_charge_refunded_first_then_refund_events(self):
        self.three_events(['charge.refunded', 'refund.created', 'refund.updated'])

    # ---- 2. a failed or canceled refund neither revokes nor restores (contour 1)
    def test_pre2_failed_or_canceled_refund_neither_revokes_nor_restores(self):
        pack_id = self.granted_pack()
        failed = self.put_refund('pi_pack', 're_failed', status='failed')
        self.assertEqual(self.deliver('refund.failed', failed), 'pack_financial_recorded')
        canceled = self.put_refund('pi_pack', 're_canceled', status='canceled')
        self.assertEqual(self.deliver('refund.updated', canceled), 'pack_financial_recorded')
        self.assertEqual(self.pack(pack_id)['state'], 'paid')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], pf.PACK_SIZE)
        # revoked by a real refund: a later failed one is only a record and restores nothing
        ok = self.put_refund('pi_pack', 're_ok')
        self.assertEqual(self.deliver('refund.created', ok), 'pack_revoked')
        again = self.put_refund('pi_pack', 're_failed_later', status='failed')
        self.assertEqual(self.deliver('refund.failed', again), 'pack_financial_recorded')
        row = self.pack(pack_id)
        self.assertEqual((row['state'], row['revoke_reason']), ('revoked', 'refund'))
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], 0)

    # ---- 3. subscription refunds are records only (contours 1, 2)
    def test_pre3_subscription_refund_events_are_records_only(self):
        self.bind_subscription()
        self.assertEqual(self.deliver('invoice.paid', sf.INVOICE), 'granted')
        with self.sessions() as db:
            user = db.get(User, 1)
            before = (user.plan, user.current_period_start, user.current_period_end)
        used = self.usage_of(self.owner)['gpt_messages_used']
        cases = [('refund.created', 'pending', 999), ('refund.updated', 'succeeded', 100),
                 ('refund.updated', 'succeeded', 999), ('refund.failed', 'failed', 999)]
        for number, (kind, status, amount) in enumerate(cases):
            self.refunds['re_sub'] = sf.refund_object(id='re_sub', status=status, amount=amount)
            self.assertEqual(self.deliver(kind, 're_sub', 'evt_pre3_%d' % number), 'financial_recorded', (kind, status, amount))
            state = self.usage_of(self.owner)
            self.assertEqual((state['plan'], state['gpt_messages_used']), ('premium', used))
        with self.sessions() as db:
            user = db.get(User, 1)
            self.assertEqual((user.plan, user.current_period_start, user.current_period_end), before)
            self.assertEqual(db.query(BillingPeriod).count(), 1)


# ---- 4. Stripe test mode, as the stand does it (contour 5)
class RefundTestModeTests(RefundBase):
    LIVEMODE = False
    ENVIRONMENT = pf.TEST_ENV

    def test_pre4_test_mode_pack_refund_like_the_stand(self):
        pack_id = self.granted_pack()
        refund_id = self.put_refund('pi_pack')
        self.assertEqual(self.deliver('refund.created', refund_id), 'pack_revoked')
        self.assertEqual(self.pack(pack_id)['state'], 'revoked')
        with self.sessions() as db:
            keys = [e.event_id for e in db.query(BillingEvent).filter_by(event_type='refund.created')]
        self.assertEqual(len(keys), 1)
        self.assertIn(':test:', keys[0])
        self.assertNotIn(':live:', keys[0])


class RefundModeProofTests(RefundBase):
    """5. The mode of a Refund is proven by its charge (contour 1). Refusal parts are GUARDS (green on the base)."""

    def setUp(self):
        super().setUp()
        self.pack_id = self.granted_pack()
        self.before_events = self.event_count()
        self.refund_id = self.put_refund('pi_pack')

    def assert_nothing_changed(self):
        self.assertEqual(self.event_count(), self.before_events)
        self.assertEqual(self.pack(self.pack_id)['state'], 'paid')
        self.assertEqual(self.usage_of(self.other)['plan'], 'free')
        self.assertEqual(self.usage_of(self.other)['pack_questions_remaining'], pf.PACK_SIZE)

    def test_pre5_refund_mode_is_proven_by_its_charge(self):
        self.charges['ch_pack']['livemode'] = not self.livemode  # GUARD: a charge of the other mode
        response = self.send(self.event('refund.created', self.refund_id))
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_changed()
        self.charges['ch_pack']['livemode'] = self.livemode
        self.refunds[self.refund_id]['charge'] = None  # GUARD: a Refund without a charge
        response = self.send(self.event('refund.created', self.refund_id))
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_changed()
        self.refunds[self.refund_id]['charge'] = 'ch_pack'  # positive control: own-mode charge
        self.assertEqual(self.deliver('refund.created', self.refund_id), 'pack_revoked')

    def test_pre5_a_refund_that_claims_livemode_is_not_trusted_over_its_charge(self):
        # Even if a Refund object ever carried livemode, the charge of the other mode still refuses it.
        self.refunds[self.refund_id]['livemode'] = self.livemode
        self.charges['ch_pack']['livemode'] = not self.livemode
        self.assertEqual(self.send(self.event('refund.created', self.refund_id)).status_code, 400)
        self.assert_nothing_changed()


class RefundRejectionLogTests(RefundBase):
    """6. Every rejected event writes one safe line (contour 4): closed set of fields, no private text."""
    ALLOWED = {'status', 'type', 'event_id', 'exception_class', 'reason', 'http_status', 'code', 'request_id'}

    def setUp(self):
        super().setUp()
        self.granted_pack()
        self.refund_id = self.put_refund('pi_pack')

    def lines(self, logs):
        found = [line for line in logs.output if LINE in line]
        self.assertEqual(len(found), 1, logs.output)
        return json.loads(found[0].split(LINE, 1)[1])

    def test_pre6_rejected_event_writes_one_safe_line(self):
        event_id = 'evt_pre6wrongmode'
        # (a) wrong-mode charge -> 400 and one line
        self.charges['ch_pack']['livemode'] = not self.livemode
        with self.assertLogs(LOGGER, level='WARNING') as logs:
            response = self.send(self.event('refund.created', self.refund_id, event_id))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.lines(logs), {'status': 400, 'type': 'refund.created', 'exception_class': 'ValueError',
                                            'reason': 'Wrong payment environment', 'event_id': event_id})
        self.charges['ch_pack']['livemode'] = self.livemode

    def test_pre6_stripe_error_is_503_with_safe_fields_only(self):
        private = ('private-email@example.com', 'sk_live_sensitive')
        error = stripe.PermissionError(' '.join(private), http_status=403, headers={'request-id': 'req_isolated'})
        self.api.refunds.retrieve.side_effect = error
        with self.assertLogs(LOGGER, level='WARNING') as logs:
            response = self.send(self.event('refund.created', self.refund_id, 'evt_pre6stripe'))
        self.assertEqual(response.status_code, 503, response.text)
        line = self.lines(logs)
        self.assertEqual((line['status'], line['type'], line['exception_class'], line['http_status'], line['request_id']),
                         (503, 'refund.created', 'PermissionError', 403, 'req_isolated'))
        self.assertNotIn('reason', line)
        self.assertLessEqual(set(line), self.ALLOWED)
        for value in private:
            self.assertNotIn(value, str(logs.output) + response.text)

    def test_pre6_unknown_error_text_is_never_logged_or_returned(self):
        self.api.refunds.retrieve.side_effect = ValueError('Ivan Private')
        with self.assertLogs(LOGGER, level='WARNING') as logs:
            response = self.send(self.event('refund.created', self.refund_id, 'evt_pre6private'))
        self.assertEqual(response.status_code, 400, response.text)
        line = self.lines(logs)
        self.assertEqual((line['status'], line['exception_class'], line['reason']), (400, 'ValueError', 'unclassified'))
        self.assertLessEqual(set(line), self.ALLOWED)
        self.assertNotIn('Ivan Private', str(logs.output) + response.text)

    def test_pre6_unsafe_event_id_and_unknown_type_are_not_copied(self):
        value = self.event('refund.created', self.refund_id)
        value['id'] = 'evt_bad id; Ivan Private'  # fails the identifier check -> 400, id must not reach the log
        with self.assertLogs(LOGGER, level='WARNING') as logs:
            response = self.send(value)
        self.assertEqual(response.status_code, 400, response.text)
        line = self.lines(logs)
        self.assertNotIn('event_id', line)
        self.assertEqual((line['status'], line['reason']), (400, 'Invalid identifier'))
        self.assertNotIn('Ivan Private', str(logs.output))

    def test_pre6_successful_refund_writes_no_rejection_line(self):
        with self.assertLogs(LOGGER, level='DEBUG') as logs:
            import logging
            logging.getLogger(LOGGER).debug('probe')  # assertLogs needs one record to exist
            self.assertEqual(self.deliver('refund.created', self.refund_id), 'pack_revoked')
        self.assertEqual([line for line in logs.output if LINE in line], [])


if __name__ == '__main__':
    unittest.main()
