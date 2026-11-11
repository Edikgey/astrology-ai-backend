"""Real pinned SDK serialization/parsing against an isolated in-memory transport."""
import copy
import json
import time
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
import stripe
import test_my_charts as fixtures
import test_stripe as sf
from database.queries import BillingPeriod, StripeCheckout
from modules import stripe_billing as billing

class StripeSDKContractTests(unittest.TestCase):
    tearDown = fixtures.MyChartsTests.tearDown
    send = sf.StripeTests.send
    def setUp(self):
        fixtures.MyChartsTests.setUp(self)
        env = patch.dict('os.environ', sf.ENV); env.start(); self.addCleanup(env.stop)
        self.calls = []
        self.start = datetime.utcnow().replace(microsecond=0)-timedelta(days=1)
        self.end = self.start+timedelta(days=30)
        self.binding = None
        transport = patch.object(stripe.RequestsClient, 'request', side_effect=self.request)
        transport.start(); self.addCleanup(transport.stop)
    def request(self, method, url, headers, post_data=None, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method,path,headers,post_data))
        self.assertEqual(headers['Stripe-Version'], billing.API_VERSION)
        if path == '/v1/account': value = {'object':'account','id':sf.ACCOUNT}
        elif path.startswith('/v1/prices/'): value = {'object':'price',**sf.price()}
        elif path == '/v1/customers': value = {'object':'customer','id':sf.CUSTOMER,'livemode':True}
        elif path == '/v1/checkout/sessions':
            params = parse_qs(post_data)
            self.assertEqual(params['ui_mode'],['embedded_page'])
            self.assertEqual(params['subscription_data[billing_mode][type]'],['flexible'])
            self.assertFalse(any(key.startswith('payment_method_types') for key in params))
            self.binding = params['metadata[checkout_binding]'][0]
            self.assertEqual(headers['Idempotency-Key'],'checkout:'+self.binding)
            value = {'object':'checkout.session','id':sf.SESSION,'status':'open','livemode':True,
                'client_secret':sf.SESSION+'_secret_test','expires_at':int(time.time())+3600}
        elif path.startswith('/v1/subscriptions/'):
            value = {'object':'subscription',**sf.subscription(self.start,self.end,self.binding,2)}
        elif path.startswith('/v1/invoices/'):
            value = {'object':'invoice',**sf.invoice(self.start,self.end)}
        elif path == '/v1/invoice_payments':
            value = {'object':'list','has_more':False,'data':[{'object':'invoice_payment','id':'inpay_test','invoice':sf.INVOICE,
                'status':'paid','amount_paid':999,'payment':{'type':'payment_intent','payment_intent':'pi_isolated'}}]}
        elif path.startswith('/v1/payment_intents/'):
            value = {'object':'payment_intent','id':'pi_isolated','livemode':True,'status':'succeeded','currency':'usd','customer':sf.CUSTOMER}
        elif path == '/v1/billing_portal/sessions':
            value = {'object':'billing_portal.session','url':'https://billing.stripe.com/p/session/test'}
        else: raise AssertionError('Unexpected isolated request '+path)
        return json.dumps(value),200,{'request-id':'req_isolated'}

    def test_checkout_paid_invoice_and_portal_with_real_sdk_objects(self):
        r = self.client.post('/payments/stripe/checkout',headers=self.other)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['session_id'],sf.SESSION)
        self.assertTrue(self.binding)
        r = self.send(sf.event())
        self.assertEqual(r.status_code,200,r.text)
        state = self.client.get('/account/usage',headers=self.other).json()
        self.assertEqual((state['plan'],state['gpt_messages_limit']),('premium',300))
        self.assertEqual(self.client.post('/payments/stripe/portal',headers=self.other).status_code,200)
        with self.sessions() as db:
            self.assertTrue(db.query(BillingPeriod).one().applied)
            self.assertEqual(db.get(StripeCheckout,self.binding).subscription_id,sf.SUB)

    def test_account_mismatch_blocks_before_checkout_creation(self):
        original = self.request
        def wrong(method,url,headers,post_data=None,**kw):
            if urlsplit(url).path == '/v1/account':
                return json.dumps({'id':'acct_foreign','object':'account'}),200,{}
            return original(method,url,headers,post_data,**kw)
        with patch.object(stripe.RequestsClient,'request',side_effect=wrong):
            r = self.client.post('/payments/stripe/checkout',headers=self.other)
        self.assertEqual(r.status_code,503)
        self.assertFalse(self.calls)
