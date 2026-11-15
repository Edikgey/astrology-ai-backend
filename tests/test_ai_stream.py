"""Real OpenAI SDK stream accumulation with fake transport chunks, no paid calls."""
import json
import logging
import re
import unittest
from unittest.mock import patch
import httpx
import openai
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


# BE-CHAT-OBS DESIGN 3.1 / 3.2: the closed reason of an interrupted chat and the exact shape of its journal line.
REQUEST = httpx.Request('POST', 'https://api.openai.com/v1/chat/completions')
CUT_TEXT = 'Ответ прервался. Обновите историю перед повторной отправкой.'
UNFINISHED_TEXT = 'Ответ прервался. Квота не списана; повторите вопрос.'     # DESIGN 4: the stream ended without finish_reason=stop
REASON_LINE = re.compile(
    r'^AI chat outcome=interrupted reason=(?P<reason>timeout|connection|auth|not_found|rate_limit|quota|bad_request|'
    r'api_status_5xx|api_error|unfinished|other) status=(?P<status>\d{3}|none) '
    r'subject=(natal|relationship|topic_career|topic_money|topic_love) model=\S+ '
    r'output_tokens=(\d+|none) duration_ms=\d+$')
STATUS_ERRORS = {400: openai.BadRequestError, 401: openai.AuthenticationError, 403: openai.PermissionDeniedError,
                 404: openai.NotFoundError, 422: openai.UnprocessableEntityError, 429: openai.RateLimitError,
                 500: openai.InternalServerError, 503: openai.InternalServerError}


def status_error(http_status, text='fake provider refusal', **body):
    """An SDK status error built locally, the way the SDK builds it from a response; no network."""
    response = httpx.Response(http_status, request=REQUEST)
    return STATUS_ERRORS[http_status](text, response=response, body={'message': text, **body})


class FailingStream(ProviderStream):
    """Provider stream that delivers its first chunks and then raises the given error."""
    def __init__(self, error, **kwargs):
        super().__init__(**kwargs)
        self.error = error

    def __iter__(self):
        for number, chunk in enumerate(super().__iter__()):
            if number == 3:
                raise self.error
            yield chunk


def outcome_lines(logs):
    return [record.getMessage() for record in logs.records if record.getMessage().startswith('AI chat outcome=')]


class InterruptedReasonTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown
    chart = fixtures.MyChartsTests.chart
    reserve = StreamingTests.reserve
    state = StreamingTests.state

    def interrupted(self, cid, question='Question', **provider):
        """One chat run with a patched provider; returns (events, journal lines, api mock, reservation state)."""
        rid = self.reserve(cid)
        with self.assertLogs(level='WARNING') as logs, \
                patch('modules.interpretation.client.chat.completions.create', **provider) as api:
            events = list(chat_events(self.engine, 1, cid, question, rid, client, OPENAI_PARAMS))
        return events, outcome_lines(logs), api, self.state(rid)

    def check(self, expected, events, lines, api, state):
        """One closed line with the expected reason and status; the user text and the release are unchanged."""
        self.assertEqual(len(lines), 1, lines)
        found = REASON_LINE.match(lines[0])
        self.assertIsNotNone(found, lines[0])
        self.assertEqual((found['reason'], found['status']), expected, lines[0])
        text = UNFINISHED_TEXT if expected[0] == 'unfinished' else CUT_TEXT
        self.assertEqual(events[-1], {'type': 'error', 'status': 502, 'detail': text})
        self.assertNotIn('done', [e['type'] for e in events])
        api.assert_called_once()
        self.assertEqual(state[0], 'released')

    def test_obs1_request_errors_get_a_closed_reason_and_status(self):
        """DESIGN 5 test 1: the error raised by the provider call itself -> reason/status from the DESIGN 3.1 table."""
        cid = self.chart()
        cases = (
            ('api timeout', openai.APITimeoutError(request=REQUEST), ('timeout', 'none')),
            ('httpx connect timeout', httpx.ConnectTimeout('slow'), ('timeout', 'none')),
            ('builtin timeout', TimeoutError('slow'), ('timeout', 'none')),
            ('api connection', openai.APIConnectionError(request=REQUEST), ('connection', 'none')),
            ('httpx connect error', httpx.ConnectError('down'), ('connection', 'none')),
            ('401', status_error(401), ('auth', '401')),
            ('403', status_error(403), ('auth', '403')),
            ('404', status_error(404), ('not_found', '404')),
            ('429 rate limit', status_error(429, code='rate_limit_exceeded'), ('rate_limit', '429')),
            ('429 no money', status_error(429, code='insufficient_quota'), ('quota', '429')),
            ('400', status_error(400), ('bad_request', '400')),
            ('422', status_error(422), ('bad_request', '422')),
            ('500', status_error(500), ('api_status_5xx', '500')),
            ('503', status_error(503), ('api_status_5xx', '503')),
            ('our own error', ValueError('not from the provider'), ('other', 'none')),
        )
        for name, error, expected in cases:
            with self.subTest(case=name):
                self.check(expected, *self.interrupted(cid, side_effect=error))

    def test_obs2_mid_stream_failures_get_a_closed_reason(self):
        """DESIGN 5 test 2: the stream breaks after the first deltas, or ends without finish_reason=stop."""
        cid = self.chart()
        error_event = openai.APIError('fake error event inside the stream', REQUEST, body={'message': 'fake'})
        cases = (
            ('builtin timeout', FailingStream(TimeoutError('slow')), ('timeout', 'none')),
            ('httpx read timeout', FailingStream(httpx.ReadTimeout('slow')), ('timeout', 'none')),
            ('remote protocol error', FailingStream(httpx.RemoteProtocolError('cut')), ('connection', 'none')),
            ('error event in the stream', FailingStream(error_event), ('api_error', 'none')),
            ('no finish_reason', ProviderStream(finish=None), ('unfinished', 'none')),
            ('finish_reason tool_calls', ProviderStream(finish='tool_calls'), ('unfinished', 'none')),
        )
        for name, provider, expected in cases:
            with self.subTest(case=name):
                self.check(expected, *self.interrupted(cid, return_value=provider))

    def test_obs3_reason_line_has_no_error_text_key_question_or_ids(self):
        """DESIGN 5 test 3: canaries in the provider error and in the question never reach any journal record."""
        cid = self.chart()
        question = 'КАНАРЕЙКА-ВОПРОС-7731 расскажи про мою карту'
        secret = 'КАНАРЕЙКА-ОШИБКА sk-proj-CANARY req_CANARY'
        error = status_error(401, secret, code='invalid_api_key', request_id='req_CANARY')
        rid = self.reserve(cid)
        with self.assertLogs(level='WARNING') as logs, \
                patch('modules.interpretation.client.chat.completions.create', side_effect=error):
            events = list(chat_events(self.engine, 1, cid, question, rid, client, OPENAI_PARAMS))
        self.assertEqual(events[-1], {'type': 'error', 'status': 502, 'detail': CUT_TEXT})
        lines = outcome_lines(logs)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r'^AI chat outcome=interrupted reason=auth status=401 subject=natal ')
        self.assertIsNotNone(REASON_LINE.match(lines[0]), lines[0])
        rendered = '\n'.join(logging.Formatter().format(record) + repr(record.args) for record in logs.records)
        for canary in ('КАНАРЕЙКА', 'sk-proj', 'CANARY', 'req_', question, secret):
            self.assertNotIn(canary, rendered)
        for private in ('chart_id', 'user_id', f'chart={cid}', 'a@example.test', 'Traceback'):
            self.assertNotIn(private, rendered)
        for record in logs.records:
            if record.getMessage().startswith('AI chat outcome='):
                self.assertTrue(all(type(arg) in (str, int) for arg in record.args), record.args)
