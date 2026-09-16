"""Semantic contracts use fake responses; no provider requests or application DB."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
import test_my_charts as fixtures
import test_relationship_ai as relationship_fixtures
from modules.ai_conversation import ANSWER_FORMAT, answer_parts
from modules.astrology_prompt import ASTROLOGY_SYSTEM_PROMPT
from modules.relationship_prompt import RELATIONSHIP_SYSTEM_PROMPT


SUGGESTIONS = [
    {'type': 'deepen', 'text': 'Что усиливает наши разногласия?'},
    {'type': 'personalize', 'text': 'Как мне говорить с Анной о своих потребностях?'},
    {'type': 'explore', 'text': 'Какая наша сильная сторона помогает нам сблизиться?'},
]


class FollowUpContractTests(unittest.TestCase):
    def parse(self, candidates):
        return answer_parts(json.dumps({'answer': 'Valid answer', 'follow_up_suggestions': candidates}), 'Our question?')

    def test_exact_types_preserve_identity_and_normalize_order(self):
        self.assertEqual(self.parse(list(reversed(SUGGESTIONS))), ('Valid answer', SUGGESTIONS))
        padded = deepcopy(SUGGESTIONS)
        padded[1]['text'] = '  ' + padded[1]['text'] + '  '
        self.assertEqual(self.parse(padded), ('Valid answer', SUGGESTIONS))

    def test_invalid_semantics_drop_all_buttons_but_keep_answer(self):
        cases = [None, {}, [], SUGGESTIONS[:2], SUGGESTIONS + [SUGGESTIONS[0]],
                 [item['text'] for item in SUGGESTIONS]]
        for replacement in (
            {'type': 'deepen', 'text': 'Duplicate type?'},
            {'type': 'unknown', 'text': 'Unknown?'},
            {'type': 'personalize'},
            {'type': 'personalize', 'text': None},
            {'type': 'personalize', 'text': 3},
            {'type': 'personalize', 'text': '   '},
            {'type': 'personalize', 'text': 'x' * 101},
            {'type': 'personalize', 'text': 'Line\nTwo'},
            {'type': 'personalize', 'text': 'Line\rTwo'},
            {'type': 'personalize', 'text': 'PERSONALIZE: My question?'},
            {'type': 'personalize', 'text': 'Our question?'},
            {'type': 'personalize', 'text': SUGGESTIONS[0]['text'].upper()},
            {'type': 'personalize', 'text': 'Fine?', 'extra': 'bad'},
            None, 'bad', [],
        ):
            cases.append([SUGGESTIONS[0], replacement, SUGGESTIONS[2]])
        for candidates in cases:
            with self.subTest(candidates=candidates):
                self.assertEqual(self.parse(candidates), ('Valid answer', []))

    def test_provider_and_both_api_schemas_use_same_typed_contract(self):
        schema = ANSWER_FORMAT['json_schema']['schema']['properties']['follow_up_suggestions']
        self.assertEqual((schema['minItems'], schema['maxItems']), (3, 3))
        item = schema['items']
        self.assertEqual(item['required'], ['type', 'text'])
        self.assertFalse(item['additionalProperties'])
        self.assertEqual(item['properties']['type']['enum'], ['deepen', 'personalize', 'explore'])
        schemas = fixtures.app.openapi()['components']['schemas']
        for model in ('GPTInterpretationResponse', 'RelationshipAnswer'):
            self.assertEqual(schemas[model]['properties']['follow_up_suggestions']['items'],
                             {'$ref': '#/components/schemas/FollowUpSuggestion'})

    def test_prompts_require_diverse_intents_and_subject_safety(self):
        for prompt in (ASTROLOGY_SYSTEM_PROMPT, RELATIONSHIP_SYSTEM_PROMPT):
            for kind in ('deepen:', 'personalize:', 'explore:'):
                self.assertIn(kind, prompt)
            self.assertIn('exactly three', prompt)
            self.assertIn('paraphrases', prompt)
            self.assertIn('without inventing events', prompt)
            self.assertIn('clickbait', prompt)
        self.assertIn('FIRST-PERSON', ASTROLOGY_SYSTEM_PROMPT)
        self.assertIn('Person A / Person B identity', RELATIONSHIP_SYSTEM_PROMPT)
        self.assertIn('speaker_person only when supplied', RELATIONSHIP_SYSTEM_PROMPT)


class RelationshipFollowUpTests(unittest.TestCase):
    setUp = fixtures.MyChartsTests.setUp
    tearDown = fixtures.MyChartsTests.tearDown
    pair = relationship_fixtures.RelationshipAITests.pair
    ask = relationship_fixtures.RelationshipAITests.ask

    def test_invalid_pair_suggestions_keep_answer_one_call_and_one_usage(self):
        rid, _ = self.pair()
        invalid = [SUGGESTIONS[0], SUGGESTIONS[0], SUGGESTIONS[2]]
        with patch('modules.interpretation.client.chat.completions.create', return_value=relationship_fixtures.reply(suggestions=invalid)) as provider:
            result = self.ask(rid)
        provider.assert_called_once()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['response'], 'Pair answer')
        self.assertEqual(result.json()['follow_up_suggestions'], [])
        self.assertEqual(self.client.get('/account/usage', headers=self.owner).json()['gpt_messages_used'], 1)
