"""Real OpenAI SDK stream accumulation with fake transport chunks, no paid calls."""
import json
import unittest
from unittest.mock import patch
import httpx
import anyio
from openai.types.chat import ChatCompletionChunk
from sqlalchemy import event
import test_my_charts as fixtures
from database.queries import GPTMessage, GPTUsage
from modules import usage
from modules.ai_stream import chat_events, streaming_response
from modules.interpretation import client, OPENAI_PARAMS


SUGGESTIONS = [
    {'type': 'deepen', 'text': 'Что усиливает эту реакцию?'},
    {'type': 'personalize', 'text': 'Как мне заметить это в повседневной жизни?'},
    {'type': 'explore', 'text': 'Как это связано с доверием?'},
]


class ProviderStream:
    def __init__(self, payload=None, fail=False, finish='stop'):
        self.response = httpx.Response(200)
        self.payload = payload or {'answer': 'Мне важны "границы".\nМой путь 🌙', 'follow_up_suggestions': SUGGESTIONS}
        self.fail, self.finish = fail, finish
        self.read = 0

    def __iter__(self):
        raw = json.dumps(self.payload, ensure_ascii=True)
        for start in range(0, len(raw), 5):
            self.read += 1
            if self.fail and start > 65:
                raise TimeoutError('fake network failure')
            yield ChatCompletionChunk(id='test', object='chat.completion.chunk', created=1, model='gpt-4o-mini',
                choices=[{'index': 0, 'delta': {'role': 'assistant', 'content': raw[start:start+5]}, 'finish_reason': None}])
        if self.finish is not None:
            yield ChatCompletionChunk(id='test', object='chat.completion.chunk', created=1, model='gpt-4o-mini',
                choices=[{'index': 0, 'delta': {}, 'finish_reason': self.finish}])
            yield ChatCompletionChunk(id='test', object='chat.completion.chunk', created=1, model='gpt-4o-mini',
                choices=[], usage={'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140})


class StreamingTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart

    def reserve(self, cid):
        with self.sessions() as db:
            return usage.reserve(db, 1, cid)

    def state(self, reservation):
        with self.sessions() as db:
            r = db.get(GPTUsage, reservation)
            return r.status, r.total_tokens

    def test_sse_route_streams_answer_then_done_persists_once_and_charges_once(self):
        cid = self.chart()
        provider = ProviderStream()
        with patch('modules.interpretation.client.chat.completions.create', return_value=provider) as api:
            result = self.client.post('/ask-gpt', headers={**self.owner, 'Accept': 'text/event-stream'},
                                      json={'chart_id': cid, 'question': 'Как мне двигаться дальше?'})
        self.assertEqual(result.status_code, 200)
        self.assertIn('text/event-stream', result.headers['content-type'])
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        self.assertEqual(events[0]['type'], 'start')
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(''.join(e['text'] for e in events if e['type']=='delta'), provider.payload['answer'])
        self.assertGreater(len([e for e in events if e['type']=='delta']), 2)
        self.assertTrue(all('follow_up_suggestions' not in e for e in events[:-1]))
        self.assertEqual(events[-1]['follow_up_suggestions'], SUGGESTIONS)
        api.assert_called_once()
        self.assertTrue(api.call_args.kwargs['stream'])
        self.assertEqual(api.call_args.kwargs['stream_options'], {'include_usage': True})
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).filter_by(chart_id=cid).count(), 4)
            self.assertEqual(db.query(GPTMessage).order_by(GPTMessage.id.desc()).first().content, provider.payload['answer'])
            self.assertEqual(db.query(GPTUsage).filter_by(total_tokens=140, status='succeeded').count(), 1)
        self.assertTrue(provider.response.is_closed)

    def test_partial_arrives_before_provider_finishes_and_disconnect_releases(self):
        cid=self.chart(); rid=self.reserve(cid); provider=ProviderStream()
        with patch('modules.interpretation.client.chat.completions.create', return_value=provider) as api:
            parts=chat_events(self.engine,1,cid,'Question',rid,client,OPENAI_PARAMS)
            self.assertEqual(next(parts)['type'],'start')
            partial=next(parts)
            self.assertEqual(partial['type'],'delta')
            self.assertLess(provider.read,10)
            self.assertEqual(self.state(rid),('reserved',None))
            parts.close()
        self.assertEqual(self.state(rid),('released',None))
        self.assertTrue(provider.response.is_closed)
        api.assert_called_once()
        with self.sessions() as db:self.assertEqual(db.query(GPTMessage).filter_by(chart_id=cid).count(),2)

    def test_network_error_truncation_or_eof_never_finalizes(self):
        cid=self.chart()
        for args in ({'fail':True},{'finish':'length'},{'finish':None}):
            rid=self.reserve(cid);provider=ProviderStream(**args)
            with patch('modules.interpretation.client.chat.completions.create',return_value=provider) as api:
                events=list(chat_events(self.engine,1,cid,'Question',rid,client,OPENAI_PARAMS))
            self.assertEqual(events[-1]['type'],'error')
            self.assertNotIn('done',[e['type'] for e in events])
            self.assertEqual(self.state(rid),('released',None))
            api.assert_called_once()

    def test_malformed_suggestions_keep_completed_stream_answer(self):
        cid=self.chart();rid=self.reserve(cid)
        provider=ProviderStream({'answer':'Valid answer','follow_up_suggestions':'bad'})
        with patch('modules.interpretation.client.chat.completions.create',return_value=provider):
            events=list(chat_events(self.engine,1,cid,'Question',rid,client,OPENAI_PARAMS))
        self.assertEqual(events[-1],{'type':'done','response':'Valid answer','follow_up_suggestions':[]})
        self.assertEqual(self.state(rid),('succeeded',140))

    def test_db_failure_after_tokens_rolls_back_messages_and_releases(self):
        cid=self.chart();rid=self.reserve(cid)
        def fail(conn,cursor,statement,*args):
            if statement.startswith('INSERT INTO gpt_messages'): raise RuntimeError('fake persistence error')
        event.listen(self.engine,'before_cursor_execute',fail)
        try:
            with patch('modules.interpretation.client.chat.completions.create',return_value=ProviderStream()):
                events=list(chat_events(self.engine,1,cid,'Question',rid,client,OPENAI_PARAMS))
        finally:event.remove(self.engine,'before_cursor_execute',fail)
        self.assertEqual(events[-1]['type'],'error')
        self.assertEqual(self.state(rid),('released',None))
        with self.sessions() as db:self.assertEqual(db.query(GPTMessage).filter_by(chart_id=cid).count(),2)

    def test_async_response_close_releases_even_before_provider_request(self):
        cid=self.chart();rid=self.reserve(cid)
        async def disconnect():
            response=streaming_response(self.engine,1,cid,'Question',rid,client,OPENAI_PARAMS)
            await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
        with patch('modules.interpretation.client.chat.completions.create') as api:
            anyio.run(disconnect)
            api.assert_not_called()
        self.assertEqual(self.state(rid),('released',None))

    def test_asgi_disconnect_closes_provider_and_releases_immediately(self):
        cid=self.chart();rid=self.reserve(cid);provider=ProviderStream()
        async def disconnect():
            disconnected=anyio.Event()
            async def receive():
                await disconnected.wait()
                return {'type':'http.disconnect'}
            async def send(message):
                if b'"type": "delta"' in message.get('body',b''):
                    disconnected.set()
                    await anyio.sleep(0)
            response=streaming_response(self.engine,1,cid,'Question',rid,client,OPENAI_PARAMS)
            await response({'type':'http','asgi':{'spec_version':'2.3'}},receive,send)
            self.assertEqual(self.state(rid),('released',None))
            self.assertTrue(provider.response.is_closed)
        with patch('modules.interpretation.client.chat.completions.create',return_value=provider):
            anyio.run(disconnect)

    def test_sse_still_requires_auth_and_ownership(self):
        cid=self.chart(user_id=2)
        with patch('modules.interpretation.client.chat.completions.create') as api:
            r=self.client.post('/ask-gpt',headers={**self.owner,'Accept':'text/event-stream'},json={'chart_id':cid,'question':'Question'})
            self.assertEqual(r.status_code,403)
            r=self.client.post('/ask-gpt',headers={'Accept':'text/event-stream'},json={'chart_id':cid,'question':'Question'})
            self.assertEqual(r.status_code,401)
            api.assert_not_called()
