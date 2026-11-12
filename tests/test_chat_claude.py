"""CHAT-CLAUDE-be regression, steps S1 (CC-1 ... CC-13, CC-15, CC-17) and S2 (CC-14, CC-16): the AI chat can run on Claude (Anthropic) instead of OpenAI.

Contract source: tasks/CHAT-CLAUDE/DESIGN.md sections 4 (provider), 7 (invariants), 8 (regression list).
Written BEFORE the code: on 0852c88 every test here is red because the behaviour is missing (modules.interpretation has no
select_chat_backend, there is no modules/ai_anthropic.py, requirements.txt has no Anthropic SDK lines), not because of a typo.

Method: real product code (context, quota, chat_events/SSE, routes) + the REAL ``anthropic`` SDK talking over loopback HTTP to
tests/anthropic_fake_server.py + isolated SQLite + synthetic data.  No real Anthropic or OpenAI call is possible: ANTHROPIC_* and
OPENAI_* variables of the shell are cleared in setUp, every endpoint is an explicit 127.0.0.1 address, and a socket guard
records (and refuses) any non-loopback connect.  CC-14 / CC-16 (prompt v3, step S2) are at the end of the file.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import unittest
from contextlib import ExitStack, contextmanager
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import anthropic
import openai

import anthropic_fake_server as fake
import test_chat_quality as cq
import test_relationship_ai as rel
from test_ai_stream import ProviderStream
from database.queries import GPTConversation, GPTMessage, GPTUsage, RelationshipMessage
from modules import usage
from modules.ai_stream import chat_events
from modules.astrology_prompt import ASTROLOGY_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from modules.relationship_prompt import RELATIONSHIP_SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[1]
STUB_KEY = 'sk-ant-test-0000000000000000'
MODEL = 'claude-haiku-5-5'
TRUNCATED, FILTERED, NETWORK_CUT = cq.TRUNCATED, cq.FILTERED, cq.NETWORK_CUT
INTERRUPTED = 'Ответ прервался. Квота не списана; повторите вопрос.'
ANSWER = 'Мне важны "границы".\nМой путь 🌙 и обратный \\ слеш.'
SUGGESTIONS = fake.SUGGESTIONS
# usage that fake.DEFAULT_USAGE reports: input 11 + cache write 3000 + cache read 500 = 3511 full input, 77 output
FULL_INPUT, OUTPUT, TOTAL = 3511, 77, 3588
ANSWER_FIELDS = {'model', 'max_tokens', 'system', 'messages', 'thinking', 'output_config'}
FORBIDDEN_FIELDS = ('temperature', 'top_p', 'top_k', 'fallbacks', 'stop_sequences', 'tools', 'tool_choice', 'metadata',
                    'betas', 'inference_geo', 'service_tier', 'max_completion_tokens', 'response_format', 'stream_options',
                    'reasoning_effort', 'container', 'mcp_servers', 'context_management')
SCHEMA_FORBIDDEN_KEYS = ('maxItems', 'minLength', 'maxLength', 'title')


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def blocks(content):
    """A message's content as a list of blocks (the API accepts a bare string or a list of blocks)."""
    return [{'type': 'text', 'text': content}] if isinstance(content, str) else list(content)


def texts(message):
    return [block['text'] for block in blocks(message['content'])]


def system_text(body):
    return ''.join(texts({'content': body['system']}))


def cut_inside_escapes(text, step=4):
    """Boundaries between a backslash and the character it escapes, plus every `step` characters."""
    edges = sorted({i for i in range(1, len(text)) if text[i - 1] == '\\'} | set(range(step, len(text), step)))
    return [text[a:b] for a, b in zip([0] + edges, edges + [len(text)])]


@contextmanager
def journal():
    """All log records from INFO up (SDK debug logging stays off, as in production)."""
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler, root = Collect(level=logging.INFO), logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def rendered(records):
    return '\n'.join(logging.Formatter().format(r) + repr(r.args) for r in records)


def lines(records, marker):
    return [r.getMessage() for r in records if marker in r.getMessage()]


def read_timeout(value):
    return getattr(value, 'read', value)


class ClaudeBase(cq.ChatQualityBase):
    """Isolated DB + fake Anthropic server on loopback + clean environment + loopback-only socket guard."""
    pair = rel.RelationshipAITests.pair

    def setUp(self):
        super().setUp()
        self.fake = fake.FakeAnthropic().start()
        self.addCleanup(self.fake.stop)
        environment = patch.dict(os.environ, {'OPENAI_API_KEY': 'test-not-a-real-key'})
        environment.start()
        self.addCleanup(environment.stop)
        for name in [k for k in os.environ if k.startswith(('ANTHROPIC_', 'OPENAI_BASE', 'LUNARIA_CHAT'))
                     or k in ('OPENAI_MODEL', 'OPENAI_REASONING_EFFORT')]:
            del os.environ[name]
        self.foreign = []
        real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

        def loopback(address):
            return not isinstance(address, tuple) or address[0] in ('127.0.0.1', '::1', 'localhost')

        def connect(sock, address):
            if not loopback(address):
                self.foreign.append(str(address[0]))
                raise ConnectionRefusedError('CHAT-CLAUDE tests: non-loopback connection refused')
            return real_connect(sock, address)

        def connect_ex(sock, address):
            if not loopback(address):
                self.foreign.append(str(address[0]))
                return 111
            return real_connect_ex(sock, address)

        for name, replacement in (('connect', connect), ('connect_ex', connect_ex)):
            guard = patch.object(socket.socket, name, replacement)
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(lambda: self.assertEqual(self.foreign, [], 'a test tried to leave the loopback interface'))

    # --- backend selection (the product's own function, driven by an explicit mapping, never by the shell) ---
    def env(self, **extra):
        values = {'LUNARIA_CHAT_PROVIDER': 'anthropic', 'ANTHROPIC_API_KEY': STUB_KEY,
                  'ANTHROPIC_BASE_URL': self.fake.url, 'OPENAI_API_KEY': 'test-not-a-real-key', **extra}
        return {k: v for k, v in values.items() if v is not None}

    def select(self, env):
        from modules import interpretation
        chooser = getattr(interpretation, 'select_chat_backend', None)
        if chooser is None:
            self.fail('modules.interpretation.select_chat_backend is missing: the provider cannot be chosen by '
                      'LUNARIA_CHAT_PROVIDER (CHAT-CLAUDE S2)')
        return chooser(env)

    def claude(self, **extra):
        client, params = self.select(self.env(**extra))
        return client, params

    def use_claude(self, **extra):
        """Make the HTTP routes (they read modules.interpretation.client / OPENAI_PARAMS per request) use Claude."""
        from modules import interpretation
        client, params = self.claude(**extra)
        stack = ExitStack()
        stack.enter_context(patch.object(interpretation, 'client', client))
        stack.enter_context(patch.object(interpretation, 'OPENAI_PARAMS', params))
        self.addCleanup(stack.close)
        return client, params

    # --- running a streamed question exactly as the SSE route does ---
    def stream(self, cid, client, params, question='Расскажи подробнее про мою карту', relationship_id=None):
        with self.sessions() as db:
            reservation = (usage.reserve(db, 1, cid) if relationship_id is None
                           else usage.reserve(db, 1, relationship_id=relationship_id))
        extra = {} if relationship_id is None else {'relationship_id': relationship_id}
        with journal() as records:
            events = list(chat_events(self.engine, 1, cid, question, reservation, client, params, **extra))
        with self.sessions() as db:
            status = db.get(GPTUsage, reservation).status
        return events, status, records, reservation

    def stored(self, cid):
        with self.sessions() as db:
            return db.query(GPTMessage).filter_by(chart_id=cid).count()

    def usage_row(self, reservation):
        with self.sessions() as db:
            row = db.get(GPTUsage, reservation)
            return {k: getattr(row, k) for k in ('status', 'model', 'input_tokens', 'output_tokens', 'total_tokens',
                                                 'summary_usage')}

    def check_answer_request(self, body, stream):
        """CC-3 / CC-10 / CC-11: the one request shape of an answer (DESIGN section 4.3)."""
        self.assertEqual(set(body) - {'stream'}, ANSWER_FIELDS)
        self.assertEqual(body.get('stream') is True, stream)
        for name in FORBIDDEN_FIELDS:
            self.assertNotIn(name, body, name)
        self.assertEqual(body['model'], MODEL)
        self.assertEqual(body['max_tokens'], 4000)                       # 2000 answer + 2000 reasoning allowance
        self.assertEqual(body['thinking'], {'type': 'adaptive'})
        self.assertEqual(set(body['output_config']), {'effort', 'format'})
        self.assertEqual(body['output_config']['effort'], 'low')
        self.assertEqual(body['output_config']['format']['type'], 'json_schema')
        schema = body['output_config']['format']['schema']
        self.assertEqual(schema['type'], 'object')
        self.assertEqual(set(schema['required']), {'answer', 'follow_up_suggestions'})
        for node in walk(schema):
            if isinstance(node, dict):
                for key in SCHEMA_FORBIDDEN_KEYS:
                    self.assertNotIn(key, node, f'{key} makes the API answer 400')
                self.assertLessEqual(node.get('minItems', 0), 1)
                if node.get('type') == 'object':
                    self.assertIs(node.get('additionalProperties'), False, node)
        # the prompt and the chart are cached, nothing else
        system = body['system']
        self.assertIsInstance(system, list)
        self.assertEqual(len(system), 1)
        self.assertEqual(system[0]['type'], 'text')
        self.assertEqual(system[0]['cache_control'], {'type': 'ephemeral'})
        marks = [node['cache_control'] for node in walk(body) if isinstance(node, dict) and 'cache_control' in node]
        self.assertEqual(marks, [{'type': 'ephemeral'}] * 2)
        first = body['messages'][0]
        self.assertEqual(first['role'], 'user')
        self.assertEqual(blocks(first['content'])[0]['cache_control'], {'type': 'ephemeral'})
        # strict alternation, starting and ending with the user, no empty text anywhere
        roles = [m['role'] for m in body['messages']]
        self.assertEqual(roles[0], 'user')
        self.assertEqual(roles[-1], 'user')
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)
        for message in body['messages']:
            for text in texts(message):
                self.assertTrue(text.strip(), 'an empty text block is an API error')


