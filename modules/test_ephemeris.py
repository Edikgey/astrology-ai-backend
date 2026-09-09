import sys
import os

# Добавляем корневую папку проекта в PYTHONPATH
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Импортируем модули
from modules.ephemeris import Ephemeris
from modules.aspects import Aspects
from modules.patterns import AstrologicalPatterns  # 🔹 Модуль с паттернами

year, month, day, hour = 2003, 5, 15, 9.00  # 12
lon, lat = 38.161944, 49.409167  # Сватово, Луганская область, Украина

# Создаём объект Ephemeris
ephem = Ephemeris(year, month, day, hour, lon, lat)

# ✅ Получаем тела сразу с символами в названиях
combined_bodies = ephem.get_all_bodies_with_symbols()

# 🔹 Получаем текст аспектов (строка)
aspects = Aspects(year, month, day, hour, lon, lat)
all_aspects = aspects.get_all_aspects()

# Печатаем текст всех аспектов
print(all_aspects)


# 🔥 Печатаем позиции тел
print("\n🌍 **Небесные тела и ключевые точки:**")
for body, data in combined_bodies.items():
    if isinstance(data, dict):
        retro = " (R)" if data.get("ретроградный", False) else ""
        sign = data.get("знак", "Нет данных")
        degree = data.get("градус", "Нет данных")
        house = f"({data.get('дом', 'Нет данных')} Дом)"
        print(f"{body}{retro}  {degree} {sign} {house}")

# 🔹 Получаем и конвертируем аспекты
raw_aspect_list = aspects.get_all_aspects_flat().split("\n")
raw_aspect_list = [line for line in raw_aspect_list if line.strip()]
aspect_dict = aspects.convert_aspects_to_symbols(raw_aspect_list)

# 🔹 Получаем очищенный словарь тел
bodies = ephem.get_all_bodies()
print("🧩 Ключи bodies_dict:", list(bodies.keys()))


# ✅ Получаем структурированные паттерны
patterns = AstrologicalPatterns(aspect_dict, bodies).get_patterns_structured()

# 🔻 Выводим в консоль для проверки
import json
print("\n🔴 **Структурированные паттерны:**")
print(json.dumps(patterns, ensure_ascii=False, indent=4))
