import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import anyio
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
import test_my_charts as fixtures
from test_synastry import facts
from test_ai_stream import ProviderStream, SUGGESTIONS
from database.queries import (User, NatalChart, ChartData, Relationship, GPTMessage, GPTConversation,
                              GPTUsage, RelationshipMessage, RelationshipConversation)
from modules import usage
from modules.ai_conversation import AIAnswer, MemoryUpdate, prepare_answer
from modules.ai_relationship_context import build_relationship_context
from modules.ai_stream import chat_events, streaming_response
from modules.interpretation import client, OPENAI_PARAMS
from modules.synastry import build_synastry_snapshot


def reply(answer='Pair answer', suggestions=SUGGESTIONS):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
        content=json.dumps({'answer': answer, 'follow_up_suggestions': suggestions})))],
        model='mock', usage=SimpleNamespace(prompt_tokens=100, completion_tokens=30, total_tokens=130))


class RelationshipAITests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown

    def pair(self, identifier=None):
        with self.sessions() as db:
            charts, saved = [], []
            for offset in (0, 30):
                info, data = facts(offset=offset)
                chart = NatalChart(user_id=1, birth_utc=info.birth_utc, **fixtures.PAYLOAD)
                db.add(chart); db.flush()
                stored = ChartData(chart_id=chart.id, bodies_for_circle=data.bodies_for_circle,
                                   aspects_for_circle=[{'from_body': '☉', 'to_body': '☽', 'aspect': '⚹'}],
                                   houses=data.houses, house_system=data.house_system)
                db.add(stored); charts.append(chart); saved.append(stored)
            relation = Relationship(id=identifier, user_id=1, chart_a_id=charts[0].id, chart_b_id=charts[1].id,
                person_a_label='Анна', person_b_label='Вячеслав', speaker_person='B',
                calculation=build_synastry_snapshot(charts[0], saved[0], charts[1], saved[1]),
                ruleset_version='synastry-major-8-v1')
            db.add(relation); db.flush()
            result = relation.id, charts[0].id
            db.commit()
            return result

    def ask(self, rid, question='Как нам лучше понимать друг друга?', sse=False, headers=None):
        return self.client.post(f'/relationships/{rid}/ask', json={'question': question},
            headers={**(self.owner if headers is None else headers), **({'Accept': 'text/event-stream'} if sse else {})})

    def reserve(self, rid):
        with self.sessions() as db:
            return usage.reserve(db, 1, relationship_id=rid)

    def state(self, reservation):
        with self.sessions() as db:
            row = db.get(GPTUsage, reservation)
            return row.status

    def test_json_one_call_one_usage_pair_persists_and_deletion_retains_quota(self):
        rid, cid = self.pair()
        with patch('modules.interpretation.client.chat.completions.create', return_value=reply()) as provider:
            response = self.ask(rid)
        self.assertEqual(response.status_code, 200, response.text)
        provider.assert_called_once()
        self.assertEqual(response.json()['relationship_id'], rid)
        history = self.client.get(f'/relationships/{rid}/messages', headers=self.owner).json()
        self.assertEqual([r['role'] for r in history], ['user', 'gpt'])
        self.assertTrue(all(r['relationship_id'] == rid and 'chart_id' not in r for r in history))
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).count(), 0)
            row = db.query(GPTUsage).one()
            self.assertEqual((row.chart_id, row.relationship_id, row.source_message_id), (None, rid, None))
            self.assertIsNotNone(row.source_relationship_message_id)
            self.assertEqual(row.total_tokens, 130)
            db.add(RelationshipConversation(relationship_id=rid, summary='pair memory', through_message_id=2))
            db.commit()
        self.assertEqual(self.client.delete(f'/relationships/{rid}', headers=self.owner).status_code, 204)
        with self.sessions() as db:
            self.assertEqual(db.query(RelationshipMessage).count(), 0)
            self.assertEqual(db.query(RelationshipConversation).count(), 0)
            self.assertEqual(db.query(GPTUsage).one().relationship_id, rid)
            self.assertIsNone(db.query(GPTUsage).one().source_relationship_message_id)
            self.assertEqual(usage.account_usage(db, 1)['gpt_messages_used'], 1)

    def test_nine_natal_plus_relationship_exhaust_free(self):
        rid, cid = self.pair()
        with self.sessions() as db:
            for i in range(9):
                usage.finalize(db, 1, usage.reserve(db, 1, cid), f'Natal {i}', 'Natal answer')
        with patch('modules.interpretation.client.chat.completions.create', return_value=reply()) as provider:
            self.assertEqual(self.ask(rid).status_code, 200)
            denied = self.ask(rid)
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(denied.json()['detail']['code'], 'GPT_LIMIT_REACHED')
            provider.assert_called_once()
        self.assertEqual(self.client.get('/account/usage', headers=self.owner).json()['gpt_messages_available'], 0)

    def test_expiration_release_and_no_double_finalization(self):
        rid, _ = self.pair(); reservation = self.reserve(rid)
        with self.sessions() as db:
            db.get(GPTUsage, reservation).expires_at = usage.utcnow() - timedelta(seconds=1); db.commit()
            with self.assertRaises(HTTPException): usage.finalize(db, 1, reservation, 'Late', 'Late answer')
            renewed = usage.reserve(db, 1, relationship_id=rid)
            usage.finalize(db, 1, renewed, 'New', 'Answer')
            with self.assertRaises(HTTPException): usage.finalize(db, 1, renewed, 'Repeat', 'Repeat answer')
            self.assertEqual(db.query(RelationshipMessage).count(), 2)
        self.assertEqual(self.state(reservation), 'released')

    def test_subject_xor_and_numeric_collision_reservations(self):
        rid, cid = self.pair()
        self.assertEqual(rid, cid)
        with self.sessions() as db:
            a = usage.reserve(db, 1, cid)
            b = usage.reserve(db, 1, relationship_id=rid)
            with self.assertRaises(HTTPException): usage.reserve(db, 1, relationship_id=rid)
            usage.finalize(db, 1, a, 'Natal', 'Natal answer')
            usage.finalize(db, 1, b, 'Pair', 'Pair answer')
            for fields in ({}, {'chart_id': cid, 'relationship_id': rid}):
                db.add(GPTUsage(user_id=1, status='reserved', plan='free', **fields))
                with self.assertRaises(IntegrityError): db.commit()
                db.rollback()
            self.assertEqual(db.query(GPTMessage).count(), 2)
            self.assertEqual(db.query(RelationshipMessage).count(), 2)

    def test_exact_42_collision_histories_memories_and_two_pairs_isolated(self):
        rid, _ = self.pair(42); second, _ = self.pair(43)
        with self.sessions() as db:
            info, data = facts(42)
            db.add(NatalChart(id=42, user_id=1, birth_utc=info.birth_utc, **fixtures.PAYLOAD)); db.flush()
            db.add(ChartData(chart_id=42, bodies_for_circle=data.bodies_for_circle, aspects_for_circle=[], houses=data.houses, house_system='Placidus'))
            db.add(GPTMessage(chart_id=42, role='user', content='NATAL_PRIVATE_HISTORY'))
            db.add(GPTConversation(chart_id=42, user_id=1, summary='NATAL_PRIVATE_MEMORY', through_message_id=0))
            for identifier, label in ((42, 'PAIR42'), (43, 'PAIR43')):
                db.add(RelationshipMessage(relationship_id=identifier, role='user', content=label+'_HISTORY'))
                db.add(RelationshipConversation(relationship_id=identifier, summary=label+'_MEMORY', through_message_id=0))
            db.commit()
            for chart_id, pair_id, included, excluded in ((42, None, 'NATAL_PRIVATE', ('PAIR42', 'PAIR43')),
                        (None, 42, 'PAIR42', ('NATAL_PRIVATE', 'PAIR43')), (None, 43, 'PAIR43', ('NATAL_PRIVATE', 'PAIR42'))):
                messages, _, _ = prepare_answer(db, 1, chart_id, 'Question', client, OPENAI_PARAMS, relationship_id=pair_id)
                contents = str(messages)
                self.assertIn(included+'_HISTORY', contents); self.assertIn(included+'_MEMORY', contents)
                for name in excluded: self.assertNotIn(name, contents)

    def test_context_projection_availability_and_snapshot_not_mutated(self):
        rid, _ = self.pair()
        with self.sessions() as db:
            relation = db.get(Relationship, rid)
            stored = db.query(ChartData).filter_by(chart_id=relation.chart_b_id).one()
            stored.houses = None
            bodies = dict(stored.bodies_for_circle); bodies.pop('MC'); stored.bodies_for_circle = bodies
            db.flush()
            a, b = db.get(NatalChart, relation.chart_a_id), db.get(NatalChart, relation.chart_b_id)
            relation.calculation = build_synastry_snapshot(a, db.query(ChartData).filter_by(chart_id=a.id).one(), b, stored)
            db.commit()
            original = json.dumps(relation.calculation, sort_keys=True)
            value = build_relationship_context(db, 1, rid); data = json.loads(value)
            self.assertEqual(value, build_relationship_context(db, 1, rid))
            self.assertEqual(data['speaker_person'], 'B')
            self.assertEqual(data['people']['A']['label'], 'Анна')
            self.assertFalse(data['angle_availability']['B']['MC'])
            self.assertFalse(data['house_overlays'][0]['available'])
            self.assertTrue(data['interchart_aspects'])
            self.assertTrue(all(r['participant_a'] != r['participant_b'] for r in data['interchart_aspects']))
            self.assertNotIn('compatibility_percentage', data)
            self.assertLessEqual(len(value.encode('utf-8')), 32000)
            self.assertEqual(json.dumps(relation.calculation, sort_keys=True), original)

    def test_context_budget_deterministic_omissions_and_minimum_guard(self):
        rid, _ = self.pair()
        with self.sessions() as db:
            full = build_relationship_context(db, 1, rid)
            with patch('modules.ai_relationship_context.RELATIONSHIP_CONTEXT_BUDGET', len(full.encode('utf-8')) - 500):
                shortened = build_relationship_context(db, 1, rid)
                self.assertEqual(shortened, build_relationship_context(db, 1, rid))
                self.assertGreater(json.loads(shortened)['omitted']['interchart_aspects'], 0)
                original_rows = json.loads(full)['interchart_aspects']
                self.assertEqual(json.loads(shortened)['interchart_aspects'], original_rows[:len(json.loads(shortened)['interchart_aspects'])])
            with patch('modules.ai_relationship_context.RELATIONSHIP_CONTEXT_BUDGET', 10):
                with self.assertRaises(HTTPException) as error: build_relationship_context(db, 1, rid)
                self.assertEqual(error.exception.status_code, 422)

    def test_stale_context_denied_without_provider_or_successful_charge(self):
        rid, cid = self.pair()
        with self.sessions() as db:
            db.get(NatalChart, cid).timezone = 'Europe/Paris'; db.commit()
        with patch('modules.interpretation.client.chat.completions.create') as provider:
            self.assertEqual(self.ask(rid).status_code, 409)
            provider.assert_not_called()
        with self.sessions() as db: self.assertEqual(db.query(GPTUsage).one().status, 'released')

    def test_owner_only_messages_ask_and_input_validation(self):
        rid, _ = self.pair()
        with patch('modules.interpretation.client.chat.completions.create') as provider:
            for headers, code in (({}, 401), (self.guest, 401), (self.other, 404)):
                self.assertEqual(self.ask(rid, headers=headers).status_code, code)
                self.assertEqual(self.client.get(f'/relationships/{rid}/messages', headers=headers).status_code, code)
            for question in (' ', 'x' * 4001): self.assertEqual(self.ask(rid, question=question).status_code, 422)
            provider.assert_not_called()

    def test_summary_atomic_and_provider_transactions_closed(self):
        rid, _ = self.pair()
        with self.sessions() as db:
            db.query(User).filter_by(id=1).update({'plan': 'premium', 'current_period_start': usage.utcnow()-timedelta(days=1),
                                                 'current_period_end': usage.utcnow()+timedelta(days=28)})
            for i in range(12):
                db.add_all([RelationshipMessage(relationship_id=rid, role='user', content=f'Pair question {i}'),
                            RelationshipMessage(relationship_id=rid, role='gpt', content=f'Pair answer {i}')])
            db.commit()
            reservation = usage.reserve(db, 1, relationship_id=rid)
            summaries = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='PAIR SUMMARY'))])
            def respond(**kwargs):
                self.assertFalse(db.in_transaction())
                return reply() if 'response_format' in kwargs else summaries
            from modules.ai_conversation import generate_answer
            with patch.object(client.chat.completions, 'create', side_effect=respond) as provider:
                answer = generate_answer(db, 1, None, 'Next', client, OPENAI_PARAMS, relationship_id=rid)
            self.assertEqual(provider.call_count, 2)
            self.assertIn('ONE Relationship', provider.call_args_list[0].kwargs['messages'][0]['content'])
            self.assertIsNone(db.get(RelationshipConversation, rid))
            usage.finalize(db, 1, reservation, 'Next', answer)
            self.assertEqual(db.get(RelationshipConversation, rid).summary, 'PAIR SUMMARY')
            self.assertEqual(db.query(GPTUsage).count(), 1)
            self.assertEqual(db.query(RelationshipMessage).count(), 26)

    def test_summary_and_answer_failure_leave_memory_unchanged(self):
        rid, _ = self.pair()
        with self.sessions() as db:
            db.add(RelationshipConversation(relationship_id=rid, summary='KEEP', through_message_id=0))
            db.commit()
        for response in (TimeoutError('mock'), reply(answer='')):
            with patch('modules.interpretation.client.chat.completions.create', side_effect=response if isinstance(response, Exception) else [response]):
                self.assertGreaterEqual(self.ask(rid).status_code, 400)
        with self.sessions() as db:
            self.assertEqual(db.get(RelationshipConversation, rid).summary, 'KEEP')
            self.assertEqual(db.query(RelationshipMessage).count(), 0)
            self.assertEqual(db.query(GPTUsage).filter_by(status='succeeded').count(), 0)

    def test_sse_contract_done_after_persistence_suggestions_final(self):
        rid, _ = self.pair(); reservation = self.reserve(rid); provider = ProviderStream()
        events = []
        with patch('modules.interpretation.client.chat.completions.create', return_value=provider) as api:
            for part in chat_events(self.engine, 1, None, 'Pair', reservation, client, OPENAI_PARAMS, relationship_id=rid):
                events.append(part)
                if part['type'] == 'delta': self.assertEqual(self.state(reservation), 'reserved')
                if part['type'] == 'done':
                    self.assertEqual(self.state(reservation), 'succeeded')
                    with self.sessions() as db: self.assertEqual(db.query(RelationshipMessage).count(), 2)
            api.assert_called_once()
        self.assertEqual(events[0]['type'], 'start'); self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-1]['follow_up_suggestions'], SUGGESTIONS)
        self.assertTrue(all('follow_up_suggestions' not in e for e in events[:-1]))
        with patch('modules.interpretation.client.chat.completions.create', return_value=ProviderStream()):
            response = self.ask(rid, sse=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/event-stream', response.headers['content-type'])

    def test_sse_provider_error_disconnect_and_deleted_subject_release(self):
        for mode in ('error', 'disconnect', 'delete', 'expired'):
            with self.subTest(mode=mode):
                rid, _ = self.pair(); reservation = self.reserve(rid); provider = ProviderStream(fail=mode == 'error')
                with patch('modules.interpretation.client.chat.completions.create', return_value=provider):
                    parts = chat_events(self.engine, 1, None, 'Pair', reservation, client, OPENAI_PARAMS, relationship_id=rid)
                    self.assertEqual(next(parts)['type'], 'start'); self.assertEqual(next(parts)['type'], 'delta')
                    if mode == 'disconnect': parts.close()
                    else:
                        if mode == 'delete': self.client.delete(f'/relationships/{rid}', headers=self.owner)
                        if mode == 'expired':
                            with self.sessions() as db:
                                db.get(GPTUsage, reservation).expires_at = usage.utcnow()-timedelta(seconds=1); db.commit()
                        rest = list(parts)
                        self.assertEqual(rest[-1]['type'], 'error')
                        self.assertFalse(any(p['type'] == 'done' for p in rest))
                self.assertEqual(self.state(reservation), 'released')
                self.assertTrue(provider.response.is_closed)
                with self.sessions() as db: self.assertEqual(db.query(RelationshipMessage).count(), 0)

    def test_sse_asgi_disconnect_closes_provider(self):
        rid, _ = self.pair(); reservation = self.reserve(rid); provider = ProviderStream()
        async def run():
            disconnected = anyio.Event()
            async def receive():
                await disconnected.wait(); return {'type': 'http.disconnect'}
            async def send(message):
                if b'"type": "delta"' in message.get('body', b''):
                    disconnected.set(); await anyio.sleep(0)
            response = streaming_response(self.engine, 1, None, 'Pair', reservation, client, OPENAI_PARAMS, relationship_id=rid)
            await response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send)
        with patch('modules.interpretation.client.chat.completions.create', return_value=provider): anyio.run(run)
        self.assertEqual(self.state(reservation), 'released')
        self.assertTrue(provider.response.is_closed)

    def test_persistence_failure_rolls_back_candidate_summary_and_usage(self):
        rid, _ = self.pair(); reservation = self.reserve(rid)
        def fail(conn, cursor, statement, *args):
            if statement.startswith('INSERT INTO relationship_conversations'): raise RuntimeError('fake storage failure')
        answer = AIAnswer('Answer', metadata={}, memory_update=MemoryUpdate(0, 1, 'candidate'))
        event.listen(self.engine, 'before_cursor_execute', fail)
        try:
            with self.sessions() as db:
                with self.assertRaises(RuntimeError): usage.finalize(db, 1, reservation, 'Pair', answer)
                usage.release(db, 1, reservation)
        finally: event.remove(self.engine, 'before_cursor_execute', fail)
        with self.sessions() as db:
            self.assertEqual(db.query(RelationshipMessage).count(), 0)
            self.assertIsNone(db.get(RelationshipConversation, rid))
        self.assertEqual(self.state(reservation), 'released')
