from modules.ephemeris import Ephemeris
from modules.aspects import Aspects
from modules.patterns import AstrologicalPatterns
import asyncio
from sqlalchemy.orm import Session
from database.connection import get_db  # если вдруг используется Depends где-то
from database.queries import ChartInterpretationData
from openai import OpenAI
client = OpenAI()
OPENAI_PARAMS = {
    "model": "gpt-4o-mini",  # или используйте другую модель по вашему усмотрению
    "temperature": 0.7,
    "presence_penalty": 0.3,
    "frequency_penalty": 0.1
}
class ChartInterpreter:
    def __init__(self, year: int, month: int, day: int, hour: float, lon: float, lat: float):
        self.date = (year, month, day, hour)
        self.coords = (lon, lat)

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

    async def ask_gpt(self, chart_id: int, db: Session, question: str) -> str:
        """
        Отправляет натальную карту и вопрос в ChatGPT, возвращает интерпретацию.
        Кэширует результат get_prompt_astrology_data() в таблицу chart_interpretation_data.
        """
        interpretation = db.query(ChartInterpretationData).filter_by(chart_id=chart_id).first()

        if not interpretation:
            # 🔁 Обязательно перед get_prompt_astrology_data
            self.enriched_bodies = self._merge_astrology_data()

            chart_data = self.get_prompt_astrology_data()
            interpretation = ChartInterpretationData(chart_id=chart_id, raw_text=chart_data)
            db.add(interpretation)
            db.commit()
            db.refresh(interpretation)
        else:
            chart_data = interpretation.raw_text

        prompt = (
            "Ты астролог. Ниже будет приведена натальная карта.\n"
            "Каждая строка имеет формат:\n"
            "[Точка]|[Знак]|[Дом]|[Ретроградность]|[Аспекты]|[Паттерны]\n"
            "Тебе нужно анализировать карту и отвечать на вопрос пользователя.\n"
            "Избегай сухих технических терминов. Говори образно и по-человечески, нежно и мягко.\n"
            "Вот натальная карта:\n"
            f"{chart_data}\n"
            f"\nВопрос: {question}"
        )

        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENAI_PARAMS["model"],
            messages=[{"role": "system", "content": prompt}],
            temperature=OPENAI_PARAMS["temperature"],
            presence_penalty=OPENAI_PARAMS["presence_penalty"],
            frequency_penalty=OPENAI_PARAMS["frequency_penalty"]
        )

        return response.choices[0].message.content.strip()
    def get_all_enriched(self):
        return self.enriched_bodies

    def get_data_for(self, body_name: str):
        return self.enriched_bodies.get(body_name)

    def get_aspects_for_chart(self):
        return self.aspects_for_chart

    def get_patterns(self):
        return self.patterns
