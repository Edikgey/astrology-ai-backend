"""TOPICS-1-be regression TB1-TB4 (tasks/TOPICS-1/DESIGN.md sections 2.1-2.6, 4, 5).

Written before the code. New names (TOPIC_QUESTIONS, topic_of, TOPIC_LEGACY_CHART, TOPIC_PROMPT, TOPIC_FOCUS) are
imported inside the tests, so on the base every test about the new feature fails for its own reason and not on module
import. Guards that are green on the base: the neighbours of the topic text (ordinary question, month, pair chat,
ownership), the pinned base prompts, the quota release after a model failure, the second request to the same chart.
Style and helpers: tests/test_my_month.py. Clock: modules.transits.server_now frozen (TransitContextBase). Synthetic
data only, no network, no .secrets; OpenAI is a stub.
"""
import hashlib
import json
import re
import unittest
from unittest.mock import patch

import test_chat_quality as cq
import test_relationship_ai as relationships
from test_ai_stream import ProviderStream
from test_my_month import (ASTROLOGY_PROMPT_SHA256, MONTH_QUESTION, MonthBase, ORDINARY_CONTEXT_SHA256,
                           RELATIONSHIP_PROMPT_SHA256, SUMMARY_PROMPT_SHA256, sse_events)
from test_transits_context import captured_logs
from database.queries import GPTUsage
from modules import usage
from modules.astrology_prompt import ASTROLOGY_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from modules.relationship_prompt import RELATIONSHIP_SYSTEM_PROMPT

# DESIGN 2.1 and 2.2 literals.
CAREER_QUESTION = 'Призвание и работа: разбор моей карты'
MONEY_QUESTION = 'Деньги: разбор моей карты'
LOVE_QUESTION = 'Любовь: разбор моей карты'
TOPICS = {'career': CAREER_QUESTION, 'money': MONEY_QUESTION, 'love': LOVE_QUESTION}
TOPIC_LEGACY_CHART = (
    'Разбор по темам строится по домам карты, а в этой карте они могут быть неточными: она построена в старой '
    'версии Lunaria, где часовой пояс рождения не проверялся. Постройте карту заново, выбрав место рождения из '
    'подсказок, и откройте разбор для новой карты.')

# DESIGN 5 TB1: not a topic (exact text only, after strip()).
NOT_TOPIC = (
    'Деньги', 'Деньги: разбор моей карты?', 'Деньги: разбор моей карты, пожалуйста', 'деньги: разбор моей карты',
    'Расскажи про деньги в моей карте', 'Что мне подходит в работе?', 'Как у меня с любовью?',
    'Любовь: разбор моей карты. А ещё про работу', 'Работа: разбор моей карты', 'Здоровье: разбор моей карты',
    '', '   ', MONTH_QUESTION)

# DESIGN 2.4: pinned fragments (one line each) and the four bold labels of every topic (12 in all).
PROMPT_FRAGMENTS = ('Do not use transits',
                    'House rulers, dispositors, dignities and element counts are not in the data',
                    'Never advise on investments', 'never forecast income', 'never tell the person to quit',
                    'Never judge a specific person')
FRAGMENT_OWNER = {'Never advise on investments': 'money', 'never forecast income': 'money',
                  'never tell the person to quit': 'career', 'Never judge a specific person': 'love'}
LABELS = {
    'career': ('**Что вам подходит**', '**Ваши сильные стороны**', '**Как вам лучше работать**', '**Что может мешать**'),
    'money': ('**Ваше отношение к деньгам**', '**Как вы зарабатываете**', '**Как вы тратите и бережёте**',
              '**На что обратить внимание**'),
    'love': ('**Что для вас важно в любви**', '**Как вы проявляете чувства**', '**Каким вы видите партнёра**',
             '**Что может мешать**'),
}
# DESIGN 2.6: the journal line of an abnormal topic outcome.
TOPIC_LINE = re.compile(r'^AI chat outcome=\w+ subject=topic_(career|money|love) model=\S+ output_tokens=(\d+|none) '
                        r'duration_ms=\d+$')
OUTCOME_TEXT = 'Разбор по теме'