# ---------------------------------------------------------------- CC-1, CC-2: choosing the provider
SUBPROCESS_ENV = {'DATABASE_URL': 'sqlite://', 'SECRET_KEY': 'chat-claude-isolated-tests-only',
                  'OPENAI_API_KEY': 'test-not-a-real-key', 'PYTHONUTF8': '1'}


def python(code, **extra):
    """Run code in a fresh interpreter with the product importable and the shell's ANTHROPIC_*/OPENAI_BASE* removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'OPENAI_BASE', 'LUNARIA_CHAT'))}
    env.update(SUBPROCESS_ENV)
    env.update(extra)
    run = subprocess.run([sys.executable, '-c', f'import sys; sys.path.insert(0, {str(ROOT)!r})\n' + code],
                         cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
    return run


class SelectBackendTests(ClaudeBase):
    def default_choice(self, env):
        with journal() as records:
            client, params = self.select(env)
        self.assertIsInstance(client, openai.OpenAI)
        self.assertEqual(client.max_retries, 0)
        self.assertEqual(read_timeout(client.timeout), 60)
        self.assertEqual(lines(records, 'AI chat provider'), [], 'the default configuration adds no journal line')
        return client, params

    def test_cc1_empty_or_openai_gives_the_unchanged_openai_client_and_parameters(self):
        for env in ({}, {'LUNARIA_CHAT_PROVIDER': 'openai'},
                    {'LUNARIA_CHAT_PROVIDER': 'openai', 'ANTHROPIC_API_KEY': STUB_KEY, 'ANTHROPIC_CHAT_MODEL': 'claude-x'}):
            with self.subTest(env=sorted(env)):
                _, params = self.default_choice(env)
                self.assertEqual(params, {'model': 'gpt-4o-mini', 'temperature': 0.7})

    def test_cc1_openai_model_variable_still_applies(self):
        with patch.dict(os.environ, {'OPENAI_MODEL': 'gpt-test-model'}):
            _, params = self.default_choice({'OPENAI_MODEL': 'gpt-test-model'})
        self.assertEqual(params, {'model': 'gpt-test-model', 'temperature': 0.7})

    def test_cc1_default_import_never_loads_the_anthropic_sdk(self):
        code = ('import json\n'
                'import modules.interpretation as m\n'
                'first = m.select_chat_backend({})\n'
                'second = m.select_chat_backend({"LUNARIA_CHAT_PROVIDER": "openai", "ANTHROPIC_API_KEY": "sk-ant-test-1"})\n'
                'print(json.dumps({"loaded": "anthropic" in sys.modules, "module_client": type(m.client).__module__,\n'
                '    "choices": [type(first[0]).__module__, type(second[0]).__module__],\n'
                '    "params": [first[1], second[1], m.OPENAI_PARAMS]}))\n')
        run = python(code, ANTHROPIC_API_KEY='sk-ant-test-1')       # key present, provider variable absent
        self.assertEqual(run.returncode, 0, run.stderr[-1500:])
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertFalse(result['loaded'], 'without LUNARIA_CHAT_PROVIDER=anthropic the SDK must not even be imported')
        self.assertEqual(result['module_client'], 'openai')
        self.assertEqual(result['choices'], ['openai', 'openai'])
        for params in result['params']:
            self.assertEqual(params, {'model': 'gpt-4o-mini', 'temperature': 0.7})

    def test_cc1_misspelt_provider_falls_back_to_openai_with_one_line_that_hides_the_value(self):
        with journal() as records:
            client, params = self.select({'LUNARIA_CHAT_PROVIDER': 'anthroppic-SECRETVALUE', 'ANTHROPIC_API_KEY': STUB_KEY})
        self.assertIsInstance(client, openai.OpenAI)
        self.assertEqual(params, {'model': 'gpt-4o-mini', 'temperature': 0.7})
        found = lines(records, 'AI chat provider')
        self.assertEqual(len(found), 1, found)
        self.assertIn('provider=unknown', found[0])
        self.assertIn('fallback=openai', found[0])
        self.assertNotIn('SECRETVALUE', rendered(records))
        self.assertNotIn(STUB_KEY, rendered(records))

    def test_cc1_anthropic_without_a_key_falls_back_to_openai_with_reason_no_key(self):
        for key in (None, ''):
            with self.subTest(key=key):
                with journal() as records:
                    client, params = self.select(self.env(ANTHROPIC_API_KEY=key))
                self.assertIsInstance(client, openai.OpenAI)
                self.assertNotIn('provider', params)
                self.assertEqual(params, {'model': 'gpt-4o-mini', 'temperature': 0.7})
                found = lines(records, 'AI chat provider')
                self.assertEqual(len(found), 1, found)
                for part in ('provider=anthropic', 'outcome=fallback', 'reason=no_key'):
                    self.assertIn(part, found[0])

    def test_cc2_anthropic_choice_builds_the_real_sdk_client_without_retries_and_with_a_60_second_timeout(self):
        with journal() as records:
            client, params = self.claude()
        self.assertIsInstance(client, anthropic.Anthropic)
        self.assertEqual(client.max_retries, 0, 'a paid call must never be repeated by the SDK')
        self.assertEqual(read_timeout(client.timeout), 60)
        self.assertEqual(str(client.base_url).rstrip('/'), self.fake.url)
        self.assertEqual(params, {'provider': 'anthropic', 'model': MODEL, 'effort': 'low'})
        found = lines(records, 'AI chat provider')
        self.assertEqual(len(found), 1, found)
        for part in ('provider=anthropic', f'model={MODEL}', 'effort=low'):
            self.assertIn(part, found[0])
        self.assertNotIn(STUB_KEY, rendered(records))

    def test_cc2_model_and_effort_variables_take_effect(self):
        for effort in ('low', 'medium', 'high'):
            with self.subTest(effort=effort):
                with journal() as records:
                    _, params = self.claude(ANTHROPIC_CHAT_MODEL='claude-sonnet-5-5', ANTHROPIC_CHAT_EFFORT=effort)
                self.assertEqual(params, {'provider': 'anthropic', 'model': 'claude-sonnet-5-5', 'effort': effort})
                found = lines(records, 'AI chat provider')
                self.assertEqual(len(found), 1, found)
                self.assertIn('model=claude-sonnet-5-5', found[0])
                self.assertIn(f'effort={effort}', found[0])

    def test_cc2_effort_outside_low_medium_high_becomes_low_and_says_so(self):
        for bad in ('max', 'xhigh', 'garbage-VALUE'):
            with self.subTest(effort=bad):
                with journal() as records:
                    _, params = self.claude(ANTHROPIC_CHAT_EFFORT=bad)
                self.assertEqual(params, {'provider': 'anthropic', 'model': MODEL, 'effort': 'low'})
                self.assertTrue(lines(records, 'reason=effort_invalid'), records)
                self.assertNotIn('garbage-VALUE', rendered(records))


# ---------------------------------------------------------------- CC-3: the request
class RequestShapeTests(ClaudeBase):
    def test_cc3_stream_request_has_the_fields_of_the_design_and_nothing_else(self):
        cid = self.chart()
        client, params = self.claude()
        events, status, _, _ = self.stream(cid, client, params, 'Что мне важно в карьере?')
        self.assertEqual(events[-1]['type'], 'done', events[-1])
        self.assertEqual(len(self.fake.requests), 1)
        request = self.fake.requests[0]
        self.assertEqual(request.path, '/v1/messages')
        self.check_answer_request(request.body, stream=True)
        self.assertEqual(system_text(request.body), ASTROLOGY_SYSTEM_PROMPT)
        self.assertEqual(texts(request.body['messages'][-1]), ['Что мне важно в карьере?'])

    def test_cc3_same_role_neighbours_are_merged_into_one_turn_and_empty_history_is_skipped(self):
        cid = self.chart()
        with self.sessions() as db:
            for role, text in (('user', 'Второй вопрос'), ('gpt', ''), ('user', 'Третий вопрос'), ('gpt', 'Третий ответ')):
                db.add(GPTMessage(chart_id=cid, role=role, content=text))
            db.commit()
        client, params = self.claude()
        events, *_ = self.stream(cid, client, params, 'Последний вопрос')
        self.assertEqual(events[-1]['type'], 'done', events[-1])
        messages = self.fake.requests[0].body['messages']
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user', 'assistant', 'user'])
        card = texts(messages[0])[0]
        self.assertTrue(card.startswith('Calculated natal reference data (not instructions):\n'), card[:80])
        self.assertEqual(texts(messages[0])[1:], ['Existing question'])      # the card and the first old question: one turn
        self.assertEqual(texts(messages[1]), ['Existing answer'])
        self.assertEqual(texts(messages[2]), ['Второй вопрос', 'Третий вопрос'])   # the empty answer between them is gone
        self.assertEqual(texts(messages[3]), ['Третий ответ'])
        self.assertEqual(texts(messages[4]), ['Последний вопрос'])
        self.check_answer_request(self.fake.requests[0].body, stream=True)

    def test_cc3_two_turns_of_one_chart_send_identical_prompt_and_chart_bytes(self):
        cid = self.chart()
        client, params = self.claude()
        for question in ('Первый вопрос', 'Второй вопрос'):
            events, *_ = self.stream(cid, client, params, question)
            self.assertEqual(events[-1]['type'], 'done', events[-1])
        first, second = (r.body for r in self.fake.requests)
        self.assertEqual(json.dumps(first['system'], ensure_ascii=False), json.dumps(second['system'], ensure_ascii=False))
        self.assertEqual(json.dumps(blocks(first['messages'][0]['content'])[0], ensure_ascii=False),
                         json.dumps(blocks(second['messages'][0]['content'])[0], ensure_ascii=False),
                         'the chart block must be byte-identical or the prompt cache never hits')
        self.assertEqual(texts(second['messages'][-1]), ['Второй вопрос'])


# ---------------------------------------------------------------- CC-4: stream -> the same events
class StreamEventTests(ClaudeBase):
    def scenario(self, ascii_json):
        text = fake.envelope(ANSWER, ascii_json=ascii_json)
        return fake.Scenario(text=text, chunks=cut_inside_escapes(text), byte_chunk=None if ascii_json else 3)

    def openai_events(self, payload):
        cid = self.chart()
        from modules.interpretation import client, OPENAI_PARAMS
        with self.sessions() as db:
            reservation = usage.reserve(db, 1, cid)
        with patch('modules.interpretation.client.chat.completions.create', return_value=ProviderStream(payload)):
            return list(chat_events(self.engine, 1, cid, 'Вопрос', reservation, client, OPENAI_PARAMS))

    def test_cc4_natal_stream_gives_start_deltas_done_and_the_same_accounting(self):
        # Escaped JSON with cuts inside \" \n \u escapes, and raw UTF-8 JSON with the transport cut inside a character.
        for ascii_json in (True, False):
            with self.subTest(ascii_json=ascii_json):
                self.fake.requests.clear()
                cid = self.chart()
                before = self.stored(cid)
                self.fake.queue(self.scenario(ascii_json))
                client, params = self.claude()
                events, status, _, reservation = self.stream(cid, client, params, 'Как мне двигаться дальше?')
                types = [e['type'] for e in events]
                self.assertEqual(types[0], 'start')
                self.assertEqual(types[-1], 'done')
                self.assertTrue(set(types[1:-1]) == {'delta'} and len(types) > 4, types)
                self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'delta'), ANSWER)
                self.assertEqual(events[-1], {'type': 'done', 'response': ANSWER, 'follow_up_suggestions': SUGGESTIONS})
                self.assertTrue(all('follow_up_suggestions' not in e for e in events[:-1]))
                # the same shape as the OpenAI path on an equivalent provider
                reference = self.openai_events({'answer': ANSWER, 'follow_up_suggestions': SUGGESTIONS})
                self.assertEqual(events[-1], reference[-1])
                self.assertEqual(types[0], reference[0]['type'])
                self.assertEqual(set(types[1:-1]), {e['type'] for e in reference[1:-1]})
                # accounting
                self.assertEqual(status, 'succeeded')
                self.assertEqual(self.stored(cid), before + 2)
                row = self.usage_row(reservation)
                self.assertEqual((row['model'], row['input_tokens'], row['output_tokens'], row['total_tokens']),
                                 (MODEL, FULL_INPUT, OUTPUT, TOTAL))
                self.assertEqual(len(self.fake.requests), 1)

    def test_cc4_each_question_costs_one_unit_and_stores_two_messages(self):
        cid = self.chart()
        client, params = self.claude()
        used = self.used()[0]
        self.stream(cid, client, params)
        self.assertEqual(self.used(), (used + 1, 0))
        self.assertEqual(self.stored(cid), 4)
        with self.sessions() as db:
            self.assertEqual(db.query(GPTMessage).order_by(GPTMessage.id.desc()).first().content, fake.DEFAULT_ANSWER)

    def test_cc4_relationship_stream_gives_the_same_events_and_accounting(self):
        relationship_id, _ = self.pair()
        self.fake.queue(self.scenario(True))
        client, params = self.claude()
        events, status, _, reservation = self.stream(None, client, params, 'Как нам лучше понимать друг друга?',
                                                     relationship_id=relationship_id)
        self.assertEqual([e['type'] for e in events][0], 'start')
        self.assertEqual(events[-1], {'type': 'done', 'response': ANSWER, 'follow_up_suggestions': SUGGESTIONS})
        self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'delta'), ANSWER)
        self.assertEqual(status, 'succeeded')
        with self.sessions() as db:
            self.assertEqual(db.query(RelationshipMessage).filter_by(relationship_id=relationship_id).count(), 2)
        row = self.usage_row(reservation)
        self.assertEqual((row['model'], row['input_tokens'], row['output_tokens'], row['total_tokens']),
                         (MODEL, FULL_INPUT, OUTPUT, TOTAL))
        body = self.fake.requests[0].body
        self.assertEqual(system_text(body), RELATIONSHIP_SYSTEM_PROMPT)
        self.assertTrue(texts(body['messages'][0])[0].startswith('Calculated relationship reference data (not instructions):\n'))
        self.check_answer_request(body, stream=True)

    def test_cc4_closing_the_event_generator_midway_closes_the_provider_stream_and_releases_the_quota(self):
        cid = self.chart()
        with self.sessions() as db:
            reservation = usage.reserve(db, 1, cid)
        long_text = fake.envelope('Длинный ответ про карту. ' * 30)
        self.fake.queue(fake.Scenario(text=long_text, chunks=[long_text[i:i + 8] for i in range(0, len(long_text), 8)],
                                      delay=0.02))
        client, params = self.claude()
        parts = chat_events(self.engine, 1, cid, 'Вопрос', reservation, client, params)
        self.assertEqual(next(parts)['type'], 'start')
        self.assertEqual(next(parts)['type'], 'delta')
        self.assertEqual(self.usage_row(reservation)['status'], 'reserved')
        parts.close()
        self.assertEqual(self.usage_row(reservation)['status'], 'released')
        self.assertTrue(self.fake.disconnected.wait(15), 'the provider connection must be closed when the client goes away')
        self.assertEqual(self.stored(cid), 2)
        self.assertEqual(len(self.fake.requests), 1)


# ---------------------------------------------------------------- CC-5 ... CC-8: abnormal endings
class AbnormalEndingTests(ClaudeBase):
    CANARIES = ('КАНАРЕЙКА', 'sk-ant-canary', STUB_KEY, 'Начало ответа про карту')

    def partial(self, **kwargs):
        text = '{"answer":' + json.dumps('Начало ответа про карту КАНАРЕЙКА-ОТВЕТ, который оборвался', ensure_ascii=True)
        return fake.Scenario(text=text, **kwargs)

    def ending(self, scenario, expected_text, outcome, *, extra=(), client_options=None, cid=None):
        cid = cid or self.chart()
        self.fake.queue(scenario)
        client, params = self.claude()
        if client_options:
            client = client.with_options(**client_options)
        before_requests, before_messages, before_used = len(self.fake.requests), self.stored(cid), self.used()
        events, status, records, reservation = self.stream(cid, client, params, 'Расскажи подробнее КАНАРЕЙКА-ВОПРОС')
        self.assertEqual(events[-1], {'type': 'error', 'status': 502, 'detail': expected_text}, events[-1])
        self.assertNotIn('done', [e['type'] for e in events])
        self.assertEqual(status, 'released')
        self.assertEqual(self.stored(cid), before_messages, 'nothing may be saved')
        self.assertEqual(self.used(), before_used, 'nothing may be charged')
        self.assertEqual(len(self.fake.requests), before_requests + 1, 'exactly one provider call, no retry')
        found = lines(records, 'AI chat outcome=')
        self.assertEqual(len(found), 1, found)
        for part in (f'AI chat outcome={outcome}', 'subject=natal', f'model={MODEL}', 'duration_ms=', *extra):
            self.assertIn(part, found[0])
        # Neither the journal nor any non-text event may carry the question, the partial answer, a provider message or the key.
        # (The text deltas legitimately carried the partial answer; the frontend drops them on the error event.)
        shown = rendered(records) + json.dumps([e for e in events if e['type'] != 'delta'], ensure_ascii=False)
        for secret in (*self.CANARIES, 'КАНАРЕЙКА-ВОПРОС'):
            self.assertNotIn(secret, shown)
        self.assertNotIn('КАНАРЕЙКА', rendered(records))
        return found[0]

    def test_cc5_max_tokens_is_the_truncated_text_with_the_token_count_and_no_text_in_the_journal(self):
        usage_4000 = {**fake.DEFAULT_USAGE, 'output_tokens': 4000}
        line = self.ending(self.partial(stop_reason='max_tokens', usage=usage_4000), TRUNCATED, 'truncated',
                           extra=('output_tokens=4000',))
        self.assertNotIn('output_tokens=none', line)

    def test_cc6_refusals_are_the_filtered_text_with_a_closed_category_label(self):
        cases = [('general_harms', 'general_harms', False), ('bio', 'bio', True), ('cyber', 'cyber', False),
                 ('frontier_llm', 'frontier_llm', True), ('reasoning_extraction', 'reasoning_extraction', False),
                 (None, 'none', True), ('a_category_from_the_future', 'other', False)]
        for category, label, midway in cases:
            with self.subTest(category=category, midway=midway):
                if midway:      # after two chunks of text were already sent
                    text = self.partial().text
                    scenario = fake.refusal(category, text=text, chunks=[text[:5], text[5:10], text[10:]])
                else:           # before any text
                    scenario = fake.refusal(category)
                self.ending(scenario, FILTERED, 'filtered', extra=(f'refusal={label}',))

    def test_cc7_a_stop_reason_that_is_not_a_normal_end_is_the_interrupted_text(self):
        for reason in ('pause_turn', 'tool_use', 'stop_sequence', None, 'a_reason_from_the_future'):
            with self.subTest(stop_reason=reason):
                self.ending(fake.answer(stop_reason=reason), INTERRUPTED, 'interrupted')

    def test_cc8_every_sdk_failure_is_the_old_text_without_charge_leak_or_retry(self):
        long_text = fake.envelope('Ответ, который не успеет закончиться. ' * 3)
        cases = {
            'connection dropped before an answer': (fake.Scenario(drop=True), None),
            'read timeout': (fake.answer(stall=3.0), {'timeout': 0.4}),
            'HTTP 429': (fake.error(429, 'rate_limit_error'), None),
            'HTTP 500': (fake.error(500, 'api_error'), None),
            'HTTP 529': (fake.error(529, 'overloaded_error'), None),
            'HTTP 400': (fake.error(400, 'invalid_request_error'), None),
            'error event in the middle of the stream': (fake.Scenario(text=long_text, error_after=3), None),
            'connection cut in the middle of the stream': (fake.Scenario(text=long_text, abort_after=3), None),
        }
        for name, (scenario, options) in cases.items():
            with self.subTest(case=name):
                self.ending(scenario, NETWORK_CUT, 'interrupted', client_options=options, extra=('output_tokens=none',))


# ---------------------------------------------------------------- CC-9: the memory summary
class SummaryTests(ClaudeBase):
    def memory(self, cid, text='Original memory'):
        with self.sessions() as db:
            db.add(GPTConversation(chart_id=cid, user_id=1, summary=text, through_message_id=0))
            db.commit()

    def pending(self, request):
        notes = [t for m in request.body['messages'] for t in texts(m) if t.startswith('Older conversation memory')]
        self.assertEqual(len(notes), 1, 'the model must be told what its memory is')
        return notes[0]

    def test_cc9_summary_request_and_success(self):
        cid = self.chart()
        self.long_chat(cid)
        self.fake.queue(fake.summary('Новая сводка памяти'))
        self.use_claude()
        used = self.used()[0]
        response = self.ask(cid)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['response'], fake.DEFAULT_ANSWER)
        self.assertEqual((len(self.fake.summary_requests()), len(self.fake.answer_requests())), (1, 1))
        body = self.fake.requests[0].body
        self.assertTrue(self.fake.requests[0].is_summary)
        self.assertEqual(set(body) - {'stream'}, ANSWER_FIELDS)
        self.assertIsNot(body.get('stream'), True)
        self.assertEqual(body['model'], MODEL)
        self.assertEqual(body['max_tokens'], 2900)                       # 900 summary + 2000 reasoning allowance
        self.assertEqual(body['thinking'], {'type': 'adaptive'})
        self.assertEqual(body['output_config'], {'effort': 'low'})
        self.assertEqual(system_text(body), SUMMARY_SYSTEM_PROMPT)
        self.assertEqual([n for n in walk(body) if isinstance(n, dict) and 'cache_control' in n], [])
        self.assertEqual([m['role'] for m in body['messages']], ['user'])
        payload = json.loads(texts(body['messages'][0])[0])
        self.assertEqual(set(payload), {'previous_memory', 'older_dialogue'})
        for name in FORBIDDEN_FIELDS:
            self.assertNotIn(name, body)
        memory = self.snapshot(cid)[1]
        self.assertEqual(memory[0], 'Новая сводка памяти')
        self.assertGreater(memory[1], 0, 'the cursor moved')
        self.assertIn('Новая сводка памяти', self.pending(self.fake.requests[1]))
        self.assertEqual(self.used(), (used + 1, 0))
        with self.sessions() as db:
            row = db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one()
            self.assertEqual(row.model, MODEL)
            self.assertEqual(len(row.summary_usage), 1)
            self.assertEqual(row.summary_usage[0], {'model': MODEL, 'input_tokens': FULL_INPUT,
                                                    'output_tokens': OUTPUT, 'total_tokens': TOTAL})

    def test_cc9_a_cut_or_refused_summary_is_dropped_and_the_question_is_still_answered(self):
        cases = {'max_tokens': fake.summary('оборванная сводка', stop_reason='max_tokens'),
                 'refusal': fake.refusal('general_harms')}
        for name, scenario in cases.items():
            with self.subTest(case=name):
                self.fake.requests.clear()
                cid = self.chart()
                self.long_chat(cid)
                self.memory(cid)
                before, used = self.snapshot(cid), self.used()[0]
                self.fake.queue(scenario)
                self.use_claude()
                response = self.ask(cid)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['response'], fake.DEFAULT_ANSWER)
                self.assertEqual(len(self.fake.requests), 2, 'no repeated summary call in one request')
                note = self.pending(self.fake.requests[1])
                self.assertIn('"older_history_pending":true', note)
                self.assertIn('Original memory', note)
                self.assertEqual(self.snapshot(cid)[1], before[1], 'cursor and summary stay as they were')
                self.assertEqual(self.used(), (used + 1, 0))
                with self.sessions() as db:
                    row = db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).order_by(GPTUsage.id.desc()).first()
                    self.assertEqual(len(row.summary_usage), 1)
                    self.assertIs(row.summary_usage[0].get('discarded'), True)

    def test_cc9_a_dropped_summary_in_a_stream_logs_summary_degraded_and_still_finishes(self):
        cid = self.chart()
        self.long_chat(cid)
        self.memory(cid)
        self.fake.queue(fake.summary('оборванная сводка', stop_reason='max_tokens'))
        client, params = self.claude()
        events, status, records, reservation = self.stream(cid, client, params, 'Как мне двигаться дальше?')
        self.assertEqual(events[-1]['type'], 'done', events[-1])
        self.assertEqual(status, 'succeeded')
        self.assertEqual(len(lines(records, 'outcome=summary_degraded')), 1, [r.getMessage() for r in records])
        self.assertEqual(lines(records, 'outcome=filtered') + lines(records, 'outcome=truncated'), [])
        self.assertIs(self.usage_row(reservation)['summary_usage'][0].get('discarded'), True)

    def test_cc9_a_summary_network_failure_refuses_the_question_without_charge_or_retry(self):
        for name, scenario in {'HTTP 529': fake.error(529, 'overloaded_error'), 'dropped': fake.Scenario(drop=True)}.items():
            with self.subTest(case=name):
                self.fake.requests.clear()
                cid = self.chart()
                self.long_chat(cid)
                self.memory(cid)
                before, state = self.snapshot(cid), self.used()
                self.fake.queue(scenario)
                self.use_claude()
                self.assertGreaterEqual(self.ask(cid).status_code, 500)
                self.assertEqual(len(self.fake.requests), 1, 'the summary is not retried and the question is not asked')
                self.assertEqual(self.snapshot(cid), before)
                self.assertEqual(self.used(), state)

    def test_cc9_at_most_two_summary_calls_per_question(self):
        cid = self.chart()
        self.long_chat(cid, pairs=80)           # a backlog that would need many summaries
        with self.sessions() as db:
            for message in db.query(GPTMessage).filter_by(chart_id=cid).all():
                message.content += ' ' + 'x' * 1000
            db.commit()
        self.use_claude()
        self.assertEqual(self.ask(cid).status_code, 200)
        self.assertGreaterEqual(len(self.fake.summary_requests()), 1)
        self.assertLessEqual(len(self.fake.summary_requests()), 2)
        self.assertEqual(len(self.fake.answer_requests()), 1)


# ---------------------------------------------------------------- CC-10: no stream (JSON routes)
class NonStreamTests(ClaudeBase):
    def ask_relationship(self, relationship_id, question='Как нам лучше понимать друг друга?'):
        return self.client.post(f'/relationships/{relationship_id}/ask', json={'question': question}, headers=self.owner)

    def test_cc10_natal_ask_gpt_answers_through_messages_create(self):
        cid = self.chart()
        self.fake.queue(fake.answer(ANSWER))
        self.use_claude()
        used = self.used()[0]
        response = self.ask(cid, 'Что мне важно в карьере?')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['response'], ANSWER)
        self.assertEqual(response.json()['follow_up_suggestions'], SUGGESTIONS)
        self.assertEqual(len(self.fake.requests), 1)
        self.check_answer_request(self.fake.requests[0].body, stream=False)
        self.assertEqual(self.snapshot(cid)[0][-1], ('gpt', ANSWER))
        self.assertEqual(self.used(), (used + 1, 0))
        with self.sessions() as db:
            row = db.query(GPTUsage).filter(GPTUsage.model.is_not(None)).one()
            self.assertEqual((row.model, row.input_tokens, row.output_tokens, row.total_tokens),
                             (MODEL, FULL_INPUT, OUTPUT, TOTAL))

    def test_cc10_relationship_ask_without_sse_answers_through_messages_create(self):
        relationship_id, _ = self.pair()
        self.fake.queue(fake.answer(ANSWER))
        self.use_claude()
        response = self.ask_relationship(relationship_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['response'], ANSWER)
        self.assertEqual(response.json()['follow_up_suggestions'], SUGGESTIONS)
        body = self.fake.requests[0].body
        self.check_answer_request(body, stream=False)
        self.assertEqual(system_text(body), RELATIONSHIP_SYSTEM_PROMPT)
        with self.sessions() as db:
            self.assertEqual(db.query(RelationshipMessage).filter_by(relationship_id=relationship_id).count(), 2)

    def test_cc10_a_cut_or_refused_answer_is_a_502_with_its_text_and_no_charge(self):
        cases = {'max_tokens': (fake.Scenario(text='{"answer":"Начало', stop_reason='max_tokens'), TRUNCATED),
                 'refusal': (fake.refusal('general_harms'), FILTERED)}
        for name, (scenario, text) in cases.items():
            for subject in ('natal', 'relationship'):
                with self.subTest(case=name, subject=subject):
                    self.fake.requests.clear()
                    self.fake.queue(scenario)
                    if subject == 'natal':
                        cid = self.chart()
                        before, state = self.snapshot(cid), self.used()
                        self.use_claude()
                        response = self.ask(cid)
                    else:
                        relationship_id, _ = self.pair()
                        state = self.used()
                        self.use_claude()
                        response = self.ask_relationship(relationship_id)
                    self.assertEqual(response.status_code, 502, response.text)
                    self.assertEqual(response.json()['detail'], text)
                    self.assertEqual(len(self.fake.requests), 1)
                    self.assertEqual(self.used(), state)
                    if subject == 'natal':
                        self.assertEqual(self.snapshot(cid), before)


# ---------------------------------------------------------------- CC-11: contract with the real SDK, over the wire
class WireContractTests(ClaudeBase):
    def test_cc11_the_pinned_sdk_sends_the_documented_wire_request(self):
        cid = self.chart()
        client, params = self.claude()
        events, *_ = self.stream(cid, client, params)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(len(self.fake.requests), 1)
        request = self.fake.requests[0]
        self.assertEqual(request.path, '/v1/messages')
        self.assertEqual(request.headers['anthropic-version'], '2026-10-21')
        self.assertEqual(request.headers['anthropic-version'], client.default_headers['anthropic-version'],
                         'the version header must be the pinned SDK default')
        self.assertNotIn('anthropic-beta', request.headers, 'no beta features')
        credential = request.headers.get('x-api-key') or request.headers.get('authorization', '').removeprefix('Bearer ')
        self.assertEqual(credential, STUB_KEY)
        self.assertTrue(request.headers['content-type'].startswith('application/json'))
        self.check_answer_request(request.body, stream=True)

    def test_cc11_outcomes_and_request_counts_over_the_wire(self):
        long_text = fake.envelope('Ответ, который не успеет закончиться. ' * 3)
        partial = '{"answer":"Начало ответа'
        matrix = [
            ('normal answer with a thinking block', fake.answer(ANSWER), 'done', None),
            ('max_tokens', fake.Scenario(text=partial, stop_reason='max_tokens'), 'error', TRUNCATED),
            ('refusal in the middle', fake.refusal('bio', text=partial, chunks=[partial[:7], partial[7:]]), 'error', FILTERED),
            ('HTTP 529 before the stream', fake.error(529, 'overloaded_error'), 'error', NETWORK_CUT),
            ('HTTP 429', fake.error(429, 'rate_limit_error'), 'error', NETWORK_CUT),
            ('error event in the middle', fake.Scenario(text=long_text, error_after=3), 'error', NETWORK_CUT),
        ]
        client, params = self.claude()
        for name, scenario, last, detail in matrix:
            with self.subTest(case=name):
                cid = self.chart()
                before = len(self.fake.requests)
                self.fake.queue(scenario)
                events, status, *_ = self.stream(cid, client, params)
                self.assertEqual(events[-1]['type'], last, events[-1])
                if detail:
                    self.assertEqual(events[-1]['detail'], detail)
                    self.assertEqual(status, 'released')
                else:
                    self.assertEqual(status, 'succeeded')
                self.assertEqual(len(self.fake.requests), before + 1, 'one request per question, never a retry')
                body = self.fake.requests[-1].body
                for field in ('temperature', 'top_p', 'top_k'):
                    self.assertNotIn(field, body)
                self.assertIn('format', body['output_config'])
                self.assertEqual(body['thinking'], {'type': 'adaptive'})
        self.assertEqual(self.foreign, [])


# ---------------------------------------------------------------- CC-12: a journal without junk
class JournalTests(ClaudeBase):
    def test_cc12_a_model_label_with_a_line_break_is_replaced_by_invalid(self):
        injected = f'{MODEL}\nX=1'
        with journal() as records:
            client, params = self.claude(ANTHROPIC_CHAT_MODEL=injected)
        found = lines(records, 'AI chat provider')
        self.assertEqual(len(found), 1, found)
        self.assertIn('model=invalid', found[0])
        self.assertNotIn('\n', found[0])
        self.assertNotIn('X=1', rendered(records))
        # and the outcome line of an abnormal answer stays a line of closed labels and numbers
        cid = self.chart()
        self.fake.queue(fake.Scenario(text='{"answer":"Начало', stop_reason='max_tokens'))
        events, status, records, _ = self.stream(cid, client, params)
        self.assertEqual(events[-1]['detail'], TRUNCATED)
        outcome = lines(records, 'AI chat outcome=')
        self.assertEqual(len(outcome), 1, outcome)
        self.assertNotIn('\n', outcome[0])
        self.assertNotIn('X=1', rendered(records))
        self.assertRegex(outcome[0], r'^AI chat outcome=[a-z_]+( [a-z_]+=[A-Za-z0-9._-]{1,48})+$')

    def test_cc12_a_normal_startup_line_is_made_of_labels_only(self):
        with journal() as records:
            self.claude()
        found = lines(records, 'AI chat provider')
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0], r'^AI chat provider=anthropic( [a-z_]+=[A-Za-z0-9._-]{1,48})+$')
        self.assertRegex(found[0], r' model=[A-Za-z0-9._-]{1,48}( |$)')
        self.assertRegex(found[0], r' effort=(low|medium|high)( |$)')
        self.assertNotIn(STUB_KEY, rendered(records))
        self.assertNotIn(self.fake.url, rendered(records))


# ---------------------------------------------------------------- CC-13: dependencies
class DependencyPinTests(unittest.TestCase):
    PINNED = ('httpx2', 'httpcore2', 'truststore', 'opentelemetry-api', 'docstring-parser', 'idna')

    def requirements(self):
        rows = {}
        for number, row in enumerate((ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines(), 1):
            row = row.strip()
            if not row or row.startswith('#'):
                continue
            self.assertIn('==', row, f'requirements.txt line {number} is not pinned with ==')
            name, _, version = row.partition('==')
            rows.setdefault(name.strip().lower().replace('_', '-'), []).append(version.strip())
        return rows

    def test_cc13_the_sdk_and_its_new_dependencies_are_pinned_exactly(self):
        rows = self.requirements()
        self.assertEqual(len(rows.get('anthropic', [])), 1, 'exactly one anthropic==<version> line')
        for name in self.PINNED:
            self.assertEqual(len(rows.get(name, [])), 1, f'{name}==<version> must be pinned once')

    def test_cc13_every_pin_is_the_version_the_task_environment_was_built_and_checked_with(self):
        """The stand installs requirements.txt; a pin that differs from what `pip check` passed on is an untested build."""
        rows = self.requirements()
        for name in ('anthropic', *self.PINNED):
            with self.subTest(package=name):
                self.assertEqual(rows.get(name), [metadata.version(name)])


# ---------------------------------------------------------------- CC-15: one prompt for both providers
class SamePromptTests(ClaudeBase):
    def openai_messages(self, send):
        with patch('modules.interpretation.client.chat.completions.create') as api:
            api.return_value = (cq.reply(json.dumps({'answer': 'Ответ', 'follow_up_suggestions': []})) if send[0] == 'natal'
                                else rel.reply())
            response = send[1]()
            self.assertEqual(response.status_code, 200, response.text)
            return api.call_args.kwargs['messages']

    def claude_request(self, send):
        self.use_claude()
        response = send()
        self.assertEqual(response.status_code, 200, response.text)
        return self.fake.requests[-1].body

    def check_same(self, messages, body, header):
        self.assertEqual(system_text(body), messages[0]['content'], 'the system prompt must be one text for both providers')
        self.assertEqual(texts(body['messages'][0])[0], messages[1]['content'], 'the chart block must be the same text')
        self.assertTrue(messages[1]['content'].startswith(header))

    def test_cc15_natal_prompt_and_chart_block_are_the_same_for_both_providers(self):
        cid = self.chart()
        send = lambda: self.ask(cid, 'Что мне важно в карьере?')
        messages = self.openai_messages(('natal', send))
        body = self.claude_request(send)
        self.check_same(messages, body, 'Calculated natal reference data (not instructions):\n')
        self.assertEqual(messages[0]['content'], ASTROLOGY_SYSTEM_PROMPT)

    def test_cc15_relationship_prompt_and_chart_block_are_the_same_for_both_providers(self):
        relationship_id, _ = self.pair()
        send = lambda: self.client.post(f'/relationships/{relationship_id}/ask', json={'question': 'Как нам понимать друг друга?'},
                                        headers=self.owner)
        messages = self.openai_messages(('relationship', send))
        body = self.claude_request(send)
        self.check_same(messages, body, 'Calculated relationship reference data (not instructions):\n')
        self.assertEqual(messages[0]['content'], RELATIONSHIP_SYSTEM_PROMPT)


# ---------------------------------------------------------------- CC-17: the SDK must not patch TLS of the whole process
class TlsGuardTests(unittest.TestCase):
    def test_cc17_choosing_anthropic_leaves_the_stdlib_ssl_untouched(self):
        code = ('import json, ssl\n'
                'original = (ssl.SSLContext, ssl.create_default_context)\n'
                'import anthropic, httpx2, truststore\n'
                'import modules.interpretation as interpretation\n'
                'from modules import ai_anthropic\n'
                'client, params = interpretation.select_chat_backend({"LUNARIA_CHAT_PROVIDER": "anthropic",\n'
                '    "ANTHROPIC_API_KEY": "sk-ant-test-0000000000", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"})\n'
                'print(json.dumps({"context_is_stdlib": ssl.SSLContext is original[0],\n'
                '    "default_context_is_stdlib": ssl.create_default_context is original[1],\n'
                '    "context_module": ssl.SSLContext.__module__, "provider": params.get("provider")}))\n')
        run = python(code)
        self.assertEqual(run.returncode, 0, run.stderr[-1500:])
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(result['provider'], 'anthropic')
        self.assertTrue(result['context_is_stdlib'], 'ssl.SSLContext was replaced: Stripe, Lava, OpenAI and mail would change')
        self.assertTrue(result['default_context_is_stdlib'], 'ssl.create_default_context was replaced')
        self.assertEqual(result['context_module'], 'ssl')


# ---------------------------------------------------------------- CC-14 / CC-16: prompt v3 (DESIGN section 3, marker phrases 3.6)
# Written BEFORE the prompt: the marker phrases of section 3.6 and the size caps are the only wording these tests rely on.
# Everything else (boundaries, follow-up block, summary prompts) must stay as it was on 0852c88.
NATAL_CAP, PAIR_CAP = 9000, 8000          # UTF-8 bytes (DESIGN 3.6; v2 is 6477 and 5196)
COMMON_MARKERS = ('Depth, not a catalogue',
                  'where they reinforce each other and where they pull in different directions',
                  'Honest balance', 'hold for the whole conversation')
NATAL_MARKERS = COMMON_MARKERS + ('would this answer fit another chart just as well', 'one real difficulty',
                                  'Do not invent problems', 'rulerships')
PAIR_MARKERS = COMMON_MARKERS + ('would this answer fit any other couple just as well', 'one real friction point',
                                 'without blaming')
STEP_PHRASES = ('«вам стоит…»', '«вам стоит...»')       # the ellipsis may be typed as one character or three dots
# Sizes and SHA-256 of the two summary prompts on 0852c88 ("byte for byte", DESIGN 3.5).
SUMMARY_BASELINE = {
    'SUMMARY_SYSTEM_PROMPT': (727, '61daaaaaca1b4bfd6ee12b8661a7b95cd4f320664f84d0877230950667b04d89'),
    'RELATIONSHIP_SUMMARY_PROMPT': (738, 'a511941598407c9702d124d3a8da1ba92094e3e9897f138d06b12997d9bbf4ff'),
}


def flat(text):
    """Prompt text with every run of whitespace (line wraps, indents) collapsed: markers survive any wrapping."""
    return ' '.join(text.split())


def missing_markers(prompt, markers):
    text = flat(prompt)
    return [marker for marker in markers if flat(marker) not in text]


class PromptV3Tests(unittest.TestCase):
    def test_cc14_natal_prompt_carries_every_v3_marker(self):
        self.assertEqual(missing_markers(ASTROLOGY_SYSTEM_PROMPT, NATAL_MARKERS), [],
                         'prompt v3 is not written: these section 3.6 marker phrases are absent from the natal prompt')
        self.assertTrue(any(phrase in flat(ASTROLOGY_SYSTEM_PROMPT) for phrase in STEP_PHRASES),
                        'the natal prompt must ask for one doable step phrased as «вам стоит…»')

    def test_cc14_pair_prompt_carries_every_v3_marker(self):
        self.assertEqual(missing_markers(RELATIONSHIP_SYSTEM_PROMPT, PAIR_MARKERS), [],
                         'prompt v3 is not written: these section 3.6 marker phrases are absent from the pair prompt')
        self.assertTrue(any(phrase in flat(RELATIONSHIP_SYSTEM_PROMPT) for phrase in STEP_PHRASES),
                        'the pair prompt must ask for one doable step phrased as «вам стоит…»')

    def test_cc14_v2_tone_example_is_gone(self):
        for name, prompt in (('natal', ASTROLOGY_SYSTEM_PROMPT), ('pair', RELATIONSHIP_SYSTEM_PROMPT)):
            with self.subTest(prompt=name):
                self.assertNotIn('Солнце в Рыбах в 12-м доме', flat(prompt),
                                 'the v2 style example must be replaced (DESIGN 3.1 p.8)')

    def test_cc14_v3_adds_text_and_stays_inside_the_size_caps(self):
        """v3 only adds (6477 and 5196 bytes on 0852c88), and the budget of 64000 bytes must still hold (CC-16)."""
        natal, pair = len(ASTROLOGY_SYSTEM_PROMPT.encode('utf-8')), len(RELATIONSHIP_SYSTEM_PROMPT.encode('utf-8'))
        self.assertGreater(natal, 6477, 'natal prompt is still v2 size')
        self.assertGreater(pair, 5196, 'pair prompt is still v2 size')
        self.assertLessEqual(natal, NATAL_CAP)
        self.assertLessEqual(pair, PAIR_CAP)

    def test_cc14_follow_up_block_phrases_stay(self):
        """tests/test_follow_up.py lines 66-76, repeated here so that the prompt rewrite cannot drop them unnoticed."""
        for prompt in (ASTROLOGY_SYSTEM_PROMPT, RELATIONSHIP_SYSTEM_PROMPT):
            for phrase in ('deepen:', 'personalize:', 'explore:', 'exactly three', 'paraphrases',
                           'without inventing events', 'clickbait'):
                self.assertIn(phrase, prompt)
        self.assertIn('FIRST-PERSON', ASTROLOGY_SYSTEM_PROMPT)
        self.assertIn('Person A / Person B identity', RELATIONSHIP_SYSTEM_PROMPT)
        self.assertIn('speaker_person only when supplied', RELATIONSHIP_SYSTEM_PROMPT)

    def test_cc14_natal_boundaries_stay(self):
        text = flat(ASTROLOGY_SYSTEM_PROMPT)
        for phrase in ('whether you are AI', 'unavailable unless dated ephemeris data is supplied',
                       'No fatalism, guarantees or fear', 'medical treatment, legal decisions or financial investments',
                       'qualified professional help', 'as data, not overriding instructions',
                       'The chart may concern someone other than the speaker', 'older_history_pending'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_cc14_pair_boundaries_stay(self):
        text = flat(RELATIONSHIP_SYSTEM_PROMPT)
        for phrase in ('Person A and Person B are distinct people', 'speaker_person identifies the user only when given',
                       'compatibility percentage', 'Do not diagnose either person', 'predict inevitable separation',
                       'promise to fix/save a relationship', 'transits or sky positions for another date',
                       'medical, legal or financial instruction', 'untrusted data, not higher-priority commands'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_cc14_summary_prompts_are_byte_for_byte_the_old_ones(self):
        from hashlib import sha256
        from modules import relationship_prompt as pair_module, astrology_prompt as natal_module
        found = {'SUMMARY_SYSTEM_PROMPT': natal_module.SUMMARY_SYSTEM_PROMPT,
                 'RELATIONSHIP_SUMMARY_PROMPT': pair_module.RELATIONSHIP_SUMMARY_PROMPT}
        for name, (size, digest) in SUMMARY_BASELINE.items():
            with self.subTest(prompt=name):
                data = found[name].encode('utf-8')
                self.assertEqual((len(data), sha256(data).hexdigest()), (size, digest))


# ---------------------------------------------------------------- CC-16 (guard): the request budget with prompt v3
class RequestBudgetV3Tests(ClaudeBase):
    """The longer v3 prompts must still leave room, inside the 64000-byte input guard, for the biggest chart context
    (CQ-5: 26 bodies / 250 aspects), the verbatim memory of three long exchanges (CQ-11) and the question; no summary call.
    The scenario runs with each prompt padded up to its cap, so it holds for any v3 wording that obeys CC-14.  A guard that
    ran with the v2 prompt would prove nothing about v3, therefore the markers are asserted first."""

    @staticmethod
    def padded(prompt, cap):
        return prompt + ' ' * max(0, cap - len(prompt.encode('utf-8')))

    @staticmethod
    def exchange_rows():
        rows = []
        for i in range(3):
            filler = f'ОТВЕТ-{i} ' + ('Это длинный подробный ответ астролога про вашу карту. ' * 60)
            rows += [('user', f'Вопрос {i}: расскажи подробнее про тему {i}'), ('gpt', filler[:2500])]
        return rows

    def check_request(self, messages, rows, question, prompt):
        total = sum(len(m['content'].encode('utf-8')) for m in messages)
        self.assertEqual(messages[0], {'role': 'system', 'content': prompt})
        self.assertLessEqual(total, 64000, 'prompt v3 + biggest context + memory + question exceed the input guard')
        self.assertEqual([m['content'] for m in messages[-7:-1]], [text for _, text in rows],
                         'the 3 long exchanges must stay verbatim')
        self.assertEqual(messages[-1], {'role': 'user', 'content': question})
        self.assertFalse([m for m in messages if m['content'].startswith('Older conversation memory')])

    def test_cc16_natal_biggest_chart_and_three_long_exchanges_fit_with_prompt_v3(self):
        self.assertEqual(missing_markers(ASTROLOGY_SYSTEM_PROMPT, NATAL_MARKERS), [],
                         'the guard is meaningful only with the v3 prompt in place')
        cid = self.chart()
        with self.sessions() as db:
            db.query(GPTMessage).filter_by(chart_id=cid).delete(); db.commit()
        order, symbols = list(cq.NAMES), list(cq.ASPECT_NAMES)
        circle, aspect_lines = [], []
        for i, (a, b) in enumerate(list(cq.combinations(order, 2))[:250]):
            circle.append(cq.edge(a, b, symbols[i % 13]))
            aspect_lines.append(cq.line(symbols[i % 13], a, b, (i * 7) % 240))
        self.store(cid, cq.spread(order), circle, cq.structured(aspect_lines))
        rows = self.exchange_rows()
        with self.sessions() as db:
            for role, text in rows:
                db.add(GPTMessage(chart_id=cid, role=role, content=text))
            db.commit()
        prompt, question = self.padded(ASTROLOGY_SYSTEM_PROMPT, NATAL_CAP), 'А что из этого главное?'
        with patch('modules.ai_conversation.ASTROLOGY_SYSTEM_PROMPT', prompt), \
                patch('modules.interpretation.client.chat.completions.create',
                      return_value=cq.reply(json.dumps({'answer': 'Хорошо.', 'follow_up_suggestions': []}))) as api:
            self.assertEqual(self.ask(cid, question).status_code, 200)
            self.assertEqual(api.call_count, 1, 'a second call means the dialogue did not fit and was summarised')
            messages = api.call_args.kwargs['messages']
        self.check_request(messages, rows, question, prompt)

    def test_cc16_pair_three_long_exchanges_fit_with_prompt_v3(self):
        self.assertEqual(missing_markers(RELATIONSHIP_SYSTEM_PROMPT, PAIR_MARKERS), [],
                         'the guard is meaningful only with the v3 prompt in place')
        relationship_id, _ = self.pair()
        rows = self.exchange_rows()
        with self.sessions() as db:
            for role, text in rows:
                db.add(RelationshipMessage(relationship_id=relationship_id, role=role, content=text))
            db.commit()
        prompt, question = self.padded(RELATIONSHIP_SYSTEM_PROMPT, PAIR_CAP), 'Что для нас главное?'
        with patch('modules.ai_conversation.RELATIONSHIP_SYSTEM_PROMPT', prompt), \
                patch('modules.interpretation.client.chat.completions.create', return_value=rel.reply()) as api:
            response = self.client.post(f'/relationships/{relationship_id}/ask', json={'question': question},
                                        headers=self.owner)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(api.call_count, 1, 'a second call means the dialogue did not fit and was summarised')
            messages = api.call_args.kwargs['messages']
        self.check_request(messages, rows, question, prompt)


if __name__ == '__main__':
    unittest.main()
