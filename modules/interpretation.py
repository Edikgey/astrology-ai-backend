from modules.ephemeris import Ephemeris
from modules.aspects import Aspects
from modules.patterns import AstrologicalPatterns
import asyncio
from sqlalchemy.orm import Session
from database.connection import get_db  # если вдруг используется Depends где-то
from database.queries import ChartInterpretationData
from openai import OpenAI
from modules.usage import OPENAI_TIMEOUT_SECONDS
from modules.ai_conversation import generate_answer
import os
client = OpenAI(timeout=OPENAI_TIMEOUT_SECONDS, max_retries=0)
OPENAI_PARAMS = {
    "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    "temperature": 0.7,
    "presence_penalty": 0.3,
    "frequency_penalty": 0.1
}
class ChartInterpreter:
    def __init__(self, year: int, month: int, day: int, hour: float, lon: float, lat: float, calculate=True):
        self.date = (year, month, day, hour)
        self.coords = (lon, lat)
        if not calculate:
            return

        # 1. Получаем тела (имя -> знак, дом, ретро)
        ephem = Ephemeris(year, month, day, hour, lon, lat)
        self.bodies = ephem.get_bodies_for_interpretation()

        # 2. Получаем аспекты (в формате для визуализации)
        aspects = Aspects(year, month, day, hour, lon, lat)
        raw_aspects = aspects.get_all_aspects_flat().split("\n")
        raw_aspects = [line.strip() for line in raw_aspects if line.strip()]
        self.aspects_for_chart = aspects.convert_aspects_for_chart(raw_aspects)

        # 3. Паттерны — по обычному dict-формату аспектов и тел
        raw_aspect_list = aspects.get_all_aspects_flat().split("\n")
        raw_aspect_list = [line for line in raw_aspect_list if line.strip()]
        aspect_dict = aspects.convert_aspects_to_symbols(raw_aspect_list)
        # Получаем очищенный словарь тел
        bodies = ephem.get_all_bodies()

        # Создаём объект паттернов с телами
        self.patterns = AstrologicalPatterns(aspect_dict, bodies).get_patterns()

        # 4. Объединяем всё в одну структуру
        self.enriched_bodies = self._merge_astrology_data()



    def get_personality_block(self) -> dict:
        """
        Возвращает данные для интерпретации личности (Солнце, Луна, Асцендент).
        """
        personality_bodies = ["☉", "☽", "AS"]
        result = {}

        for body in personality_bodies:
            data = self.enriched_bodies.get(body)
            if data:
                result[body] = {
                    "знак": data["знак"],
                    "дом": data["дом"],
                    "ретроградность": data["ретроградный"],
                    "аспекты": data["аспекты"],
                    "паттерны": data["паттерны"]
                }

        return result
    def _merge_astrology_data(self) -> dict:
        enriched = {symbol: data.copy() for symbol, data in self.bodies.items()}

        for body in enriched:
            enriched[body]["аспекты"] = []
            enriched[body]["паттерны"] = []

        for aspect in self.aspects_for_chart:
            from_body = aspect["from_body"]
            to_body = aspect["to_body"]
            aspect_symbol = aspect["aspect"]

            if from_body in enriched:
                enriched[from_body]["аспекты"].append({
                    "to": to_body,
                    "aspect": aspect_symbol
                })
            if to_body in enriched:
                enriched[to_body]["аспекты"].append({
                    "to": from_body,
                    "aspect": aspect_symbol
                })

        for pattern_text in self.patterns:
            for body in enriched:
                if body in pattern_text:
                    enriched[body]["паттерны"].append(pattern_text)

        return enriched


    def get_prompt_astrology_data(self) -> str:
        """
        Возвращает строку с данными о всех телах в формате:
            ☉ | Телец | 10 | Нет | ☌ ☊, ☍ ☋ | Большой Крест
        Каждая строка имеет 4 пробела отступа.
        """
        enriched = self.enriched_bodies
        lines = []

        for symbol, data in enriched.items():
            знак = data["знак"]
            дом = data["дом"]
            ретро = "Да" if data["ретроградный"] else "Нет"
            аспекты = ", ".join([f"{a['aspect']} {a['to']}" for a in data["аспекты"]]) if data["аспекты"] else "–"
            паттерны = ", ".join(data["паттерны"]) if data["паттерны"] else "–"

            lines.append(f"{symbol}|{знак}|{дом}|{ретро}|{аспекты}|{паттерны}")

        return "\n".join(lines)

    async def ask_gpt(self, chart_id: int, db: Session, question: str, user_id: int):
        # The sync FastAPI worker owns the DB session; no asyncio thread receives it.
        return generate_answer(db, user_id, chart_id, question, client, OPENAI_PARAMS)

    def get_all_enriched(self):
        return self.enriched_bodies

    def get_data_for(self, body_name: str):
        return self.enriched_bodies.get(body_name)

    def get_aspects_for_chart(self):
        return self.aspects_for_chart

    def get_patterns(self):
        return self.patterns