def flat(text):
    return ' '.join(text.split())


def outcome_lines(lines):
    return [line for line in lines if line.startswith('AI chat outcome=')]


class TopicBase(MonthBase):
    """A verified or legacy chart with a premium owner; the transit search is replaced where the model is called."""

    def answer(self, cid, question, sse=False, reply=None):
        """One ask with the stub model and the synthetic transit rows. Returns (response, provider, text)."""
        reply = reply or (ProviderStream(payload={'answer': OUTCOME_TEXT, 'follow_up_suggestions': None}) if sse else
                          relationships.reply(answer=OUTCOME_TEXT, suggestions=None))
        with self.patch_find(return_value=self.synthetic_window()):
            response, provider = self.ask(cid, question, sse, reply=reply)
        return response, provider

    def history(self, cid):
        response = self.client.get(f'/gpt-messages?chart_id={cid}', headers=self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


# --------------------------------------------------------------------------------------------- TB1: recognition

class RecognitionTests(unittest.TestCase):
    def test_tb1_topic_questions_are_the_design_literals(self):
        from modules.ai_transit_guard import TOPIC_QUESTIONS
        self.assertEqual(TOPIC_QUESTIONS, TOPICS)
        self.assertEqual(list(TOPIC_QUESTIONS), ['career', 'money', 'love'])

    def test_tb1_the_legacy_text_is_the_design_literal(self):
        from modules import ai_transit_guard
        self.assertEqual(ai_transit_guard.TOPIC_LEGACY_CHART, TOPIC_LEGACY_CHART)

    def test_tb1_the_button_texts_give_their_keys_even_with_spaces_around(self):
        from modules.ai_transit_guard import topic_of
        for key, text in TOPICS.items():
            for variant in (text, f'  {text}', f'{text}  ', f'\n{text}\t'):
                with self.subTest(key=key, variant=variant):
                    self.assertEqual(topic_of(variant), key)

    def test_tb1_any_other_text_is_an_ordinary_question(self):
        from modules.ai_transit_guard import topic_of
        self.assertEqual(len(NOT_TOPIC), 13)
        for question in NOT_TOPIC:
            with self.subTest(question=question):
                self.assertIsNone(topic_of(question))

    def test_tb1_the_button_texts_stay_outside_the_transit_and_month_guards(self):
        from modules.ai_transit_guard import TOPIC_QUESTIONS, is_month_request, requires_transit_data, topic_of
        self.assertEqual(len(TOPIC_QUESTIONS), 3)
        for key, text in TOPIC_QUESTIONS.items():
            with self.subTest(key=key):
                self.assertEqual(topic_of(text), key)
                self.assertFalse(requires_transit_data(text))
                self.assertFalse(is_month_request(text))

    def test_tb1_the_month_button_is_not_a_topic(self):
        """Green neighbour on the base for the month side; red for the topic side through the import."""
        from modules.ai_transit_guard import is_month_request, topic_of
        self.assertTrue(is_month_request(MONTH_QUESTION))
        self.assertIsNone(topic_of(MONTH_QUESTION))


# --------------------------------------------------------------------------------------------- TB2: legacy chart

class LegacyChartTests(TopicBase):
    def assert_topic_refused(self, cid, text, sse):
        used = self.used()                     # first: the usage endpoint backfills the user's history rows once
        before = self.counts()
        with patch('modules.interpretation.client.chat.completions.create',
                   return_value=ProviderStream() if sse else relationships.reply(answer=OUTCOME_TEXT)) as provider:
            response = self.post('/ask-gpt', {'chart_id': cid, 'question': text}, sse)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['detail'], TOPIC_LEGACY_CHART)
        provider.assert_not_called()                               # the model is not called
        self.assertEqual(self.counts(), before)                    # no usage row, no history
        self.assertEqual((self.used(), self.reserved_rows()), (used, 0))

    def test_tb2_legacy_chart_topic_text_is_refused_in_json_and_sse_before_model_and_quota(self):
        cid = self.legacy()
        for key, text in TOPICS.items():
            for sse in (False, True):
                with self.subTest(topic=key, sse=sse):
                    self.assert_topic_refused(cid, text, sse)

    def test_tb2_the_refusal_also_holds_with_surrounding_spaces(self):
        cid = self.legacy()
        for sse in (False, True):
            with self.subTest(sse=sse):
                self.assert_topic_refused(cid, f'  {MONEY_QUESTION}  ', sse)

    def test_tb2_an_ordinary_question_on_the_same_legacy_chart_is_answered(self):
        """Green guard: only the topic text is refused, not the neighbours (here the chart is the same legacy one)."""
        for question in ('Что мне подходит в работе?', 'Деньги', 'Как у меня с любовью?'):
            for sse in (False, True):
                cid = self.legacy()
                reply = ProviderStream() if sse else relationships.reply(answer='Обычный ответ')
                with self.subTest(question=question, sse=sse), patch(
                        'modules.interpretation.client.chat.completions.create', return_value=reply) as provider:
                    response = self.post('/ask-gpt', {'chart_id': cid, 'question': question}, sse)
                    self.assertEqual(response.status_code, 200, response.text)
                    provider.assert_called_once()

    def test_tb2_someone_elses_chart_gets_the_ownership_error_before_the_legacy_text(self):
        cid = self.legacy()
        for sse in (False, True):
            with self.subTest(sse=sse), patch('modules.interpretation.client.chat.completions.create') as provider:
                headers = {**self.other, **({'Accept': 'text/event-stream'} if sse else {})}
                response = self.client.post('/ask-gpt', json={'chart_id': cid, 'question': CAREER_QUESTION},
                                            headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotEqual(response.json()['detail'], TOPIC_LEGACY_CHART)
                provider.assert_not_called()

    def test_tb2_a_missing_chart_is_404_before_the_legacy_text(self):
        with patch('modules.interpretation.client.chat.completions.create') as provider:
            response = self.client.post('/ask-gpt', json={'chart_id': 987654, 'question': LOVE_QUESTION},
                                        headers=self.owner)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotEqual(response.json()['detail'], TOPIC_LEGACY_CHART)
        provider.assert_not_called()

    def test_tb2_a_verified_chart_gets_the_reading(self):
        """The refusal is for legacy charts only: the same text on a verified chart reaches the model once."""
        from modules.astrology_prompt import TOPIC_PROMPT
        for sse in (False, True):
            cid = self.verified()
            with self.subTest(sse=sse):
                response, provider = self.answer(cid, MONEY_QUESTION, sse)
                self.assertEqual(response.status_code, 200, response.text)
                provider.assert_called_once()
                self.assertIn(TOPIC_PROMPT, provider.call_args.kwargs['messages'][0]['content'])

    def test_tb2_the_pair_chat_does_not_know_topics(self):
        """Green guard: in the pair chat the topic text is an ordinary question (no refusal, pair prompt)."""
        relationship_id, _ = relationships.RelationshipAITests.pair(self)
        for sse in (False, True):
            reply = ProviderStream() if sse else relationships.reply(answer='Ответ пары')
            with self.subTest(sse=sse), patch('modules.interpretation.client.chat.completions.create',
                                              return_value=reply) as provider:
                response = self.post(f'/relationships/{relationship_id}/ask', {'question': LOVE_QUESTION}, sse)
                self.assertEqual(response.status_code, 200, response.text)
                provider.assert_called_once()
                self.assertEqual(provider.call_args.kwargs['messages'][0],
                                 {'role': 'system', 'content': RELATIONSHIP_SYSTEM_PROMPT})


# --------------------------------------------------------------------------------------------- TB3: prompt

class PromptTests(TopicBase):
    def test_tb3_the_prompt_has_the_pinned_fragments_in_the_right_places(self):
        from modules.astrology_prompt import TOPIC_FOCUS, TOPIC_PROMPT
        self.assertIsInstance(TOPIC_PROMPT, str)
        self.assertEqual(set(TOPIC_FOCUS), set(TOPICS))
        whole = {key: flat(TOPIC_PROMPT + '\n' + TOPIC_FOCUS[key]) for key in TOPICS}
        for fragment in PROMPT_FRAGMENTS:
            for key in TOPICS:
                present = fragment in whole[key]
                owner = FRAGMENT_OWNER.get(fragment)
                with self.subTest(fragment=fragment, topic=key):
                    self.assertEqual(present, owner is None or owner == key)

    def test_tb3_every_topic_has_its_four_bold_labels_in_order(self):
        from modules.astrology_prompt import TOPIC_FOCUS
        self.assertEqual(sum(len(labels) for labels in LABELS.values()), 12)
        for key, labels in LABELS.items():
            text = flat(TOPIC_FOCUS[key])
            positions = [text.find(label) for label in labels]
            with self.subTest(topic=key):
                self.assertTrue(all(position >= 0 for position in positions), positions)
                self.assertEqual(positions, sorted(positions))

    def test_tb3_the_model_gets_base_plus_topic_prompt_and_the_same_call_parameters(self):
        from modules.astrology_prompt import TOPIC_FOCUS, TOPIC_PROMPT
        for key, text in TOPICS.items():
            for sse in (False, True):
                with self.subTest(topic=key, sse=sse), self.patch_find(return_value=self.synthetic_window()):
                    ordinary_chart, topic_chart = self.verified(), self.verified()
                    response, ordinary = self.ask(ordinary_chart, 'Что меня ждёт в работе?', sse)
                    self.assertEqual(response.status_code, 200, response.text)
                    response, topic = self.ask(topic_chart, text, sse)
                    self.assertEqual(response.status_code, 200, response.text)
                    ordinary_call, topic_call = ordinary.call_args.kwargs, topic.call_args.kwargs
                    topic.assert_called_once()
                    self.assertEqual(topic_call['messages'][0], {
                        'role': 'system',
                        'content': ASTROLOGY_SYSTEM_PROMPT + '\n' + TOPIC_PROMPT + '\n' + TOPIC_FOCUS[key]})
                    rest = lambda kwargs: {k: v for k, v in kwargs.items() if k != 'messages'}
                    self.assertEqual(rest(topic_call), rest(ordinary_call))
                    self.assertEqual(topic_call['messages'][1]['role'], 'user')
                    self.assertEqual(len(topic_call['messages']), len(ordinary_call['messages']))

    def test_tb3_the_context_is_byte_for_byte_the_ordinary_one(self):
        """The topic gets the same chart context as an ordinary question, pinned by the base SHA of test_my_month."""
        from modules.astrology_prompt import TOPIC_PROMPT
        for key, text in TOPICS.items():
            with self.subTest(topic=key), self.patch_find(return_value=self.synthetic_window()):
                cid = self.verified()
                with self.sessions() as db:
                    from modules.ai_chart_context import build_chart_context
                    raw = build_chart_context(db, 1, cid)
                response, provider = self.ask(cid, text)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(TOPIC_PROMPT, provider.call_args.kwargs['messages'][0]['content'])
                sent = provider.call_args.kwargs['messages'][1]['content']
                self.assertEqual(sent.split('\n', 1)[1], raw)
                self.assertNotIn('month', json.loads(raw)['transits'])
                if key == 'career':
                    self.assertEqual(hashlib.sha256(raw.encode('utf-8')).hexdigest(), ORDINARY_CONTEXT_SHA256)

    def test_tb3_the_ordinary_question_and_the_month_keep_their_prompts(self):
        """Green guard: the topic branch changes neither the ordinary question nor «Мой месяц» (DECISIONS 21, 22)."""
        from modules.astrology_prompt import MY_MONTH_PROMPT
        for sse in (False, True):
            with self.subTest(sse=sse), self.patch_find(return_value=self.synthetic_window()):
                for question, expected in (
                        ('Что меня ждёт в работе?', ASTROLOGY_SYSTEM_PROMPT),
                        ('Деньги', ASTROLOGY_SYSTEM_PROMPT),
                        (MONTH_QUESTION, ASTROLOGY_SYSTEM_PROMPT + '\n' + MY_MONTH_PROMPT)):
                    response, provider = self.ask(self.verified(), question, sse)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(provider.call_args.kwargs['messages'][0], {'role': 'system', 'content': expected},
                                     question)

    def test_tb3_the_base_prompts_are_unchanged(self):
        """Green guard: the base prompts are pinned byte for byte (DESIGN 2.4)."""
        sha = lambda text: hashlib.sha256(text.encode('utf-8')).hexdigest()
        self.assertEqual(sha(ASTROLOGY_SYSTEM_PROMPT), ASTROLOGY_PROMPT_SHA256)
        self.assertEqual(sha(SUMMARY_SYSTEM_PROMPT), SUMMARY_PROMPT_SHA256)
        self.assertEqual(sha(RELATIONSHIP_SYSTEM_PROMPT), RELATIONSHIP_PROMPT_SHA256)


# --------------------------------------------------------------------------------------------- TB4: history and failure

class HistoryAndFailureTests(TopicBase):
    def reading(self, cid, key, sse):
        from modules.astrology_prompt import TOPIC_PROMPT
        response, provider = self.answer(cid, TOPICS[key], sse)
        self.assertEqual(response.status_code, 200, response.text)
        provider.assert_called_once()
        self.assertIn(TOPIC_PROMPT, provider.call_args.kwargs['messages'][0]['content'])
        if sse:
            events = sse_events(response.text)
            self.assertEqual(events[-1]['type'], 'done', response.text[-300:])
            return events[-1]['response'], provider
        return response.json()['response'], provider

    def test_tb4_a_good_reading_is_saved_as_a_pair_and_costs_one_question(self):
        from modules.astrology_prompt import TOPIC_PROMPT
        for key in TOPICS:
            for sse in (False, True):
                cid = self.verified()
                with self.subTest(topic=key, sse=sse):
                    before_history, before_used = len(self.history(cid)), self.used()
                    text, provider = self.reading(cid, key, sse)
                    self.assertEqual(text, OUTCOME_TEXT)
                    self.assertIn(TOPIC_PROMPT, provider.call_args.kwargs['messages'][0]['content'])
                    history = self.history(cid)
                    self.assertEqual(len(history), before_history + 2)
                    self.assertEqual([(row['role'], row['content']) for row in history[-2:]],
                                     [('user', TOPICS[key]), ('gpt', OUTCOME_TEXT)])
                    self.assertEqual(self.used(), (before_used[0] + 1, 0))

    def test_tb4_reading_the_pair_again_asks_no_model_and_costs_nothing(self):
        cid = self.verified()
        self.reading(cid, 'money', False)
        used = self.used()
        with patch('modules.interpretation.client.chat.completions.create') as provider:
            history = self.history(cid)
            self.assertEqual(self.history(cid), history)
        provider.assert_not_called()
        self.assertEqual([(row['role'], row['content']) for row in history[-2:]],
                         [('user', MONEY_QUESTION), ('gpt', OUTCOME_TEXT)])
        self.assertEqual(self.used(), used)

    def test_tb4_a_new_reading_is_a_new_question_and_old_pairs_stay(self):
        cid = self.verified()
        self.reading(cid, 'money', False)
        used = self.used()
        self.reading(cid, 'money', False)
        self.assertEqual(self.used(), (used[0] + 1, 0))
        contents = [row['content'] for row in self.history(cid) if row['role'] == 'user']
        self.assertEqual(contents.count(MONEY_QUESTION), 2)

    def test_tb4_a_cut_stream_releases_the_quota_saves_no_pair_and_logs_one_topic_line(self):
        from modules.astrology_prompt import TOPIC_PROMPT
        for key, text in TOPICS.items():
            for finish in ('length', 'content_filter'):
                cid = self.verified()
                with self.subTest(topic=key, finish=finish), captured_logs() as lines:
                    before_history, before_used = self.history(cid), self.used()
                    response, provider = self.answer(cid, text, True, reply=cq.TruncatedStream(finish))
                    events = sse_events(response.text)
                    self.assertEqual(events[-1]['type'], 'error', response.text[-300:])
                    self.assertEqual(events[-1]['status'], 502)
                    self.assertNotIn('done', [event['type'] for event in events])
                    provider.assert_called_once()                                     # no automatic retry
                    self.assertIn(TOPIC_PROMPT, provider.call_args.kwargs['messages'][0]['content'])
                    self.assertEqual((self.used(), self.reserved_rows()), (before_used, 0))   # quota released
                    self.assertEqual(self.history(cid), before_history)               # no pair
                    found = outcome_lines(lines)
                    self.assertEqual(len(found), 1, found)
                    match = TOPIC_LINE.match(found[0])
                    self.assertTrue(match, found[0])
                    self.assertEqual(match[1], key)
                    self.assertIn(f'subject=topic_{key} ', found[0])
                    for private in (text, 'КАНАРЕЙКА', 'Начало ответа', f'chart={cid}', 'chart_id', 'user_id'):
                        self.assertNotIn(private, found[0])

    def test_tb4_the_money_cut_matches_the_design_expression_exactly(self):
        """DESIGN 2.6 for the exact case of the brief: a cut stream on «Деньги» gives subject=topic_money."""
        cid = self.verified()
        with captured_logs() as lines:
            self.answer(cid, MONEY_QUESTION, True, reply=cq.TruncatedStream('length'))
        found = outcome_lines(lines)
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0], r'^AI chat outcome=truncated subject=topic_money model=\S+ output_tokens=\d+ '
                                   r'duration_ms=\d+$')

    def test_tb4_a_broken_stream_is_interrupted_with_the_topic_label(self):
        cid = self.verified()
        with captured_logs() as lines:
            before_used = self.used()
            response, provider = self.answer(cid, LOVE_QUESTION, True, reply=ProviderStream(fail=True))
            events = sse_events(response.text)
            self.assertEqual((events[-1]['type'], events[-1]['status']), ('error', 502))
            provider.assert_called_once()
            self.assertEqual((self.used(), self.reserved_rows()), (before_used, 0))
        found = outcome_lines(lines)
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0], r'^AI chat outcome=interrupted subject=topic_love model=\S+ output_tokens=none '
                                   r'duration_ms=\d+$')

    def test_tb4_an_ordinary_question_keeps_the_natal_label_and_a_good_reading_writes_no_line(self):
        """The ordinary label stays natal (green half on the base); a good topic answer is not a journal event (red half)."""
        cid = self.verified()
        with captured_logs() as lines:
            self.answer(cid, 'Расскажи про мою карту', True, reply=cq.TruncatedStream('length'))
        found = outcome_lines(lines)
        self.assertEqual(len(found), 1, found)
        self.assertIn('subject=natal ', found[0])
        with captured_logs() as quiet:
            self.reading(self.verified(), 'career', True)
        self.assertEqual(outcome_lines(quiet), [])

    def test_tb4_a_failed_json_call_releases_the_quota_saves_no_pair_and_is_not_repeated(self):
        """Guard on the base (release is old); the topic text must keep it."""
        cid = self.verified()
        before_history, before_used = self.history(cid), self.used()
        with self.patch_find(return_value=self.synthetic_window()), patch(
                'modules.interpretation.client.chat.completions.create', side_effect=TimeoutError('fake')) as provider:
            response = self.post('/ask-gpt', {'chart_id': cid, 'question': MONEY_QUESTION}, False)
        self.assertGreaterEqual(response.status_code, 500, response.text)
        provider.assert_called_once()
        self.assertEqual((self.used(), self.reserved_rows()), (before_used, 0))
        self.assertEqual(self.history(cid), before_history)

    def test_tb4_a_second_topic_request_to_the_same_chart_is_409_in_progress(self):
        """Guard on the base: double press and two tabs end at the server (DESIGN 2.5)."""
        cid = self.verified()
        with self.sessions() as db:
            usage.reserve(db, 1, cid)
        for sse in (False, True):
            with self.subTest(sse=sse), patch('modules.interpretation.client.chat.completions.create') as provider:
                response = self.post('/ask-gpt', {'chart_id': cid, 'question': LOVE_QUESTION}, sse)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['detail']['code'], 'GPT_REQUEST_IN_PROGRESS')
                provider.assert_not_called()
        with self.sessions() as db:
            self.assertEqual(db.query(GPTUsage).filter_by(status='reserved').count(), 1)


if __name__ == '__main__':
    unittest.main()
