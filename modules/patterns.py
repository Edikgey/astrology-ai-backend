from itertools import combinations
from typing import Dict, List, Tuple
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.aspects  import Aspects  # Импортируем класс Aspects

class AstrologicalPatterns:
    def __init__(self, aspects_dict, bodies_dict=None):
        """
        Класс для поиска астрологических паттернов.
        :param aspects_dict: Словарь аспектов формата {планета: [(аспект, другая планета)]}.
        :param bodies_dict: Словарь всех тел с информацией о положении (например, {'☽': {'sign': 'Скорпион', 'градус': '13° 6′'}})
        """
        self.aspects = aspects_dict
        self.bodies = bodies_dict or {}
        self.patterns = []
        self.structured_patterns = []  # Для фронтенда

        # 🔠 Маппинг символов на человекочитаемые лейблы
        self.symbol_to_label = {
            "☉": "Солнце", "☽": "Луна", "☿": "Меркурий", "♀": "Венера", "♂": "Марс",
            "♃": "Юпитер", "♄": "Сатурн", "♅": "Уран", "♆": "Нептун", "♇": "Плутон",
            "⚳": "Церера", "⚴": "Паллада", "⚵": "Юнона", "⚶": "Веста",
            "⚷": "Хирон", "φ": "Фол"
        }
    def get_patterns(self):
        """
        Запускает все алгоритмы поиска паттернов и возвращает результат.
        """
        self.patterns.clear()
        self.find_t_square()
        self.find_grand_cross()
        self.find_thors_hammer()
        self.find_golden_yod()
        self.find_stelliums()
        self.find_yod()
        self.find_grand_trine()
        self.find_minor_grand_trine()
        self.find_mystic_rectangle()
        self.find_kite()
        self.find_boomerang()
        self.find_wedge()
        return self.patterns if self.patterns else ["Нет найденных паттернов"]
    def get_patterns_structured(self) -> List[Dict]:
        """
        Возвращает список найденных паттернов в структурированном виде:
        [
            {
                "type": "T-квадрат",
                "bodies": [
                    {"symbol": "☽", "label": "Луна", "degree": "13° 6′", "sign": "Скорпион"},
                    ...
                ]
            }
        ]
        """
        self.structured_patterns = []
        self.find_t_square_structured()
        self.find_grand_cross_structured()
        self.find_thors_hammer_structured()
        self.find_golden_yod_structured()  
        self.find_stelliums_structured()
        self.find_golden_yod_structured()
        self.find_grand_trine_structured()
        self.find_minor_grand_trine_structured()
        self.find_mystic_rectangle_structured()
        self.find_kite_structured()
        self.find_boomerang_structured()
        self.find_wedge_structured()
        return self.structured_patterns
    def find_t_square(self):
        """
        Ищет T-квадраты в натальной карте.
        """
        oppositions = set()  # Храним оппозиции в виде множества (уникальные пары)
        squares = {}  # Храним квадраты в формате {планета: {список планет в квадрате}}

        # 1. Собираем оппозиции и квадраты
        for planet, aspect_list in self.aspects.items():
            for aspect, other_planet in aspect_list:
                if aspect == "☍":  # Оппозиция
                    oppositions.add(tuple(sorted((planet, other_planet))))  # Избегаем дубликатов A ☍ B и B ☍ A
                elif aspect == "□":  # Квадрат
                    if planet not in squares:
                        squares[planet] = set()
                    squares[planet].add(other_planet)


        # 2. Ищем T-квадраты
        for (planet1, planet2) in oppositions:
            for t_planet in self.aspects.keys():
                # Проверяем, есть ли t-планета в квадратах к обоим участникам оппозиции
                if t_planet in squares.get(planet1, set()) and t_planet in squares.get(planet2, set()):
                    t_square = f"T-квадрат: {planet1} ☍ {planet2}, {t_planet} □ ({planet1}, {planet2})"
                    if t_square not in self.patterns:  # Исключаем дубли
                        self.patterns.append(t_square)
    def find_t_square_structured(self):
        """
        Ищет T-квадраты и сохраняет их в self.structured_patterns
        в виде объектов с телами, градусами и знаками.
        """
        oppositions = set()
        squares = {}

        for planet, aspect_list in self.aspects.items():
            for aspect, other_planet in aspect_list:
                if aspect == "☍":
                    oppositions.add(tuple(sorted((planet, other_planet))))
                elif aspect == "□":
                    squares.setdefault(planet, set()).add(other_planet)

        for planet1, planet2 in oppositions:
            for t_planet in self.aspects.keys():
                if (
                    t_planet in squares.get(planet1, set()) and
                    t_planet in squares.get(planet2, set())
                ):
                    body_list = []
                    for symbol in [planet1, planet2, t_planet]:
                        body = self.bodies.get(symbol)
                        if body:
                            body_list.append({
                                "symbol": symbol,
                                "label": self.symbol_to_label.get(symbol, symbol),
                                "degree": body.get("градус", ""),
                                "sign": body.get("знак", "")
                            })
                    self.structured_patterns.append({
                        "type": "T-квадрат",
                        "bodies": body_list
                    })

    def find_grand_cross(self):
        """
        Ищет Большой Крест (Grand Cross) в натальной карте.
        """
        oppositions = set()
        squares = {}

        # 1. Собираем оппозиции и квадраты
        for planet, aspect_list in self.aspects.items():
            for aspect, other_planet in aspect_list:
                if aspect == "☍":
                    oppositions.add(tuple(sorted((planet, other_planet))))
                elif aspect == "□":
                    if planet not in squares:
                        squares[planet] = set()
                    squares[planet].add(other_planet)

        # 2. Ищем Большой Крест
        for (planet1, planet2) in oppositions:
            for (planet3, planet4) in oppositions:
                if planet1 == planet3 or planet2 == planet4 or planet1 == planet4 or planet2 == planet3:
                    continue  # Исключаем дубликаты

                # Проверяем, образуют ли эти планеты 4 квадрата
                if (planet1 in squares.get(planet3, set()) and
                    planet2 in squares.get(planet3, set()) and
                    planet1 in squares.get(planet4, set()) and
                    planet2 in squares.get(planet4, set())):
                    grand_cross = f"Большой Крест: {planet1} ☍ {planet2}, {planet3} ☍ {planet4}, {planet1} □ {planet3}, {planet2} □ {planet3}, {planet1} □ {planet4}, {planet2} □ {planet4}"
                    if grand_cross not in self.patterns:
                        self.patterns.append(grand_cross)
    def find_grand_cross_structured(self):
        """
        Ищет Большой Крест (Grand Cross) и сохраняет в self.structured_patterns
        в виде объектов с телами, градусами и знаками.
        """
        oppositions = set()
        squares = {}

        for planet, aspect_list in self.aspects.items():
            for aspect, other_planet in aspect_list:
                if aspect == "☍":
                    oppositions.add(tuple(sorted((planet, other_planet))))
                elif aspect == "□":
                    squares.setdefault(planet, set()).add(other_planet)

        checked = set()

        for (planet1, planet2) in oppositions:
            for (planet3, planet4) in oppositions:
                key = frozenset([planet1, planet2, planet3, planet4])
                if len(key) < 4 or key in checked:
                    continue
                checked.add(key)

                if (
                    planet1 in squares.get(planet3, set()) and
                    planet2 in squares.get(planet3, set()) and
                    planet1 in squares.get(planet4, set()) and
                    planet2 in squares.get(planet4, set())
                ):
                    body_list = []
                    for symbol in [planet1, planet2, planet3, planet4]:
                        body = self.bodies.get(symbol)
                        if body:
                            body_list.append({
                                "symbol": symbol,
                                "label": self.symbol_to_label.get(symbol, symbol),
                                "degree": body.get("градус", ""),
                                "sign": body.get("знак", "")
                            })

                    self.structured_patterns.append({
                        "type": "Большой Крест",
                        "bodies": body_list
                    })
    def find_thors_hammer(self):
        """
        Ищет Молот Тора (Thor's Hammer) в натальной карте.
        """
        squares = {}
        sesquiquadrates = {}

        # 1. Собираем квадраты (90°) и полуторные квадраты (135°)
        for planet, aspect_list in self.aspects.items():
            for aspect, other_planet in aspect_list:
                if aspect == "□":  # Квадрат (90°)
                    squares.setdefault(planet, set()).add(other_planet)
                elif aspect == "∠∠":  # Полуторный квадрат (135°)
                    sesquiquadrates.setdefault(planet, set()).add(other_planet)


        # 2. Ищем Молоты Тора
        for planet1, square_planets in squares.items():
            for planet2 in square_planets:
                for t_planet in sesquiquadrates.get(planet1, set()) & sesquiquadrates.get(planet2, set()):
                    if (t_planet in sesquiquadrates.get(planet1, set()) and
                        t_planet in sesquiquadrates.get(planet2, set()) and
                        planet1 != planet2 and planet1 != t_planet and planet2 != t_planet):

                        thors_hammer = f"Молот Тора: {planet1} □ {planet2}, {t_planet} ∠∠ ({planet1}, {planet2})"
                        if thors_hammer not in self.patterns:
                            self.patterns.append(thors_hammer)
    def find_thors_hammer_structured(self):
        """
        Ищет Молот Тора (Thor's Hammer) и сохраняет в self.structured_patterns в структурированном виде.
        """
        squares = {}
        sesquiquadrates = {}

        for planet, aspect_list in self.aspects.items():
            for aspect, other_planet in aspect_list:
                if aspect == "□":
                    squares.setdefault(planet, set()).add(other_planet)
                elif aspect == "∠∠":
                    sesquiquadrates.setdefault(planet, set()).add(other_planet)

        seen = set()

        for planet1, square_planets in squares.items():
            for planet2 in square_planets:
                common_third = sesquiquadrates.get(planet1, set()) & sesquiquadrates.get(planet2, set())

                for t_planet in common_third:
                    key = frozenset((planet1, planet2, t_planet))
                    if key in seen:
                        continue
                    seen.add(key)

                    body_list = []
                    for symbol in [planet1, planet2, t_planet]:
                        body = self.bodies.get(symbol)
                        if body:
                            body_list.append({
                                "symbol": symbol,
                                "label": self.symbol_to_label.get(symbol, symbol),
                                "degree": body.get("градус", ""),
                                "sign": body.get("знак", "")
                            })

                    self.structured_patterns.append({
                        "type": "Молот Тора",
                        "bodies": body_list
                    })


    def collect_aspects(self):
        """
        Собирает все квинтили (Q) и биквинтили (bQ) в отдельные словари.
        """
        quintiles = {}
        biquintiles = {}

        for planet, aspect_list in self.aspects.items():
            for aspect, other in aspect_list:
                if aspect == "Q":
                    quintiles.setdefault(planet, set()).add(other)
                    quintiles.setdefault(other, set()).add(planet)  # Симметрично!
                elif aspect == "bQ":
                    biquintiles.setdefault(planet, set()).add(other)
                    # Внимание: не добавляем other → planet (bQ — не всегда симметричен!)

        return quintiles, biquintiles

    def find_quintile_pairs(self, quintiles):
        """
        Находит уникальные пары планет, соединённые квинтилем.
        """
        seen = set()
        pairs = []

        for a in quintiles:
            for b in quintiles[a]:
                if a == b:
                    continue
                key = frozenset((a, b))
                if key not in seen:
                    seen.add(key)
                    pairs.append((a, b))
        return pairs

    def find_biquintile_intersections(self, quintile_pairs, biquintiles):
        """
        Ищет третью планету C, соединённую биквинтилем (144°) с обеими планетами A и B.
        """
        possible_yods = []

        for a, b in quintile_pairs:
            bq_a = biquintiles.get(a, set())
            bq_b = biquintiles.get(b, set())

            # Пересечение — это возможные планеты C
            common = bq_a & bq_b

            for c in common:
                if c != a and c != b:
                    possible_yods.append((a, b, c))

        return possible_yods

    def format_golden_yods(self, possible_yods):
        """
        Форматирует найденные Золотые Йоды в человекочитаемый формат.
        """
        unique_patterns = set()
        formatted_patterns = []

        for a, b, c in possible_yods:
            key = frozenset((a, b, c))
            if key in unique_patterns:
                continue
            unique_patterns.add(key)

            golden_yod = f"⭐ Золотой Йод: {a} Q {b}, {a} bQ {c}, {b} bQ {c}"
            formatted_patterns.append(golden_yod)

        return formatted_patterns

    def find_golden_yod(self):
        """
        Ищет паттерн Золотой Йод: A Q B, C bQ A и C bQ B.
        """
        quintiles, biquintiles = self.collect_aspects()
        quintile_pairs = self.find_quintile_pairs(quintiles)

        golden_yods = []
        seen = set()

        for a, b in quintile_pairs:
            for c in biquintiles:
                if a in biquintiles[c] and b in biquintiles[c]:
                    key = frozenset((a, b, c))
                    if key not in seen:
                        seen.add(key)
                        golden_yod = f"⭐ Золотой Йод: {a} Q {b}, {c} bQ {a}, {c} bQ {b}"
                        golden_yods.append(golden_yod)

        self.patterns.extend(golden_yods)
    def find_golden_yod_structured(self):
        """
        Ищет Золотой Йод и сохраняет результат в self.structured_patterns
        в структурированном виде для фронта.
        """
        quintiles, biquintiles = self.collect_aspects()
        quintile_pairs = self.find_quintile_pairs(quintiles)
        possible_yods = self.find_biquintile_intersections(quintile_pairs, biquintiles)

        unique_patterns = set()

        for a, b, c in possible_yods:
            key = frozenset((a, b, c))
            if key in unique_patterns:
                continue
            unique_patterns.add(key)

            body_list = []
            for symbol in [a, b, c]:
                body = self.bodies.get(symbol)
                if body:
                    body_list.append({
                        "symbol": symbol,
                        "label": self.symbol_to_label.get(symbol, symbol),
                        "degree": body.get("градус", ""),
                        "sign": body.get("знак", "")
                    })

            self.structured_patterns.append({
                "type": "Золотой Йод",
                "bodies": body_list
            })

    def find_stelliums(self):
        """
        Поиск стеллиумов: 4 и более объектов в одном знаке Зодиака.
        """
        sign_counts = {}

        for body, info in self.bodies.items():
            sign = info.get("знак") or info.get("sign")
            if not sign:
                continue
            if sign not in sign_counts:
                sign_counts[sign] = []
            sign_counts[sign].append(body)

        stelliums = {sign: bodies for sign, bodies in sign_counts.items() if len(bodies) >= 4}

        for sign, bodies in stelliums.items():
            formatted = f"⭐ Стеллиум в знаке {sign}: {', '.join(bodies)}"
            self.patterns.append(formatted)

        return stelliums
    
    def find_stelliums_structured(self):
        """
        Поиск стеллиумов: 4 и более объектов в одном знаке Зодиака.
        Сохраняет результат в self.structured_patterns в виде списка объектов.
        """
        sign_groups = {}

        for symbol, info in self.bodies.items():
            sign = info.get("знак") or info.get("sign")
            if not sign:
                continue
            sign_groups.setdefault(sign, []).append(symbol)

        for sign, symbols in sign_groups.items():
            if len(symbols) >= 4:
                body_list = []
                for symbol in symbols:
                    body = self.bodies.get(symbol)
                    if body:
                        body_list.append({
                            "symbol": symbol,
                            "label": self.symbol_to_label.get(symbol, symbol),
                            "degree": body.get("градус", ""),
                            "sign": sign
                        })

                self.structured_patterns.append({
                    "type": f"Стеллиум в знаке {sign}",
                    "bodies": body_list
                })

    def find_yod(self):
        """
        Ищет паттерн Йод (Yod): A ⚹ B, C ⚻ A и C ⚻ B.
        """
        sextiles = []
        quincunxes = {}

        for planet, aspect_list in self.aspects.items():
            for aspect, other in aspect_list:
                if aspect == "⚹":
                    pair = tuple(sorted((planet, other)))
                    if pair not in sextiles:
                        sextiles.append(pair)
                elif aspect == "⚻":
                    quincunxes.setdefault(planet, set()).add(other)

        found_yods = []
        seen = set()

        for a, b in sextiles:
            for c in quincunxes:
                if a in quincunxes[c] and b in quincunxes[c]:
                    key = frozenset((a, b, c))
                    if key not in seen:
                        seen.add(key)
                        yod = f"🔺 Йод: {a} ⚹ {b}, {c} ⚻ {a}, {c} ⚻ {b}"
                        found_yods.append(yod)

        self.patterns.extend(found_yods)

    def find_yod_structured(self):
        """
        Ищет Йод (Yod): A ⚹ B, C ⚻ A и C ⚻ B.
        Сохраняет результат в self.structured_patterns.
        """
        sextiles = []
        quincunxes = {}

        for planet, aspect_list in self.aspects.items():
            for aspect, other in aspect_list:
                if aspect == "⚹":
                    pair = tuple(sorted((planet, other)))
                    if pair not in sextiles:
                        sextiles.append(pair)
                elif aspect == "⚻":
                    quincunxes.setdefault(planet, set()).add(other)

        seen = set()

        for a, b in sextiles:
            for c in quincunxes:
                if a in quincunxes[c] and b in quincunxes[c]:
                    key = frozenset((a, b, c))
                    if key in seen:
                        continue
                    seen.add(key)

                    body_list = []
                    for symbol in [a, b, c]:
                        body = self.bodies.get(symbol)
                        if body:
                            body_list.append({
                                "symbol": symbol,
                                "label": self.symbol_to_label.get(symbol, symbol),
                                "degree": body.get("градус", ""),
                                "sign": body.get("знак", "")
                            })

                    self.structured_patterns.append({
                        "type": "Йод",
                        "bodies": body_list
                    })

    def find_grand_trine(self):
        """
        Ищет Гранд Трины — три планеты, соединённые трином (△) и находящиеся в одной стихии.
        """
        element_map = {
            "Овен": "Огонь", "Лев": "Огонь", "Стрелец": "Огонь",
            "Телец": "Земля", "Дева": "Земля", "Козерог": "Земля",
            "Близнецы": "Воздух", "Весы": "Воздух", "Водолей": "Воздух",
            "Рак": "Вода", "Скорпион": "Вода", "Рыбы": "Вода"
        }

        KNOWN_OBJECTS = {
            "Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀", "Марс": "♂",
            "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅", "Нептун": "♆", "Плутон": "♇",
            "Северный Узел": "☊", "Южный Узел": "☋", "Церера": "⚳", "Паллада": "⚴",
            "Юнона": "⚵", "Веста": "⚶", "Хирон": "⚷", "Фол": "φ",
            "Черная Луна Лилит": "⚸", "Белая Луна Селена": "W",
            "Асцендент": "AS", "Десцендент": "DS", "Средина Неба": "MC", "Надир": "IC",
            "Вертекс": "Vx", "Часть Фортуны": "X"
        }
        elements = {"Огонь": [], "Земля": [], "Воздух": [], "Вода": []}

        for name, data in self.bodies.items():
            sign = data.get("знак") or data.get("sign")
            if not sign:
                continue
            element = element_map.get(sign)
            if not element:
                continue
            symbol = KNOWN_OBJECTS.get(name)
            if not symbol:
                continue
            elements[element].append(symbol)

        seen = set()
        found_trines = []

        for element, group in elements.items():
            if len(group) < 3:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    for k in range(j + 1, len(group)):
                        a, b, c = group[i], group[j], group[k]
                        trines = [
                            ("△", b) in self.aspects.get(a, []),
                            ("△", c) in self.aspects.get(a, []),
                            ("△", c) in self.aspects.get(b, [])
                        ]
                        if all(trines):
                            key = frozenset((a, b, c))
                            if key not in seen:
                                seen.add(key)
                                pattern = f"🔺 Гранд Трин ({element}): {a}, {b}, {c}"
                                found_trines.append(pattern)

        self.patterns.extend(found_trines)
    def find_grand_trine_structured(self):
        """
        Ищет Гранд Трины (Grand Trine) и сохраняет результат в self.structured_patterns.
        Каждая тройка планет связана трином (△) и принадлежит одной стихии.
        """
        element_map = {
            "Овен": "Огонь", "Лев": "Огонь", "Стрелец": "Огонь",
            "Телец": "Земля", "Дева": "Земля", "Козерог": "Земля",
            "Близнецы": "Воздух", "Весы": "Воздух", "Водолей": "Воздух",
            "Рак": "Вода", "Скорпион": "Вода", "Рыбы": "Вода"
        }

        elements = {"Огонь": [], "Земля": [], "Воздух": [], "Вода": []}

        for symbol, data in self.bodies.items():
            sign = data.get("знак") or data.get("sign")
            if not sign:
                continue
            element = element_map.get(sign)
            if not element:
                continue
            elements[element].append(symbol)

        seen = set()

        for element, group in elements.items():
            if len(group) < 3:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    for k in range(j + 1, len(group)):
                        a, b, c = group[i], group[j], group[k]
                        trines = [
                            ("△", b) in self.aspects.get(a, []),
                            ("△", c) in self.aspects.get(a, []),
                            ("△", c) in self.aspects.get(b, [])
                        ]
                        if all(trines):
                            key = frozenset((a, b, c))
                            if key in seen:
                                continue
                            seen.add(key)

                            body_list = []
                            for sym in [a, b, c]:
                                body = self.bodies.get(sym)
                                if body:
                                    body_list.append({
                                        "symbol": sym,
                                        "label": self.symbol_to_label.get(sym, sym),
                                        "degree": body.get("градус", ""),
                                        "sign": body.get("знак", "")
                                    })

                            self.structured_patterns.append({
                                "type": f"Гранд Трин ({element})",
                                "bodies": body_list
                            })

    def find_minor_grand_trine(self):
        """
        Ищет паттерн Бисекстиль (Minor Grand Trine): A ⚹ C, B ⚹ C, A ☍ B.
        """
        sextiles = []
        oppositions = set()

        # Шаг 1: Собираем все секстили и оппозиции
        for planet, aspect_list in self.aspects.items():
            for aspect, other in aspect_list:
                if aspect == "⚹":
                    pair = tuple(sorted((planet, other)))
                    if pair not in sextiles:
                        sextiles.append(pair)
                elif aspect == "☍":
                    oppositions.add(frozenset((planet, other)))

        found_minor_trines = []
        seen = set()

        # Шаг 2: Проверяем возможные бисекстили
        for (a, c1) in sextiles:
            for (b, c2) in sextiles:
                if c1 != c2:
                    continue  # Центр (С) должен быть общий
                c = c1
                if len({a, b, c}) < 3:
                    continue  # Все точки должны быть разные

                opp_pair = frozenset((a, b))
                if opp_pair in oppositions:
                    key = frozenset((a, b, c))
                    if key not in seen:
                        seen.add(key)
                        pattern = f"🔷 Бисекстиль: {a} ⚹ {c}, {b} ⚹ {c}, {a} ☍ {b}"
                        found_minor_trines.append(pattern)

        self.patterns.extend(found_minor_trines)

    def find_minor_grand_trine_structured(self):
        """
        Ищет паттерн Бисекстиль (Minor Grand Trine) и сохраняет результат в self.structured_patterns.
        Формат: A ⚹ C, B ⚹ C, A ☍ B
        """
        sextiles = []
        oppositions = set()

        # Шаг 1: Собираем все секстили и оппозиции
        for planet, aspect_list in self.aspects.items():
            for aspect, other in aspect_list:
                if aspect == "⚹":
                    pair = tuple(sorted((planet, other)))
                    if pair not in sextiles:
                        sextiles.append(pair)
                elif aspect == "☍":
                    oppositions.add(frozenset((planet, other)))

        seen = set()

        # Шаг 2: Проверяем возможные бисекстили
        for (a, c1) in sextiles:
            for (b, c2) in sextiles:
                if c1 != c2:
                    continue  # Центр (С) должен быть общий
                c = c1
                if len({a, b, c}) < 3:
                    continue

                opp_pair = frozenset((a, b))
                if opp_pair in oppositions:
                    key = frozenset((a, b, c))
                    if key in seen:
                        continue
                    seen.add(key)

                    body_list = []
                    for sym in [a, b, c]:
                        body = self.bodies.get(sym)
                        if body:
                            body_list.append({
                                "symbol": sym,
                                "label": self.symbol_to_label.get(sym, sym),
                                "degree": body.get("градус", ""),
                                "sign": body.get("знак", "")
                            })

                    self.structured_patterns.append({
                        "type": "Бисекстиль",
                        "bodies": body_list
                    })


    def find_mystic_rectangle(self):
        """
        Ищет паттерн Мистический Прямоугольник (Mystic Rectangle):
        Две оппозиции (☍) и два мягких аспекта (⚹ или △), соединяющие концы оппозиций перекрёстно.
        Пример: A ☍ B и C ☍ D, при этом A ⚹/△ D и B ⚹/△ C.
        """
        oppositions = []
        soft_aspects = {}

        for planet, aspect_list in self.aspects.items():
            for aspect, other in aspect_list:
                if aspect == "☍":
                    pair = frozenset((planet, other))
                    if pair not in oppositions:
                        oppositions.append(pair)
                elif aspect in ("⚹", "△"):
                    soft_aspects.setdefault(planet, set()).add(other)

        found = set()

        for i in range(len(oppositions)):
            for j in range(i + 1, len(oppositions)):
                A, B = list(oppositions[i])
                C, D = list(oppositions[j])

                combinations = [
                    (A, B, C, D),
                    (A, B, D, C),
                    (B, A, C, D),
                    (B, A, D, C)
                ]

                for A1, B1, C1, D1 in combinations:
                    if D1 in soft_aspects.get(A1, set()) and C1 in soft_aspects.get(B1, set()):
                        key = tuple(sorted([
                            tuple(sorted((A1, B1))),
                            tuple(sorted((C1, D1))),
                            (A1, D1),
                            (B1, C1)
                        ]))
                        if key not in found:
                            found.add(key)
                            pattern = f"🧩 Мистический Прямоугольник: {A1} ☍ {B1}, {C1} ☍ {D1}, {A1} ⚹/△ {D1}, {B1} ⚹/△ {C1}"
                            self.patterns.append(pattern)
    def find_mystic_rectangle_structured(self):
        """
        Ищет паттерн Мистический Прямоугольник и сохраняет результат в self.structured_patterns.
        Условия: A ☍ B и C ☍ D, при этом A ⚹/△ D и B ⚹/△ C
        """
        oppositions = []
        soft_aspects = {}

        # Сбор оппозиций и мягких аспектов
        for planet, aspect_list in self.aspects.items():
            for aspect, other in aspect_list:
                if aspect == "☍":
                    pair = frozenset((planet, other))
                    if pair not in oppositions:
                        oppositions.append(pair)
                elif aspect in ("⚹", "△"):
                    soft_aspects.setdefault(planet, set()).add(other)

        seen = set()

        for i in range(len(oppositions)):
            for j in range(i + 1, len(oppositions)):
                A, B = list(oppositions[i])
                C, D = list(oppositions[j])

                # Все возможные комбинации перестановок
                combinations = [
                    (A, B, C, D),
                    (A, B, D, C),
                    (B, A, C, D),
                    (B, A, D, C)
                ]

                for A1, B1, C1, D1 in combinations:
                    if D1 in soft_aspects.get(A1, set()) and C1 in soft_aspects.get(B1, set()):
                        key = frozenset((A1, B1, C1, D1))
                        if key in seen:
                            continue
                        seen.add(key)

                        body_list = []
                        for sym in [A1, B1, C1, D1]:
                            body = self.bodies.get(sym)
                            if body:
                                body_list.append({
                                    "symbol": sym,
                                    "label": self.symbol_to_label.get(sym, sym),
                                    "degree": body.get("градус", ""),
                                    "sign": body.get("знак", "")
                                })

                        self.structured_patterns.append({
                            "type": "Мистический Прямоугольник",
                            "bodies": body_list
                        })

    def find_kite(self):
        element_map = {
            "Овен": "Огонь", "Лев": "Огонь", "Стрелец": "Огонь",
            "Телец": "Земля", "Дева": "Земля", "Козерог": "Земля",
            "Близнецы": "Воздух", "Весы": "Воздух", "Водолей": "Воздух",
            "Рак": "Вода", "Скорпион": "Вода", "Рыбы": "Вода"
        }

        KNOWN_OBJECTS = {
            "Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀", "Марс": "♂",
            "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅", "Нептун": "♆", "Плутон": "♇",
            "Северный Узел": "☊", "Южный Узел": "☋", "Церера": "⚳", "Паллада": "⚴",
            "Юнона": "⚵", "Веста": "⚶", "Хирон": "⚷", "Фол": "φ",
            "Черная Луна Лилит": "⚸", "Белая Луна Селена": "W",
            "Асцендент": "AS", "Десцендент": "DS", "Средина Неба": "MC", "Надир": "IC",
            "Вертекс": "Vx", "Часть Фортуны": "X"
        }

        elements = {"Огонь": [], "Земля": [], "Воздух": [], "Вода": []}

        for name, data in self.bodies.items():
            sign = data.get("знак") or data.get("sign")
            if not sign:
                continue
            element = element_map.get(sign)
            if not element:
                continue
            symbol = KNOWN_OBJECTS.get(name)
            if not symbol:
                continue
            elements[element].append(symbol)

        from itertools import combinations

        found_kites = []
        seen_keys = set()

        for element, group in elements.items():
            if len(group) < 3:
                continue
            for a, b, c in combinations(group, 3):
                if all([
                    ("△", b) in self.aspects.get(a, []),
                    ("△", c) in self.aspects.get(a, []),
                    ("△", c) in self.aspects.get(b, []),
                ]):
                    trine_points = [a, b, c]
                    for d in self.bodies:
                        d_symbol = KNOWN_OBJECTS.get(d)
                        if not d_symbol or d_symbol in trine_points:
                            continue
                        for apex in trine_points:
                            base = [p for p in trine_points if p != apex]
                            opp = ("☍", apex) in self.aspects.get(d_symbol, []) or ("☍", d_symbol) in self.aspects.get(apex, [])
                            sextile_1 = ("⚹", base[0]) in self.aspects.get(d_symbol, []) or ("⚹", d_symbol) in self.aspects.get(base[0], [])
                            sextile_2 = ("⚹", base[1]) in self.aspects.get(d_symbol, []) or ("⚹", d_symbol) in self.aspects.get(base[1], [])
                            if opp and sextile_1 and sextile_2:
                                key = frozenset([a, b, c, d_symbol])
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    pattern = f"🎯 Кайт: Гранд Трин ({element}) {a}, {b}, {c}; {d_symbol} ☍ {apex}, ⚹ {base[0]}, ⚹ {base[1]}"
                                    found_kites.append(pattern)

        self.patterns.extend(found_kites)

    def find_kite_structured(self):
        """
        Ищет Кайт и сохраняет результат в self.structured_patterns
        в структурированном виде для фронта.
        """
        from itertools import combinations

        element_map = {
            "Овен": "Огонь", "Лев": "Огонь", "Стрелец": "Огонь",
            "Телец": "Земля", "Дева": "Земля", "Козерог": "Земля",
            "Близнецы": "Воздух", "Весы": "Воздух", "Водолей": "Воздух",
            "Рак": "Вода", "Скорпион": "Вода", "Рыбы": "Вода"
        }

        elements = {"Огонь": [], "Земля": [], "Воздух": [], "Вода": []}

        for symbol, data in self.bodies.items():
            sign = data.get("знак")
            element = element_map.get(sign)
            if element:
                elements[element].append(symbol)

        seen_keys = set()

        for element, group in elements.items():
            if len(group) < 3:
                continue

            for a, b, c in combinations(group, 3):
                if all([
                    ("△", b) in self.aspects.get(a, []),
                    ("△", c) in self.aspects.get(a, []),
                    ("△", c) in self.aspects.get(b, []),
                ]):
                    trine_points = [a, b, c]
                    for d_symbol in self.bodies:
                        if d_symbol in trine_points:
                            continue
                        for apex in trine_points:
                            base = [p for p in trine_points if p != apex]
                            opp = ("☍", apex) in self.aspects.get(d_symbol, []) or ("☍", d_symbol) in self.aspects.get(apex, [])
                            sextile_1 = ("⚹", base[0]) in self.aspects.get(d_symbol, []) or ("⚹", d_symbol) in self.aspects.get(base[0], [])
                            sextile_2 = ("⚹", base[1]) in self.aspects.get(d_symbol, []) or ("⚹", d_symbol) in self.aspects.get(base[1], [])

                            if opp and sextile_1 and sextile_2:
                                key = frozenset([a, b, c, d_symbol])
                                if key in seen_keys:
                                    continue
                                seen_keys.add(key)

                                # Собираем тела
                                all_symbols = [a, b, c, d_symbol]
                                body_list = []
                                for sym in all_symbols:
                                    body = self.bodies.get(sym)
                                    if body:
                                        body_list.append({
                                            "symbol": sym,
                                            "label": self.symbol_to_label.get(sym, sym),
                                            "degree": body.get("градус", ""),
                                            "sign": body.get("знак", "")
                                        })

                                self.structured_patterns.append({
                                    "type": "Кайт",
                                    "bodies": body_list
                                })

    def find_boomerang(self):
        """
        Ищет паттерн Бумеранг:
        Две планеты находятся в секстиле (⚹), обе формируют квинконс (⚻) к третьей,
        а к этой третьей добавляется оппозиция (☍) от четвёртой.
        Пример: A ⚹ B, A ⚻ C, B ⚻ C, C ☍ D
        """
        from collections import defaultdict

        soft_aspects = []
        quincunxes = defaultdict(set)
        oppositions = defaultdict(set)

        for planet, aspect_list in self.aspects.items():
            for aspect, target in aspect_list:
                if aspect == "⚹":
                    soft_aspects.append((planet, target))
                elif aspect == "⚻":
                    quincunxes[planet].add(target)
                elif aspect == "☍":
                    oppositions[planet].add(target)

        found = []
        seen_keys = set()

        for a, b in soft_aspects:
            common_c = quincunxes[a] & quincunxes[b]
            for c in common_c:
                possible_d = oppositions[c] | {k for k, v in oppositions.items() if c in v}
                for d in possible_d:
                    key = frozenset([a, b, c, d])
                    if len(key) < 4 or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    pattern = f"🎯 Бумеранг: {a} ⚹ {b}, оба ⚻ {c}, {c} ☍ {d}"
                    found.append(pattern)

        self.patterns.extend(found)
    def find_boomerang_structured(self):
        """
        Ищет паттерн Бумеранг и сохраняет результат в self.structured_patterns
        в структурированном виде для фронта.
        """
        from collections import defaultdict

        soft_aspects = []
        quincunxes = defaultdict(set)
        oppositions = defaultdict(set)

        for planet, aspect_list in self.aspects.items():
            for aspect, target in aspect_list:
                if aspect == "⚹":
                    soft_aspects.append((planet, target))
                elif aspect == "⚻":
                    quincunxes[planet].add(target)
                elif aspect == "☍":
                    oppositions[planet].add(target)

        found = []
        seen_keys = set()

        for a, b in soft_aspects:
            common_c = quincunxes[a] & quincunxes[b]
            for c in common_c:
                possible_d = oppositions[c] | {k for k, v in oppositions.items() if c in v}
                for d in possible_d:
                    key = frozenset([a, b, c, d])
                    if len(key) < 4 or key in seen_keys:
                        continue
                    seen_keys.add(key)

                    # Собираем тела
                    all_symbols = [a, b, c, d]
                    body_list = []
                    for sym in all_symbols:
                        body = self.bodies.get(sym)
                        if body:
                            body_list.append({
                                "symbol": sym,
                                "label": self.symbol_to_label.get(sym, sym),
                                "degree": body.get("градус", ""),
                                "sign": body.get("знак", "")
                            })

                    self.structured_patterns.append({
                        "type": "Бумеранг",
                        "bodies": body_list
                    })

    def find_wedge(self):
        """
        Ищет паттерн Парус:
        Две планеты в трине (△), и третья делает секстили (⚹) к обеим.
        Пример: A △ B, C ⚹ A, C ⚹ B
        """
        from collections import defaultdict

        trines = []
        sextiles = defaultdict(set)

        for planet, aspect_list in self.aspects.items():
            for aspect, target in aspect_list:
                if aspect == "△":
                    trines.append((planet, target))
                elif aspect == "⚹":
                    sextiles[planet].add(target)

        found = []
        seen = set()

        for a, b in trines:
            for c in sextiles:
                if a in sextiles[c] and b in sextiles[c]:
                    key = frozenset([a, b, c])
                    if key in seen:
                        continue
                    seen.add(key)
                    pattern = f"🎯 Парус: {c} ⚹ {a}, ⚹ {b}, {a} △ {b}"
                    found.append(pattern)
    def find_wedge_structured(self):
        """
        Ищет паттерн Парус и сохраняет результат в self.structured_patterns
        в структурированном виде для фронта.
        """
        from collections import defaultdict

        trines = []
        sextiles = defaultdict(set)

        for planet, aspect_list in self.aspects.items():
            for aspect, target in aspect_list:
                if aspect == "△":
                    trines.append((planet, target))
                elif aspect == "⚹":
                    sextiles[planet].add(target)

        found = []
        seen = set()

        for a, b in trines:
            for c in sextiles:
                if a in sextiles[c] and b in sextiles[c]:
                    key = frozenset([a, b, c])
                    if key in seen:
                        continue
                    seen.add(key)

                    # Собираем тела
                    all_symbols = [c, a, b]
                    body_list = []
                    for sym in all_symbols:
                        body = self.bodies.get(sym)
                        if body:
                            body_list.append({
                                "symbol": sym,
                                "label": self.symbol_to_label.get(sym, sym),
                                "degree": body.get("градус", ""),
                                "sign": body.get("знак", "")
                            })

                    self.structured_patterns.append({
                        "type": "Парус",
                        "bodies": body_list
                    })

    