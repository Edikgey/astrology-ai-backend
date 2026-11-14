"""Requests that need live positions must never be sent as natal chart questions.

The route test for transit questions moved to test_transits_guard.py (TG4/TG5, TRANSITS-1): an intentional
contract change - a verified chart now answers them, a legacy chart and the pair chat refuse with new texts.
"""
import unittest
from unittest.mock import patch

import test_my_charts as charts
import test_relationship_ai as relationships
from modules.ai_transit_guard import requires_transit_data


URANUS_QUESTION = (
    "Какой транзит Урана сейчас, 15 ноября 2026 года в 12:00 UTC? "
    "В каком знаке он на эту дату?"
)


class TransitGuardTests(unittest.TestCase):
    setUp = charts.MyChartsTests.setUp
    tearDown = charts.MyChartsTests.tearDown
    chart = charts.MyChartsTests.chart

    def test_detects_dated_positions_and_requested_transit_calculations(self):
        questions = (
            URANUS_QUESTION,
            "В каком знаке Уран 15.11.26?",
            "Где был Уран 15 ноября?",
            "Где сейчас Сатурн?",
            "Где транзитный Уран?",
            "Покажи мои транзиты.",
            "Какие сейчас транзиты относительно моей натальной карты?",
            "Коли Сатурн увійде в Овен?",
            "Де зараз Уран?",
            "Where is Uranus on November 15?",
            "What sign is Saturn in tomorrow?",
            "Calculate transits for our relationship.",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertTrue(requires_transit_data(question))

    def test_keeps_natal_and_general_questions(self):
        questions = (
            "Где Уран в моей натальной карте?",
            "В каком знаке был Уран на момент рождения, 15 ноября 1996 года?",
            "Где в моей карте Юпитер? Сегодня мне предложили новую работу.",
            "Сегодня я прочитал про транзиты. Что такое транзиты?",
            "Что обычно означает транзит Сатурна через седьмой дом?",
            "Як Сатурн у моїй натальній карті впливає на стосунки?",
            "Where was Uranus at birth on November 15, 1996?",
            "What are transits in astrology?",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertFalse(requires_transit_data(question))

    def test_natal_uranus_question_still_reaches_chat(self):
        chart_id = self.chart()
        with patch(
            "modules.interpretation.client.chat.completions.create",
            return_value=relationships.reply(answer="Натальный ответ"),
        ) as provider:
            response = self.client.post(
                "/ask-gpt",
                json={"chart_id": chart_id, "question": "Где Уран в моей натальной карте?"},
                headers=self.owner,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["response"], "Натальный ответ")
        provider.assert_called_once()
