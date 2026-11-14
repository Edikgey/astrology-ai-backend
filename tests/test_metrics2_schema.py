"""METRICS-2 regression B1-B2: wire contract of the new analytics events (DESIGN §1).

Pure schema checks: no database, network, keys or production data.
B3 (forum campaign sources) was removed by the owner on 2026-11-14: CAMPAIGN_SOURCES stays as on base.
"""
import unittest
from uuid import uuid4
from pydantic import ValidationError
from modules import product_analytics_schema as schema
from modules.product_analytics_schema import ClientEvent, checked_properties

NEW_SERVER = {
    'guest_chart_saved': {'method': ('email', 'google')},
    'payment_failed': {'provider': ('stripe', 'lava'),
                       'stage': ('checkout', 'first_payment', 'renewal', 'confirmation')},
    'free_limit_would_block': {'resource': ('chart', 'question')},
}
WHEEL_OBJECTS = ('point', 'house', 'aspect')
NEW_CLIENT = {
    'wheel_tooltip_opened': {'object': WHEEL_OBJECTS},
    'wheel_question_selected': {'object': WHEEL_OBJECTS},
    'site_error_occurred': {'kind': ('render', 'script', 'request')},
}
# Strings that must never be accepted as an enum value: content, ids, amounts, raw errors.
TEXTS = ('CANARY free text', 'a@example.test', 'in_CANARY', '999', '799.00', 'Солнце', 'Овен',
         'TypeError: x is undefined', 'https://mylunariaai.com/chart/1', '', ' point', 'POINT')


def record(**overrides):
    return dict(event_id=str(uuid4()), session_id=str(uuid4()), name='page_viewed', route_key='home',
                viewport_class='unknown', campaign_source='unspecified', properties={}) | overrides


def combinations(spec):
    keys = list(spec)
    result = [{}]
    for key in keys:
        result = [item | {key: value} for item in result for value in spec[key]]
    return result


class Metrics2ServerSchemaTests(unittest.TestCase):
    """B1. Three new server events, exact closed properties."""

    def test_b1_new_server_events_are_declared_with_exact_closed_values(self):
        for name, spec in NEW_SERVER.items():
            with self.subTest(name=name):
                self.assertIn(name, schema.SERVER_EVENTS, f'{name} is not a server event')
                self.assertEqual({k: tuple(v) for k, v in schema.SERVER_EVENTS[name].items()}, spec)
                self.assertNotIn(name, schema.CLIENT_EVENTS)

    def test_b1_every_allowed_value_passes_checked_properties(self):
        for name, spec in NEW_SERVER.items():
            for properties in combinations(spec):
                with self.subTest(name=name, properties=properties):
                    self.assertEqual(checked_properties(name, properties, source='server'), properties)

    def test_b1_extra_key_foreign_value_or_text_is_rejected(self):
        for name, spec in NEW_SERVER.items():
            valid = {key: values[0] for key, values in spec.items()}
            checked_properties(name, valid, source='server')  # precondition: the event exists
            for extra in ('email', 'invoice_id', 'amount', 'user_id', 'chart_id', 'message', 'question', 'plan'):
                with self.subTest(name=name, extra=extra), self.assertRaises(ValueError):
                    checked_properties(name, valid | {extra: 'CANARY'}, source='server')
            for key in spec:
                for text in TEXTS + ('paddle', 'stripe_live', 'renewal_2'):
                    with self.subTest(name=name, key=key, text=text), self.assertRaises(ValueError):
                        checked_properties(name, valid | {key: text}, source='server')
                with self.subTest(name=name, missing=key), self.assertRaises(ValueError):
                    checked_properties(name, {k: v for k, v in valid.items() if k != key}, source='server')
                with self.subTest(name=name, key=key, non_string=True), self.assertRaises(ValueError):
                    checked_properties(name, valid | {key: 1}, source='server')
            with self.subTest(name=name, source='client'), self.assertRaises(ValueError):
                checked_properties(name, valid, source='client')


class Metrics2ClientSchemaTests(unittest.TestCase):
    """B2. Three new client events through ClientEvent; texts rejected; server names refused from the browser."""

    def test_b2_new_client_events_are_declared_with_exact_closed_values(self):
        for name, spec in NEW_CLIENT.items():
            with self.subTest(name=name):
                self.assertIn(name, schema.CLIENT_EVENTS, f'{name} is not a client event')
                self.assertEqual({k: tuple(v) for k, v in schema.CLIENT_EVENTS[name].items()}, spec)
                self.assertNotIn(name, schema.SERVER_EVENTS)

    def test_b2_every_allowed_value_passes_client_event(self):
        for name, spec in NEW_CLIENT.items():
            for properties in combinations(spec):
                with self.subTest(name=name, properties=properties):
                    event = ClientEvent.model_validate(record(name=name, properties=properties))
                    self.assertEqual(event.properties, properties)

    def test_b2_text_extra_keys_and_error_details_are_rejected(self):
        for name, spec in NEW_CLIENT.items():
            valid = {key: values[0] for key, values in spec.items()}
            ClientEvent.model_validate(record(name=name, properties=valid))  # precondition
            for extra in ('planet', 'sign', 'house', 'aspect', 'degree', 'question', 'message', 'stack',
                          'filename', 'url', 'status', 'route', 'chart_id'):
                with self.subTest(name=name, extra=extra), self.assertRaises(ValidationError):
                    ClientEvent.model_validate(record(name=name, properties=valid | {extra: 'CANARY'}))
            for key in spec:
                for text in TEXTS + ('group', 'pattern', 'planet', 'Sun', 'network'):
                    with self.subTest(name=name, key=key, text=text), self.assertRaises(ValidationError):
                        ClientEvent.model_validate(record(name=name, properties=valid | {key: text}))
            with self.subTest(name=name, empty=True), self.assertRaises(ValidationError):
                ClientEvent.model_validate(record(name=name, properties={}))

    def test_b2_browser_cannot_send_new_server_facts(self):
        for name, spec in NEW_SERVER.items():
            properties = {key: values[0] for key, values in spec.items()}
            with self.subTest(name=name), self.assertRaises(ValidationError):
                ClientEvent.model_validate(record(name=name, properties=properties))


if __name__ == '__main__':
    unittest.main()
