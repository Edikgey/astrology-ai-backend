"""Privacy boundary tests; no production data, network, API keys or DB required."""
import json
import unittest
from uuid import uuid4
from pydantic import ValidationError
from modules.product_analytics_schema import ClientBatch, ClientEvent, CLIENT_EVENTS, SERVER_EVENTS, checked_properties


def record(**overrides):
    return dict(event_id=str(uuid4()), session_id=str(uuid4()), name='page_viewed',
                route_key='home', viewport_class='unknown', campaign_source='unspecified', properties={}) | overrides


class ProductAnalyticsSchemaTests(unittest.TestCase):
    def test_valid_envelope_roundtrip_contains_no_synthetic_private_context(self):
        item = record(route_key='relationship_view')
        batch = ClientBatch.model_validate(dict(policy_version='posthog-v1', visitor_id=str(uuid4()), events=[item]))
        serialized = batch.model_dump_json()
        self.assertEqual(json.loads(serialized)['events'][0], item)
        for forbidden in ('birth', 'email', 'token', 'customer', 'referrer', 'url'):
            self.assertNotIn(forbidden, serialized)

    def test_every_client_schema_rejects_extra_sensitive_properties(self):
        for name, schema in CLIENT_EVENTS.items():
            with self.subTest(name=name):
                properties = {key: values[0] for key, values in schema.items()}
                ClientEvent.model_validate(record(name=name, properties=properties))
                for key in ('question', 'answer', 'email', 'birth_date', 'token', '$current_url',
                            '$referrer', 'user_id', 'customer_id', 'client_secret', '__proto__'):
                    with self.assertRaises(ValidationError):
                        ClientEvent.model_validate(record(name=name, properties=properties | {key: 'CANARY_PRIVATE'}))

    def test_client_cannot_forge_paid_or_other_server_conversions(self):
        for name in SERVER_EVENTS:
            with self.subTest(name=name), self.assertRaises(ValidationError):
                ClientEvent.model_validate(record(name=name))

    def test_server_schemas_also_reject_private_payload_and_unknown_sources(self):
        for name, schema in SERVER_EVENTS.items():
            properties = {key: values[0] for key, values in schema.items()}
            self.assertEqual(checked_properties(name, properties, source='server'), properties)
            with self.assertRaises(ValueError):
                checked_properties(name, properties | {'invoice': 'CANARY'}, source='server')
        with self.assertRaises(ValueError):
            checked_properties('page_viewed', {}, source='arbitrary')

    def test_raw_urls_fragments_and_ids_are_not_page_categories(self):
        for route in ('/my-charts', 'https://mylunariaai.com/', 'home?token=CANARY',
                      'chart_view/91234', 'checkout', 'reset-password', 'home#CANARY'):
            with self.subTest(route=route), self.assertRaises(ValidationError):
                ClientEvent.model_validate(record(route_key=route))

    def test_envelope_disallows_identity_override_arbitrary_metadata_and_coercion(self):
        for changes in ({'user_id': 1}, {'distinct_id': 'CANARY'}, {'timestamp': 'CANARY'},
                        {'name': '$snapshot'}, {'name': 123}, {'properties': []},
                        {'event_id': 'CANARY_JWT'}, {'session_id': 'cs_live_CANARY'},
                        {'name': 'payment_provider_selected', 'properties': {'provider': 'paddle'}},
                        {'name': 'ai_answer_failed', 'properties': {'subject': 'natal', 'reason': 'CANARY_ERROR'}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ClientEvent.model_validate(record(**changes))

    def test_batch_requires_current_policy_bounded_size_and_unique_event_ids(self):
        batch = dict(policy_version='posthog-v1', visitor_id=str(uuid4()), events=[record()])
        for changes in ({'policy_version': 'clarity-v1'}, {'events': []},
                        {'events': [record() for _ in range(21)]},
                        {'events': batch['events'] * 2}, {'user_id': 1},
                        {'referrer': 'CANARY_REFERRER'}, {'visitor_id': 'CANARY_SESSION_TOKEN'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ClientBatch.model_validate(batch | changes)
        self.assertEqual(len(ClientBatch.model_validate(batch | {'events': [record() for _ in range(20)]}).events), 20)


if __name__ == '__main__':
    unittest.main()
