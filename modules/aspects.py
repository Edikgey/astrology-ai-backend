from modules.ephemeris import Ephemeris
import sys
import os


sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

class Aspects:
    def __init__(self, year, month, day, hour, lon, lat):
        ephemeris = Ephemeris(year, month, day, hour, lon, lat)
        self.planets = ephemeris.get_planet_longitudes()
        self.asteroids = ephemeris.get_asteroid_longitudes()
        self.key_points = ephemeris.get_key_point_longitudes()
        
        self.objects = {**self.planets, **self.asteroids, **self.key_points}

        self.major_aspects = {
            "Соединение": (0, "max", "☌"),
            "Оппозиция": (180, "max", "☍"),
            "Трин": (120, "mid", "△"),
            "Квадрат": (90, "mid", "□"),
            "Секстиль": (60, "min", "⚹")
        }

        self.minor_aspects = {
            "Полусекстиль": (30, "min", "⚺"),
            "Полуквадрат": (45, "min", "∠"),
            "Квинтиль": (72, "min", "Q"),
            "Биквинтиль": (144, "min", "bQ"),
            "Септиль": (51.43, "min", "S"),
            "Новиль": (40, "min", "N"),
            "Квинконс": (150, "min", "⚻"),
            "Полутороквадрат": (135, "min", "∠∠")
        }
        # Словари для символов объектов и аспектов
        self.KNOWN_OBJECTS = {
            "Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀", "Марс": "♂",
            "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅", "Нептун": "♆", "Плутон": "♇",
            "Северный Узел": "☊", "Южный Узел": "☋", "Церера": "⚳", "Паллада": "⚴",
            "Юнона": "⚵", "Веста": "⚶", "Хирон": "⚷", "Фол": "φ",
            "Черная Луна Лилит": "⚸", "Белая Луна Селена": "W",
            "Асцендент": "AS", "Десцендент": "DS", "Средина Неба": "MC", "Надир": "IC",
            "Вертекс": "Vx", "Часть Фортуны": "X"
        }
        

        self.ASPECT_SYMBOLS = {
            "соединение": "☌", "оппозиция": "☍", "трин": "△", "квадрат": "□",
            "секстиль": "⚹", "квинконс": "⚻", "полуквадрат": "∠", "биквинтиль": "bQ",
            "новиль": "N", "септиль": "S", "полусекстиль": "⚺", "квинтиль": "Q", "полутороквадрат": "∠∠"
        }    
    def get_major_orb(self, obj1, obj2, aspect_type):
        return 8  # Фиксированный орбис 8 градусов для мажорных аспектов
    
    def get_minor_orb(self, obj1, obj2, aspect_type):
        return 4 # Фиксированный орбис 4 градуса для минорных аспектов
    
    def get_major_aspects(self):
        return self.get_aspects(self.major_aspects, self.get_major_orb)
    
    def get_minor_aspects(self):
        return self.get_aspects(self.minor_aspects, self.get_minor_orb)
    
    def get_aspects(self, aspects_dict, orb_function):
        aspects = []
        checked_pairs = set()

        for obj1, pos1 in self.objects.items():
            for obj2, pos2 in self.objects.items():
                if obj1 == obj2 or (obj1, obj2) in checked_pairs or (obj2, obj1) in checked_pairs:
                    continue
                checked_pairs.add((obj1, obj2))

                if not isinstance(pos1, (int, float)) or not isinstance(pos2, (int, float)):
                    continue

                angle_diff = min(abs(pos1 - pos2), 360 - abs(pos1 - pos2))

                for aspect_name, (aspect_angle, _, symbol) in aspects_dict.items():
                    orb_value = orb_function(obj1, obj2, aspect_name)

                    if aspect_angle - orb_value <= angle_diff <= aspect_angle + orb_value:
                        actual_orb = abs(angle_diff - aspect_angle)
                        aspect_str = f"    {symbol} {obj1} {aspect_name.lower()} {obj2} орбис: {self.format_orb(actual_orb)}"
                        aspects.append(aspect_str.strip())

        return aspects
    
    def get_all_aspects(self):
        major_aspects = self.get_major_aspects()
        minor_aspects = self.get_minor_aspects()

        formatted_major_aspects = "\n".join(major_aspects) if major_aspects else "    Нет мажорных аспектов"
        formatted_minor_aspects = "\n".join(minor_aspects) if minor_aspects else "    Нет минорных аспектов"

        return f"**Мажорные аспекты:**\n{formatted_major_aspects}\n\n**Минорные аспекты:**\n{formatted_minor_aspects}"
    def get_all_aspects_structured(self) -> dict:
        """
        Возвращает мажорные и минорные аспекты в виде словаря для фронтенда:
        {
            "major": [...],
            "minor": [...]
        }
        """
        major_aspects = self.get_major_aspects()
        minor_aspects = self.get_minor_aspects()

        return {
            "major": major_aspects if major_aspects else ["Нет мажорных аспектов"],
            "minor": minor_aspects if minor_aspects else ["Нет минорных аспектов"]
        }

    def get_all_aspects_flat(self):
        """
        Возвращает все аспекты (мажорные + минорные) в виде плоского текста без заголовков.
        """
        major_aspects = self.get_major_aspects() or []
        minor_aspects = self.get_minor_aspects() or []
        return "\n".join(major_aspects + minor_aspects)


    def format_orb(self, orb):
        degrees = int(orb)
        minutes = round((orb - degrees) * 60)
        if minutes == 60:
            degrees += 1
            minutes = 0
        return f"{degrees}° {minutes}'"

    def convert_aspects_to_symbols(self, aspect_list):
        """
        Преобразует список аспектов в символическую форму и формирует словарь.
        Возвращает: dict {объект: [(аспект, другая планета)]}
        """
        aspects_dict = {}
        symbol_to_name = {v: k for k, v in self.ASPECT_SYMBOLS.items()}
        all_aspect_symbols = set(symbol_to_name.keys())

        excluded_objects = {"AS", "DS", "MC", "IC", "Vx", "⚸", "W", "X", "☋", "☊"}
        used_symbols = set()

        for aspect in aspect_list:
            if "орбис" in aspect:
                aspect = aspect.split("орбис")[0].strip()

            # Поиск символа аспекта — сортируем по длине (сначала длинные вроде ∠∠, bQ)
            aspect_symbol = None
            for symbol in sorted(all_aspect_symbols, key=len, reverse=True):
                if f"{symbol} " in aspect:
                    aspect_symbol = symbol
                    break

            if not aspect_symbol:
                print(f"⚠ Не найден символ в строке: {aspect}")
                continue

            try:
                cleaned = aspect.replace(aspect_symbol, "").strip()
                parts = cleaned.split()
                aspect_name = symbol_to_name[aspect_symbol]

                if aspect_name not in parts:
                    print(f"⚠ Не найдено слово '{aspect_name}' в: {aspect}")
                    continue

                idx = parts.index(aspect_name)
                obj1_name = " ".join(parts[:idx])
                obj2_name = " ".join(parts[idx + 1:])

                if not obj1_name or not obj2_name:
                    print(f"⚠ Не удалось извлечь объекты из: {aspect}")
                    continue

                obj1_sym = self.KNOWN_OBJECTS.get(obj1_name, obj1_name)
                obj2_sym = self.KNOWN_OBJECTS.get(obj2_name, obj2_name)

                result = f"{obj1_sym} {aspect_symbol} {obj2_sym}"
        

                used_symbols.add(aspect_symbol)

                if obj1_sym in excluded_objects or obj2_sym in excluded_objects:
           
                    continue

                if obj1_sym not in aspects_dict:
                    aspects_dict[obj1_sym] = []
                aspects_dict[obj1_sym].append((aspect_symbol, obj2_sym))

            except Exception as e:
                print(f"❌ Ошибка при обработке: {aspect} → {e}")

        if not aspects_dict:
            print("❌ ВНИМАНИЕ: Нет сконвертированных аспектов!")


        return aspects_dict
    def convert_aspects_for_chart(self, aspect_list):
        """
        Преобразует список аспектов в символическую форму и формирует список для визуализации.
        Возвращает: list из словарей {'from_body': символ, 'to_body': символ, 'aspect': символ}
        """
        aspects_result = []
        symbol_to_name = {v: k for k, v in self.ASPECT_SYMBOLS.items()}
        all_aspect_symbols = set(symbol_to_name.keys())

        # Итерируем по каждому аспекту
        for aspect in aspect_list:
            if "орбис" in aspect:
                aspect = aspect.split("орбис")[0].strip()

            # Поиск символа аспекта — сортируем по длине (сначала длинные вроде ∠∠, bQ)
            aspect_symbol = None
            for symbol in sorted(all_aspect_symbols, key=len, reverse=True):
                if f"{symbol} " in aspect:
                    aspect_symbol = symbol
                    break

            if not aspect_symbol:
                print(f"⚠ Не найден символ в строке: {aspect}")
                continue

            try:
                cleaned = aspect.replace(aspect_symbol, "").strip()
                parts = cleaned.split()
                aspect_name = symbol_to_name[aspect_symbol]

                if aspect_name not in parts:
                    print(f"⚠ Не найдено слово '{aspect_name}' в: {aspect}")
                    continue

                idx = parts.index(aspect_name)
                obj1_name = " ".join(parts[:idx])
                obj2_name = " ".join(parts[idx + 1:])

                if not obj1_name or not obj2_name:
                    print(f"⚠ Не удалось извлечь объекты из: {aspect}")
                    continue

                # Получаем символы объектов из KNOWN_OBJECTS
                obj1_sym = self.KNOWN_OBJECTS.get(obj1_name, obj1_name)
                obj2_sym = self.KNOWN_OBJECTS.get(obj2_name, obj2_name)

                # Формируем запись с правильными ключами
                aspects_result.append({
                    "from_body": obj1_sym,
                    "to_body": obj2_sym,
                    "aspect": aspect_symbol
                })

            except Exception as e:
                print(f"❌ Ошибка при парсинге аспекта: {aspect} → {e}")

        return aspects_result
