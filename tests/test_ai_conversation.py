"""AI integration tests: real context/accounting code, fake provider, isolated DB."""
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import event
import test_my_charts as fixtures
from database.queries import ChartData, GPTConversation, GPTMessage, GPTUsage
from modules import usage, ai_conversation as ai
from modules.ai_chart_context import build_chart_context
from modules.astrology_prompt import ASTROLOGY_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT


def reply(text='Answer', tokens=True, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)],
                           model='gpt-4o-mini-test', usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45, total_tokens=168) if tokens else None)


class AIConversationTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart

    def test_structured_answer_suggestions_use_one_call_and_one_quota_unit(self):
        chart_id = self.chart()
        suggestions = ['Как проявляется моё лидерство?', 'Что мешает мне развиваться?', 'Как это связано с деньгами?']
        before = self.client.get('/account/usage', headers=self.owner).json()['gpt_messages_used']
        payload = {'answer': 'В работе важна самостоятельность.', 'follow_up_suggestions': suggestions}
        with patch('modules.interpretation.client.chat.completions.create', return_value=reply(json.dumps(payload))) as api:
            result = self.ask(chart_id, 'Что мне важно в карьере?')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['response'], payload['answer'])
            self.assertEqual(result.json()['follow_up_suggestions'], suggestions)
            api.assert_called_once()
            self.assertEqual(api.call_args.kwargs['response_format'], ai.ANSWER_FORMAT)
            self.assertTrue(api.call_args.kwargs['response_format']['json_schema']['strict'])
        self.assertEqual(self.client.get('/account/usage', headers=self.owner).json()['gpt_messages_used'], before + 1)
        history = self.client.get(f'/gpt-messages?chart_id={chart_id}', headers=self.owner).json()
        self.assertEqual(history[-1]['content'], payload['answer'])
        self.assertNotIn('follow_up_suggestions', history[-1])

    def test_missing_or_malformed_suggestions_preserve_answer_without_retry(self):
        chart_id = self.chart()
        cases = [{}, {'follow_up_suggestions': None}, {'follow_up_suggestions': 'broken'},
                 {'follow_up_suggestions': ['One?', 42, 'Three?']},
                 {'follow_up_suggestions': ['Same?', 'same?', 'Next?']},
                 {'follow_up_suggestions': ['Only one?']}]
        before = self.client.get('/account/usage', headers=self.owner).json()['gpt_messages_used']
        for extra in cases:
            with self.subTest(extra=extra), patch('modules.interpretation.client.chat.completions.create',
                    return_value=reply(json.dumps({'answer': 'Valid answer', **extra}))) as api:
                result = self.ask(chart_id)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()['response'], 'Valid answer')
                self.assertEqual(result.json()['follow_up_suggestions'], [])
                api.assert_called_once()
        self.assertEqual(self.client.get('/account/usage', headers=self.owner).json()['gpt_messages_used'], before + len(cases))

    def test_invalid_structured_answer_releases_reservation(self):
        chart_id = self.chart()
        before = self.client.get('/account/usage', headers=self.owner).json()['gpt_messages_used']
        for content in ('{"follow_up_suggestions": []}', '{"answer": ""}', '{"answer":'):
            with patch('modules.interpretation.client.chat.completions.create', return_value=reply(content)) as api:
                self.assertEqual(self.ask(chart_id).status_code, 502)
                api.assert_called_once()
        state = self.client.get('/account/usage', headers=self.owner).json()
        self.assertEqual(state['gpt_messages_used'], before)
        self.assertEqual(state['gpt_messages_reserved'], 0)

    def test_summary_remains_plain_and_receives_only_answer_history(self):
        chart_id = self.chart()
        self.long_chat(chart_id)
        payload = {'answer': 'Continue the topic.', 'follow_up_suggestions': ['What helps me grow?', 'How can I lead?', 'How does this affect relationships?']}
        with patch('modules.interpretation.client.chat.completions.create',
                   side_effect=[reply('Existing memory'), reply(json.dumps(payload))]) as api:
            result = self.ask(chart_id)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(api.call_count, 2)  # Existing memory call + one answer, never a suggestions call.
            self.assertNotIn('response_format', api.call_args_list[0].kwargs)
            self.assertIn('Existing memory', str(api.call_args_list[1].kwargs['messages']))
        self.assertEqual(self.snapshot(chart_id)[0][-1], ('gpt', payload['answer']))

    def ask(self, chart_id, question='А как это проявляется в отношениях?', headers=None):
        return self.client.post('/ask-gpt', headers=headers or self.owner, json={'chart_id':chart_id, 'question':question})

    def long_chat(self, chart_id, pairs=12, size=0):
        with self.sessions() as db:
            now = datetime.utcnow()
            usage.set_plan(db, 1, 'premium', now-timedelta(days=1), now+timedelta(days=29))
            for i in range(pairs):
                db.add(GPTMessage(chart_id=chart_id, role='user', content=f'Question-{i:02d} '+('x'*size)))
                db.add(GPTMessage(chart_id=chart_id, role='gpt', content=f'Answer-{i:02d}'))
            db.commit()

    def snapshot(self, chart_id):
        with self.sessions() as db:
            memory = db.get(GPTConversation, chart_id)
            return ([(m.role, m.content) for m in db.query(GPTMessage).filter_by(chart_id=chart_id).order_by(GPTMessage.id)],
                    (memory.summary, memory.through_message_id) if memory else None)

    def test_second_and_third_question_receive_previous_verbatim_dialogue(self):
        chart_id=self.chart()
        with patch('modules.interpretation.client.chat.completions.create', side_effect=[reply('Сильные стороны — устойчивость.'),reply('В отношениях это надёжность.'),reply()]) as api:
            questions=['Какие у меня сильные стороны?','А как это проявляется в отношениях?','А что из этого самое проблемное?']
            for question in questions:
                self.assertEqual(self.ask(chart_id,question).status_code,200)
            second=api.call_args_list[1].kwargs['messages']
            self.assertEqual(second[-3:], [{'role':'user','content':questions[0]}, {'role':'assistant','content':'Сильные стороны — устойчивость.'}, {'role':'user','content':questions[1]}])
            third=api.call_args.kwargs['messages']
            self.assertEqual(third[-2]['content'],'В отношениях это надёжность.')
            self.assertEqual(third[-1],{'role':'user','content':questions[2]})
            self.assertEqual(third[0],{'role':'system','content':ASTROLOGY_SYSTEM_PROMPT})

    def test_summary_is_atomic_persistent_and_old_transcript_not_resent(self):
        chart_id=self.chart();self.long_chat(chart_id)
        before=self.snapshot(chart_id)[0]
        with patch('modules.interpretation.client.chat.completions.create', side_effect=[reply('Discussed work; user prefers practical advice.'),reply('Personal answer')]) as api:
            self.assertEqual(self.ask(chart_id).status_code,200)
            self.assertEqual(api.call_count,2)
            summary_input=api.call_args_list[0].kwargs['messages']
            self.assertEqual(summary_input[0]['content'],SUMMARY_SYSTEM_PROMPT)
            self.assertIn('Question-00',summary_input[1]['content'])
            messages=api.call_args.kwargs['messages']
            self.assertIn('Discussed work',messages[2]['content'])
            self.assertNotIn('Question-00',str(messages))
            self.assertEqual(messages[-3]['content'],'Question-11 ')
        after,memory=self.snapshot(chart_id)
        self.assertEqual(after[:-2],before)
        self.assertIsNotNone(memory)
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply()) as api:
            self.assertEqual(self.ask(chart_id,'И дальше?').status_code,200)
            api.assert_called_once()
            self.assertIn('Discussed work',str(api.call_args.kwargs['messages']))
            self.assertNotIn('Question-00',str(api.call_args.kwargs['messages']))

    def test_chart_and_owner_memories_are_independent(self):
        a=self.chart();b=self.chart();other=self.chart(user_id=2)
        with self.sessions() as db:
            for cid,owner,note in [(a,1,'PRIVATE_A'),(b,1,'PRIVATE_B'),(other,2,'PRIVATE_OTHER')]:
                db.add(GPTConversation(chart_id=cid,user_id=owner,summary=note,through_message_id=0))
            db.commit()
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply()) as api:
            for cid,note in [(a,'PRIVATE_A'),(b,'PRIVATE_B')]:
                self.assertEqual(self.ask(cid).status_code,200)
                context=str(api.call_args.kwargs['messages'])
                self.assertIn(note,context)
                for excluded in {'PRIVATE_A','PRIVATE_B','PRIVATE_OTHER'}-{note}:
                    self.assertNotIn(excluded,context)
            count=api.call_count
            self.assertEqual(self.ask(other).status_code,403)
            self.assertEqual(self.client.get(f'/gpt-messages?chart_id={other}',headers=self.owner).status_code,403)
            self.assertEqual(api.call_count,count)
        with self.sessions() as db:
            with self.assertRaises(Exception):build_chart_context(db,1,other)

    def test_summary_or_answer_failure_preserves_memory_messages_and_quota(self):
        chart_id=self.chart();self.long_chat(chart_id)
        with self.sessions() as db:
            db.add(GPTConversation(chart_id=chart_id,user_id=1,summary='Original memory',through_message_id=0));db.commit()
        before=self.snapshot(chart_id)
        for responses in ([TimeoutError('mock')], [reply('Candidate memory'),TimeoutError('mock')],
                          [reply('Candidate memory'),reply('')], [reply('incomplete',finish='length')]):
            with self.subTest(responses=len(responses)):
                state=self.client.get('/account/usage',headers=self.owner).json()
                with patch('modules.interpretation.client.chat.completions.create',side_effect=responses):
                    self.assertGreaterEqual(self.ask(chart_id).status_code,500)
                self.assertEqual(self.snapshot(chart_id),before)
                new=self.client.get('/account/usage',headers=self.owner).json()
                self.assertEqual(new['gpt_messages_used'],state['gpt_messages_used'])
                self.assertEqual(new['gpt_messages_reserved'],0)

    def test_persistence_failure_rolls_back_candidate_summary_and_token_usage(self):
        chart_id=self.chart();self.long_chat(chart_id)
        before=self.snapshot(chart_id)
        def fail(conn,cursor,statement,*args):
            if statement.startswith('INSERT INTO gpt_conversations'):raise RuntimeError('mock DB failure')
        event.listen(self.engine,'before_cursor_execute',fail)
        try:
            with patch('modules.interpretation.client.chat.completions.create',side_effect=[reply('Memory'),reply()]):
                self.assertEqual(self.ask(chart_id).status_code,500)
        finally:event.remove(self.engine,'before_cursor_execute',fail)
        self.assertEqual(self.snapshot(chart_id),before)
        with self.sessions() as db:
            row=db.query(GPTUsage).filter_by(status='released').one()
            self.assertIsNone(row.total_tokens)

    def test_success_records_provider_tokens_and_finalizes_once(self):
        chart_id=self.chart()
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply()):
            self.assertEqual(self.ask(chart_id).status_code,200)
        with self.sessions() as db:
            row=db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one()
            self.assertEqual((row.input_tokens,row.output_tokens,row.total_tokens,row.model),(123,45,168,'gpt-4o-mini-test'))
            with self.assertRaises(Exception):usage.finalize(db,1,row.id,'duplicate','duplicate')
            self.assertEqual(db.query(GPTMessage).filter_by(chart_id=chart_id).count(),4)
            self.assertEqual(db.query(GPTUsage).filter_by(status='succeeded',user_id=1).count(),2)

    def test_summary_call_cost_is_separate_from_one_product_message(self):
        chart_id=self.chart();self.long_chat(chart_id)
        before=self.client.get('/account/usage',headers=self.owner).json()['gpt_messages_used']
        with patch('modules.interpretation.client.chat.completions.create',side_effect=[reply('Memory'),reply()]):
            self.assertEqual(self.ask(chart_id).status_code,200)
        self.assertEqual(self.client.get('/account/usage',headers=self.owner).json()['gpt_messages_used'],before+1)
        with self.sessions() as db:
            row=db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one()
            self.assertEqual(row.total_tokens,168)
            self.assertEqual(row.summary_usage[0]['total_tokens'],168)

    def test_missing_provider_usage_is_null_not_fabricated(self):
        chart_id=self.chart()
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply(tokens=False)):
            self.assertEqual(self.ask(chart_id).status_code,200)
        with self.sessions() as db:
            row=db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one()
            self.assertIsNone(row.input_tokens);self.assertIsNone(row.output_tokens);self.assertIsNone(row.total_tokens)

    def test_chart_context_has_real_placements_houses_aspects_and_deduplicated_patterns(self):
        chart_id=self.chart()
        with self.sessions() as db:
            data=db.query(ChartData).filter_by(chart_id=chart_id).one()
            data.bodies_for_circle={**fixtures.BODY,'☽':{'label':'Луна','sign':'Телец','degree':31.0,'house':2,'retrograde':False},
                                    'AS':{'label':'Асцендент','sign':'Овен','degree':12.0,'house':1,'retrograde':False}}
            data.aspects_for_circle=[{'from_body':'☉','to_body':'☽','aspect':'□'},{'from_body':'☽','to_body':'☉','aspect':'□'}]
            data.patterns_data=[{'type':'Test calculated pattern','bodies':[{'symbol':'☉'},{'symbol':'☽'}]}]
            data.houses=[{'symbol':str(i),'degree':float(i*30 % 360)} for i in range(1,13)]
            data.house_system='Placidus'
            db.commit()
            result=json.loads(build_chart_context(db,1,chart_id))
            self.assertEqual(result['core_bodies'],['☉','☽','AS'])
            self.assertIn(['☉','Солнце','Овен',10.0,1,False],result['placements'])
            self.assertEqual(result['birth']['date'],[2000,1,2])
            self.assertEqual(len(result['houses']['cusp_longitudes_deg']),12)
            self.assertEqual(len(result['aspects']),1)
            self.assertEqual(len(result['patterns']),1)
            self.assertNotIn('cached chart',str(result))

    def test_missing_calculated_chart_fails_without_provider_or_charge(self):
        chart_id=self.chart(related=False)
        with patch('modules.interpretation.client.chat.completions.create') as api:
            self.assertEqual(self.ask(chart_id).status_code,409);api.assert_not_called()
        self.assertEqual(self.client.get('/account/usage',headers=self.owner).json()['gpt_messages_used'],0)

    def test_budget_reduces_history_and_keeps_new_question_and_chart_intact(self):
        chart_id=self.chart();self.long_chat(chart_id,pairs=4,size=3500)
        question='Что из этого самое проблемное?'
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply('Compact memory or answer')) as api:
            self.assertEqual(self.ask(chart_id,question).status_code,200)
            messages=api.call_args.kwargs['messages']
            self.assertEqual(messages[-1],{'role':'user','content':question})
            self.assertIn('"placements"',messages[1]['content'])
            self.assertLessEqual(sum(map(ai.message_size,messages)),ai.MAX_INPUT_BUDGET)
            self.assertNotIn('Question-00',str(messages))
            self.assertIn('Question-03',str(messages))

    def test_backlog_compaction_is_bounded_and_never_claims_full_memory(self):
        chart_id=self.chart();self.long_chat(chart_id,pairs=80,size=1000)
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply('Bounded notes')) as api:
            self.assertEqual(self.ask(chart_id).status_code,200)
            self.assertLessEqual(api.call_count,3)
            self.assertIn('"older_history_pending":true',api.call_args.kwargs['messages'][2]['content'])
        with self.sessions() as db:
            memory=db.get(GPTConversation,chart_id)
            self.assertLess(memory.through_message_id,db.query(GPTMessage.id).order_by(GPTMessage.id.desc()).first()[0])

    def test_read_transactions_are_closed_during_summary_and_answer_calls(self):
        chart_id=self.chart();self.long_chat(chart_id)
        sessions=[]
        def test_db():
            with self.sessions() as db:sessions.append(db);yield db
        fixtures.app.dependency_overrides[fixtures.get_db]=test_db
        def provider(**kwargs):
            self.assertFalse(any(db.in_transaction() for db in sessions));return reply('Notes or answer')
        with patch('modules.interpretation.client.chat.completions.create',side_effect=provider) as api:
            self.assertEqual(self.ask(chart_id).status_code,200);self.assertEqual(api.call_count,2)

    def test_history_after_refresh_retains_full_transcript_in_stable_order(self):
        chart_id=self.chart();self.long_chat(chart_id)
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply('Answer saved once')):
            self.assertEqual(self.ask(chart_id,'Newest question').status_code,200)
        rows=self.client.get(f'/gpt-messages?chart_id={chart_id}',headers=self.owner).json()
        self.assertEqual(len(rows),28)
        self.assertEqual([r['content'] for r in rows][-2:],['Newest question','Answer saved once'])
        self.assertEqual([r['id'] for r in rows],sorted(r['id'] for r in rows))

    def test_deleting_chart_deletes_memory_but_preserves_accounting(self):
        chart_id=self.chart();self.long_chat(chart_id)
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply('Notes')):
            self.assertEqual(self.ask(chart_id).status_code,200)
        used=self.client.get('/account/usage',headers=self.owner).json()['gpt_messages_used']
        self.assertEqual(self.client.delete(f'/natal-chart/{chart_id}',headers=self.owner).status_code,204)
        with self.sessions() as db:self.assertIsNone(db.get(GPTConversation,chart_id))
        self.assertEqual(self.client.get('/account/usage',headers=self.owner).json()['gpt_messages_used'],used)

    def test_input_validation_never_calls_openai_or_reserves(self):
        chart_id=self.chart()
        with patch('modules.interpretation.client.chat.completions.create') as api:
            for question in ('', '   ', 'x'*4001):self.assertEqual(self.ask(chart_id,question).status_code,422)
            api.assert_not_called()
        with self.sessions() as db:self.assertEqual(db.query(GPTUsage).count(),0)

    def test_expired_response_cannot_overwrite_newer_memory_or_charge(self):
        chart_id=self.chart()
        with self.sessions() as db:
            first=usage.reserve(db,1,chart_id)
            db.get(GPTUsage,first).expires_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
            second=usage.reserve(db,1,chart_id)
            through=db.query(GPTMessage.id).filter_by(chart_id=chart_id).order_by(GPTMessage.id.desc()).first()[0]
            fresh=ai.AIAnswer('Fresh answer',metadata={'model':'mock'},memory_update=ai.MemoryUpdate(0,through,'Fresh memory'))
            usage.finalize(db,1,second,'Fresh question',fresh)
            stale=ai.AIAnswer('Stale answer',metadata={'model':'mock'},memory_update=ai.MemoryUpdate(0,through,'Stale memory'))
            with self.assertRaises(Exception):usage.finalize(db,1,first,'Stale question',stale)
            self.assertEqual(db.get(GPTConversation,chart_id).summary,'Fresh memory')
            self.assertEqual(db.get(GPTUsage,first).status,'released')
            self.assertEqual(db.query(GPTMessage).filter_by(chart_id=chart_id).count(),4)

    def test_oversized_legacy_message_is_bounded_only_for_summary_raw_history_survives(self):
        chart_id=self.chart()
        original='HEAD '+('x'*40000)+' TAIL'
        with self.sessions() as db:
            db.add(GPTMessage(chart_id=chart_id,role='user',content=original));db.commit()
        self.long_chat(chart_id,pairs=6)
        with patch('modules.interpretation.client.chat.completions.create',return_value=reply('Compact notes')) as api:
            self.assertEqual(self.ask(chart_id).status_code,200)
            summary_calls=[call.kwargs['messages'] for call in api.call_args_list[:-1]]
            summary_text=' '.join(messages[1]['content'] for messages in summary_calls)
            self.assertIn('middle omitted',summary_text)
            self.assertIn('HEAD',summary_text);self.assertIn('TAIL',summary_text)
            for messages in summary_calls:
                self.assertLessEqual(len(messages[1]['content'].encode('utf-8')),ai.SUMMARY_INPUT_BUDGET)
        self.assertIn(('user',original),self.snapshot(chart_id)[0])
