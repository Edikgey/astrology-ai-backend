import unittest
from unittest.mock import patch
import test_my_charts as fixtures
from test_synastry import facts
from database.queries import NatalChart, ChartData, Relationship, GPTMessage, GPTUsage
from sqlalchemy.exc import IntegrityError


class RelationshipTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart

    def pair(self):
        ids = [self.chart(), self.chart()]
        with self.sessions() as db:
            for i, identifier in enumerate(ids):
                chart, stored = facts(identifier, i * 30)
                db.query(NatalChart).filter_by(id=identifier).update({"birth_utc": chart.birth_utc})
                db.query(ChartData).filter_by(chart_id=identifier).update({
                    "bodies_for_circle": stored.bodies_for_circle, "houses": stored.houses,
                    "house_system": stored.house_system})
            db.commit()
        return {"chart_a_id": ids[0], "chart_b_id": ids[1], "person_a_label": " Анна ",
                "person_b_label": "Вячеслав", "speaker_person": "B"}

    def create(self, data):
        return self.client.post('/relationships', headers=self.owner, json=data)

    def test_create_snapshot_and_duplicate_reverse_preserve_original_orientation(self):
        data = self.pair()
        with patch('api.endpoints.Ephemeris', side_effect=AssertionError('No recalculation')):
            first = self.create(data)
            self.assertEqual(first.status_code, 201, first.text)
            reverse = {**data, "chart_a_id": data['chart_b_id'], "chart_b_id": data['chart_a_id'],
                       "person_a_label": "Changed", "speaker_person": "A"}
            second = self.create(reverse)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(first.json()['person_a_label'], 'Анна')
        with self.sessions() as db:
            self.assertEqual(db.query(Relationship).count(), 1)
            self.assertEqual(db.query(GPTUsage).count(), 0)
            self.assertEqual(db.query(GPTMessage).count(), 4)

    def test_auth_ownership_same_chart_and_metadata_validation(self):
        data = self.pair()
        for headers in ({}, self.guest):
            self.assertEqual(self.client.post('/relationships', headers=headers, json=data).status_code, 401)
            self.assertEqual(self.client.get('/relationships', headers=headers).status_code, 401)
        for identifier in (self.chart(user_id=2), self.chart(user_id=None, session_token=self.guest_token), 99999):
            self.assertEqual(self.create({**data, "chart_b_id": identifier}).status_code, 404)
        for changes in ({"chart_b_id": data['chart_a_id']}, {"person_a_label": "  "},
                        {"person_b_label": "x" * 101}, {"person_a_label": "a\nb"},
                        {"speaker_person": "C"}, {"user_id": 2}, {"chart_a_id": True}):
            self.assertEqual(self.create({**data, **changes}).status_code, 422)

    def test_unverified_and_ambiguous_saved_data_rejected(self):
        data = self.pair()
        with self.sessions() as db:
            db.query(NatalChart).filter_by(id=data['chart_a_id']).update({'birth_utc': None})
            db.commit()
        result = self.create(data)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['detail']['reason'], 'legacy_unverified')
        with self.sessions() as db:
            db.add(ChartData(chart_id=data['chart_b_id'], bodies_for_circle={}, aspects_for_circle=[]))
            db.commit()
        self.assertEqual(self.create(data).json()['detail']['code'], 'SYNASTRY_DATA_UNAVAILABLE')

    def test_owner_scoped_get_list_filter_delete_and_chart_preservation(self):
        data = self.pair()
        identifier = self.create(data).json()['id']
        self.assertEqual(self.client.get('/relationships', headers=self.other).json()['count'], 0)
        for method in ('get', 'delete'):
            self.assertEqual(getattr(self.client, method)(f'/relationships/{identifier}', headers=self.other).status_code, 404)
        self.assertEqual(self.client.get(f'/relationships/{identifier}', headers=self.owner).status_code, 200)
        listed = self.client.get(f"/relationships?chart_id={data['chart_b_id']}", headers=self.owner).json()
        self.assertEqual(listed['count'], 1)
        self.assertNotIn('calculation', listed['relationships'][0])
        self.assertEqual(self.client.get('/relationships?chart_id=99999', headers=self.owner).json()['count'], 0)
        self.assertEqual(self.client.delete(f'/relationships/{identifier}', headers=self.owner).status_code, 204)
        with self.sessions() as db:
            self.assertEqual(db.query(NatalChart).count(), 2)
            self.assertEqual(db.query(ChartData).count(), 2)
            self.assertEqual(db.query(GPTMessage).count(), 4)
            self.assertEqual(db.query(GPTUsage).count(), 0)

    def test_natal_delete_blocked_before_ledger_or_history_mutation(self):
        data = self.pair()
        identifier = self.create(data).json()['id']
        for cid in (data['chart_a_id'], data['chart_b_id']):
            blocked = self.client.delete(f'/natal-chart/{cid}', headers=self.owner)
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertEqual(blocked.json()['detail']['code'], 'RELATIONSHIP_DEPENDENCIES_EXIST')
        with self.sessions() as db:
            self.assertEqual(db.query(GPTUsage).count(), 0)
            self.assertEqual(db.query(GPTMessage).count(), 4)
        self.client.delete(f'/relationships/{identifier}', headers=self.owner)
        self.assertEqual(self.client.delete(f"/natal-chart/{data['chart_a_id']}", headers=self.owner).status_code, 204)

    def test_database_guards_without_application_checks(self):
        data = self.pair()
        self.create(data)
        with self.sessions() as db:
            for a, b in ((data['chart_b_id'], data['chart_a_id']), (data['chart_a_id'], data['chart_a_id']), (99999, data['chart_b_id'])):
                db.add(Relationship(user_id=1, chart_a_id=a, chart_b_id=b, person_a_label='A', person_b_label='B',
                                    calculation={}, ruleset_version='test'))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()
            with self.assertRaises(IntegrityError):
                db.query(GPTMessage).filter_by(chart_id=data['chart_a_id']).delete(synchronize_session=False)
                db.query(fixtures.ChartInterpretationData).filter_by(chart_id=data['chart_a_id']).delete(synchronize_session=False)
                db.query(ChartData).filter_by(chart_id=data['chart_a_id']).delete(synchronize_session=False)
                db.query(NatalChart).filter_by(id=data['chart_a_id']).delete(synchronize_session=False)
                db.commit()
            db.rollback()
