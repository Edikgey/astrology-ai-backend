import datetime
import math
import numpy as np
import swisseph as swe
from astroquery.jplhorizons import Horizons
import ephem
from modules.cusp_math import house_for_longitude



HOUSE_SYSTEM_CODE = b'P'
HOUSE_SYSTEM_NAME = "Placidus"

ZODIAC_SIGNS = [
    "Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева",
    "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"
]

class Ephemeris:
    def __init__(self, year, month, day, hour, lon, lat):
        """
        Инициализация объекта для вычислений.
        :param year: Год рождения
        :param month: Месяц рождения
        :param day: День рождения
        :param hour: Час (UTC)
        :param lon: Долгота (местоположение)
        :param lat: Широта (местоположение)
        """
        self.year = year
        self.month = month
        self.day = day
        self.hour = hour  # 📌 Сохраняем дату рождения
        self.jd = swe.julday(year, month, day, hour)  # Получаем Юлианскую дату
        self.lon, self.lat = lon, lat  # Сохраняем координаты
        self.flags = swe.FLG_SWIEPH | swe.FLG_SPEED  # Флаги для расчётов
    

    def get_planets(self):
        """ Получает координаты планет и узлов + знаки, градусы и дома по системе Плацидуса """
        bodies = {
            "Солнце": swe.SUN, "Луна": swe.MOON, "Меркурий": swe.MERCURY, "Венера": swe.VENUS,
            "Марс": swe.MARS, "Юпитер": swe.JUPITER, "Сатурн": swe.SATURN,
            "Уран": swe.URANUS, "Нептун": swe.NEPTUNE, "Плутон": swe.PLUTO,
            "Северный Узел": swe.TRUE_NODE  # Добавляем Северный Узел
        }

        results = {}
        houses = self.get_houses()  # Получаем границы домов Плацидуса

        for body, id in bodies.items():
            pos, _ = swe.calc_ut(self.jd, id, self.flags)  
            speed = pos[3]  # Берем скорость движения тела

            sign, degree_str = self._get_sign_and_degree(pos[0])  
            parts = degree_str.split("°")
            if len(parts) < 2:
                raise ValueError(f"Ошибка в разборе градуса: {degree_str}")

            degree = float(parts[0]) + float(parts[1].strip("'")) / 60

            # ✅ Определяем, является ли объект ретроградным
            retro = True if body in ["Северный Узел", "Южный Узел"] else speed < 0  

            house = self._find_house(pos[0], sign, houses, is_key_point=True)

            results[body] = {
                "знак": sign,
                "градус": degree_str,
                "ретроградный": retro,
                "дом": house,
                "абс_долгота": pos[0] 
            }

        # 🔹 Южный Узел = Северный Узел + 180°
        north_abs_long = results["Северный Узел"]["абс_долгота"]
        south_node_long = (north_abs_long + 180) % 360

        south_sign, south_degree_str = self._get_sign_and_degree(south_node_long)
        results["Южный Узел"] = {
            "знак": south_sign,
            "градус": south_degree_str,
            "ретроградный": True,
            "дом": self._find_house(south_node_long, south_sign, houses, is_key_point=True),
            "абс_долгота": south_node_long
        }


        return results

    def get_planet_longitudes(self):
        """ Получает долготу (эклиптические координаты) планет напрямую из Swiss Ephemeris """
        bodies = {
            "Солнце": swe.SUN, "Луна": swe.MOON, "Меркурий": swe.MERCURY, "Венера": swe.VENUS,
            "Марс": swe.MARS, "Юпитер": swe.JUPITER, "Сатурн": swe.SATURN,
            "Уран": swe.URANUS, "Нептун": swe.NEPTUNE, "Плутон": swe.PLUTO,
            "Северный Узел": swe.TRUE_NODE  # Добавляем Северный Узел
        }

        longitudes = {}

        for body, id in bodies.items():
            pos, _ = swe.calc_ut(self.jd, id, self.flags)  
            longitude = pos[0]  # Берем долготу

            longitudes[body] = longitude  # Сохраняем долготу в результат

        # 🔹 **Добавляем Южный Узел**
        longitudes["Южный Узел"] = (longitudes["Северный Узел"] + 180) % 360  # 180° от Северного Узла

        return longitudes


    def calculate_selena(self):
        """Рассчитывает Белую Луну (Селену) как антипод Чёрной Луны (Лилит)"""
        
        # 🔹 Рассчитываем Чёрную Луну Лилит (Средний Лунный Апогей)
        lilith_pos, _ = swe.calc_ut(self.jd, swe.MEAN_APOG, self.flags)

        # 🔹 Белая Луна (Селена) – это 180° от Лилит
        selena_pos = (lilith_pos[0] + 180) % 360

        return selena_pos
    def get_asteroids(self):
        """ Получает координаты астероидов (⚸ ⚳ ⚵ ⚴ ⚶ ⚷ φ W) + знаки и дома по системе Плацидуса """
        
        # 🔹 **Используем JPL Horizons для астероидов**
        jpl_asteroids = {
            "Церера": "1", "Паллада": "2", "Юнона": "3", "Веста": "4",
            "Хирон": "2060", "Фол": "5145"
        }

        results = {}
        houses = self.get_houses()  # Загружаем границы домов Плацидуса

        for name, spk_id in jpl_asteroids.items():
            try:
                obj = Horizons(id=spk_id, location='500', epochs=self.jd, id_type='smallbody')
                eph = obj.ephemerides()

                sign, degree_str = self._get_sign_and_degree(eph["ObsEclLon"][0])  # Определяем знак
                parts = degree_str.split("°")

                if len(parts) < 2:
                    raise ValueError(f"Ошибка в разборе градуса: {degree_str}")

                degree = float(parts[0]) + float(parts[1].strip("'")) / 60
                lat = eph["ObsEclLat"][0]  # Эклиптическая широта

                # ✅ Определяем дом
                house = self._find_house(eph["ObsEclLon"][0], sign, houses, is_key_point=True)

                # 🔹 **Корректно извлекаем скорость**
                retrograde = eph["RA_rate"][0] < 0  # Используем правильный индекс

                results[name] = {
                    "знак": sign,
                    "градус": degree_str,
                    "ретроградный": retrograde,
                    "дом": house,
                    "абс_долгота": eph["ObsEclLon"][0]

                }
            except Exception as e:
                print(f"⚠️ Ошибка при получении {name} через JPL Horizons: {e}")
         # 🔹 **Рассчитываем Черную Луну Лилит (Средний Лунный Апогей)**
        lilith_pos, _ = swe.calc_ut(self.jd, swe.MEAN_APOG, self.flags)
        # 🔹 **Простой расчёт Белой Луны Селены как антипод Лилит**
        selena_pos = self.calculate_selena()

        
        sw_asteroids = {
            "Черная Луна Лилит": lilith_pos[0],  # Лилит (Средний Апогей)
            "Белая Луна Селена": selena_pos  # Белая Луна (основной или резервный метод)
        }

        for body, abs_degree in sw_asteroids.items():
            try:
                sign, degree_str = self._get_sign_and_degree(abs_degree)

                parts = degree_str.split("°")
                if len(parts) < 2:
                    raise ValueError(f"Ошибка в разборе градуса: {degree_str}")

                degree = float(parts[0]) + float(parts[1].strip("'")) / 60

                # ✅ Определяем дом
                house = self._find_house(abs_degree, sign, houses, is_key_point=True)

                # 🔥 **Исправление ретроградности Лилит**
                if body == "Черная Луна Лилит":
                    retrograde = False  # Средняя Лилит (MEAN_APOG) не бывает ретроградной
                elif body == "Белая Луна Селена":
                    retrograde = False  # Селена тоже никогда не ретроградная
                else:
                    retrograde = True  # Остальные тела могут быть ретроградными

                results[body] = {
                    "знак": sign,
                    "градус": degree_str,
                    "ретроградный": retrograde,
                    "дом": house,
                    "абс_долгота": abs_degree

                }
                
            except swe.Error:
                print(f"⚠️ Ошибка при расчёте {body}")

        return results
    
    def get_asteroid_longitudes(self):
        """ Получает долготу (эклиптические координаты) астероидов напрямую из JPL Horizons """
        
        # 🔹 **Список астероидов с их SPK ID**
        jpl_asteroids = {
            "Церера": "1", "Паллада": "2", "Юнона": "3", "Веста": "4",
            "Хирон": "2060", "Фол": "5145"
        }

        longitudes = {}

        for name, spk_id in jpl_asteroids.items():
            try:
                obj = Horizons(id=spk_id, location='500', epochs=self.jd, id_type='smallbody')
                eph = obj.ephemerides()

                longitude = eph["ObsEclLon"][0]  # Эклиптическая долгота

                longitudes[name] = longitude  # Сохраняем чистое значение долготы

            except Exception as e:
                print(f"⚠️ Ошибка при получении долготы {name} через JPL Horizons: {e}")

        return longitudes



    
    def _get_sign_and_degree(self, degree):
        """ Определяет знак Зодиака и градусы """
        sign_index = int(degree // 30)
        sign = ZODIAC_SIGNS[sign_index]
        deg = degree % 30
        minutes = (deg - int(deg)) * 60  # Минуты
        


        return sign, f"{int(deg)}° {int(minutes)}'"

    def get_houses(self):
        """ Вычисляет границы домов по системе Плацидуса и ключевые точки """
        cusps, ascmc = swe.houses(self.jd, self.lat, self.lon, HOUSE_SYSTEM_CODE)  # 'P' = Плацидус

        houses = {
            "Дом 1": cusps[0],
            "Дом 2": cusps[1],
            "Дом 3": cusps[2],
            "Дом 4": cusps[3],
            "Дом 5": cusps[4],
            "Дом 6": cusps[5],
            "Дом 7": cusps[6],
            "Дом 8": cusps[7],
            "Дом 9": cusps[8],
            "Дом 10": cusps[9],
            "Дом 11": cusps[10],
            "Дом 12": cusps[11]
        }


        return houses
    def get_house_cusps(self):
        houses = self.get_houses()
        return [{"symbol": str(i), "degree": houses[f"Дом {i}"]} for i in range(1, 13)]

    def _find_house(self, degree, sign, houses, is_key_point=False):
        """Определение дома для планет и ключевых точек по куспидам домов Плацидуса."""

        # 🔥 Для ключевых точек (AS, DS, MC, IC, X) используем абсолютную долготу напрямую
        if is_key_point:
            absolute_degree = degree  # Здесь знак не нужен!
        else:
            # 🔥 Таблица соответствия знаков зодиака их градусам
            zodiac_map = {
                "Овен": 0, "Телец": 30, "Близнецы": 60, "Рак": 90, "Лев": 120, "Дева": 150,
                "Весы": 180, "Скорпион": 210, "Стрелец": 240, "Козерог": 270, "Водолей": 300, "Рыбы": 330
            }

            # ✅ Получаем абсолютную долготу (0-360°)
            if isinstance(sign, str):
                base_degree = zodiac_map.get(sign)
                if base_degree is None:
                    raise ValueError(f"❌ Ошибка: знак '{sign}' не найден в таблице зодиака!")
            else:
                raise TypeError("❌ sign должен быть строкой!")

            absolute_degree = base_degree + degree  # Убираем % 360

        cusps = [houses[f"Дом {i}"] for i in range(1, 13)]
        return house_for_longitude(absolute_degree, cusps)


    def get_key_points(self):
        """ Получает ключевые точки гороскопа с их знаками, градусами, домами и абсолютной долготой """

        # ✅ Используем систему домов **Placidus**
        cusps, ascmc = swe.houses(self.jd, self.lat, self.lon, HOUSE_SYSTEM_CODE)
        houses = self.get_houses()  

        # ✅ Пересчитываем Солнце и Луну
        sun_pos, _ = swe.calc_ut(self.jd, swe.SUN, self.flags)
        moon_pos, _ = swe.calc_ut(self.jd, swe.MOON, self.flags)
        is_daytime_flag = self.is_daytime(sun_pos, ascmc)

        # ✅ Рассчитываем координаты ключевых точек
        vertex_longitude = self.calculate_vertex()
        part_of_fortune = self.calculate_part_of_fortune(ascmc, sun_pos, moon_pos, is_daytime_flag)

        raw_key_points = {
            "Асцендент": ascmc[0],
            "Средина Неба": ascmc[1],
            "Десцендент": (ascmc[0] + 180) % 360,
            "Надир": (ascmc[1] + 180) % 360,
            "Вертекс": vertex_longitude,
            "Часть Фортуны": part_of_fortune
        }

        key_points = {}
        for key, abs_degree in raw_key_points.items():
            sign, degree_str = self._get_sign_and_degree(abs_degree)
            is_key = key in {
                "Асцендент", "Десцендент", "Средина Неба", "Надир",
                "Часть Фортуны", "Вертекс",
            }
            house = self._find_house(abs_degree, sign, houses, is_key_point=is_key)

            key_points[key] = {
                "знак": sign,
                "градус": degree_str,
                "дом": house,
                "абс_долгота": abs_degree  # ← добавлено сюда
            }

        return key_points

    def get_key_point_longitudes(self):
        """ Получает долготу ключевых точек гороскопа (только числа) """

        # Основные оси (Асцендент, Десцендент, МС, IC)
        ascmc = swe.houses(self.jd, self.lat, self.lon, HOUSE_SYSTEM_CODE)[1]  # Асцендент и МС
        asc, mc, desc, ic = ascmc[0], ascmc[1], (ascmc[0] + 180) % 360, (ascmc[1] + 180) % 360

        # Долгота Солнца и Луны
        sun_pos = swe.calc_ut(self.jd, swe.SUN, self.flags)[0]
        moon_pos = swe.calc_ut(self.jd, swe.MOON, self.flags)[0]

        # Время суток (для расчета ЧФ)
        is_daytime_flag = self.is_daytime(sun_pos, ascmc)

        # Вычисляемые точки
        vertex = self.calculate_vertex()
        part_of_fortune = self.calculate_part_of_fortune(ascmc, sun_pos, moon_pos, is_daytime_flag)

        # 🔹 Чёрная Луна Лилит (Средний Лунный Апогей)
        lilith_pos, _ = swe.calc_ut(self.jd, swe.MEAN_APOG, self.flags)

        # 🔹 Белая Луна (Селена) – антипод Лилит
        selena = (lilith_pos[0] + 180) % 360  

        # Список ключевых точек и их долготы
        key_longitudes = {
            "Асцендент": asc,
            "Десцендент": desc,
            "Средина Неба": mc,
            "Надир": ic,
            "Вертекс": vertex,
            "Часть Фортуны": part_of_fortune,
            "Черная Луна Лилит": lilith_pos[0],
            "Белая Луна Селена": selena
        }

        return key_longitudes

    def is_daytime(self, sun_pos, ascmc):
        """ Определяет дневное или ночное рождение, основываясь на положении Солнца относительно MC """

        asc = ascmc[0]  # Асцендент (горизонт)
        mc = ascmc[1]  # MC (Средина неба)
        ic = (mc + 180) % 360  # Надир (IC)
        
        # Дневное рождение: если Солнце между MC и IC (верхняя полусфера)
        is_day = mc <= sun_pos[0] < ic

        return is_day



    def calculate_vertex(self):
        """Swiss Ephemeris returns Vertex at ascmc[3] for the actual birthplace.

        Do not replace latitude with 90-lat: that creates polar/out-of-range
        coordinates near the equator and throughout the southern hemisphere.
        """
        _, ascmc = swe.houses(self.jd, self.lat, self.lon, HOUSE_SYSTEM_CODE)
        return ascmc[3]



    def calculate_part_of_fortune(self, ascmc, sun_pos, moon_pos, is_daytime_flag):
        """ Рассчитывает Часть Фортуны (Pars Fortunae) с учетом времени рождения """
        
        if is_daytime_flag:
            part_of_fortune = (ascmc[0] + moon_pos[0] - sun_pos[0]) % 360
        else:
            part_of_fortune = (ascmc[0] + sun_pos[0] - moon_pos[0]) % 360

        return part_of_fortune



    def get_planet_houses(self):
        """ Определяет, в каком доме находится каждая планета """
        planet_positions = self.get_planets()
        for data in planet_positions.values():
            data["дом"] = f"{data['дом']} Дом"
        return planet_positions

    def get_moon_phase(self):
        """ Определяет фазу Луны и её угол на русском языке """
        pos_moon, _ = swe.calc_ut(self.jd, swe.MOON, self.flags)
        pos_sun, _ = swe.calc_ut(self.jd, swe.SUN, self.flags)

        phase_angle = (pos_moon[0] - pos_sun[0]) % 360  # Разница углов

        # Определение фазы
        if 0 <= phase_angle < 45:
            phase = "Новолуние"
        elif 45 <= phase_angle < 90:
            phase = "Растущий серп"
        elif 90 <= phase_angle < 135:
            phase = "Первая четверть"
        elif 135 <= phase_angle < 180:
            phase = "Растущая Луна"
        elif 180 <= phase_angle < 225:
            phase = "Полнолуние"
        elif 225 <= phase_angle < 270:
            phase = "Убывающая Луна"
        elif 270 <= phase_angle < 315:
            phase = "Последняя четверть"
        else:
            phase = "Старая Луна"

        return {"фаза": phase, "угол": phase_angle}
    def get_all_bodies(self) -> dict:
        """
        Возвращает тела с ключами-символами (☉, ☽ и т.д.), исключая фиктивные и ключевые точки.
        """
        body_symbols = {
            "Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀", "Марс": "♂",
            "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅", "Нептун": "♆", "Плутон": "♇",
            "Северный Узел": "☊", "Южный Узел": "☋", "Церера": "⚳", "Паллада": "⚴",
            "Юнона": "⚵", "Веста": "⚶", "Хирон": "⚷", "Фол": "φ",
            "Черная Луна Лилит": "⚸", "Белая Луна Селена": "W",
            "Асцендент": "AS", "Десцендент": "DS", "Средина Неба": "MC", "Надир": "IC",
            "Вертекс": "Vx", "Часть Фортуны": "X"
        }

        excluded = {
            "Асцендент", "Десцендент", "Средина Неба", "Надир",
            "Часть Фортуны", "Вертекс", "Черная Луна Лилит", "Белая Луна Селена",
            "Северный Узел", "Южный Узел"
        }

        all_bodies = {
            **self.get_planets(),
            **self.get_asteroids(),
            **self.get_key_points()
        }

        cleaned = {}
        for name, data in all_bodies.items():
            name_clean = name.replace(" (R)", "").strip()
            if name_clean not in excluded:
                symbol = body_symbols.get(name_clean, name_clean)
                cleaned[symbol] = data

        return cleaned


    def get_all_bodies_with_symbols(self) -> dict:
        """
        Возвращает все данные (планеты, астероиды, ключевые точки), добавляя знак из словаря перед названием.
        Ничего не исключает.
        """
        body_symbols = {
            "Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀", "Марс": "♂",
            "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅", "Нептун": "♆", "Плутон": "♇",
            "Северный Узел": "☊", "Южный Узел": "☋", "Церера": "⚳", "Паллада": "⚴",
            "Юнона": "⚵", "Веста": "⚶", "Хирон": "⚷", "Фол": "φ",
            "Черная Луна Лилит": "⚸", "Белая Луна Селена": "W",
            "Асцендент": "AS", "Десцендент": "DS", "Средина Неба": "MC", "Надир": "IC",
            "Вертекс": "Vx", "Часть Фортуны": "X"
        }

        all_bodies = {
            **self.get_planets(),
            **self.get_asteroids(),
            **self.get_key_points()
        }

        result = {}
        for name, data in all_bodies.items():
            name_clean = name.replace(" (R)", "").strip()
            symbol = body_symbols.get(name_clean, "")
            full_name = f"{symbol} {name}" if symbol else name
            result[full_name] = data

        return result
    def get_all_bodies_with_degrees(self) -> dict:
        print("🟢 Вызван метод get_all_bodies_with_degrees()")

        body_symbols = {
            "Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀", "Марс": "♂",
            "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅", "Нептун": "♆", "Плутон": "♇",
            "Северный Узел": "☊", "Южный Узел": "☋", "Церера": "⚳", "Паллада": "⚴",
            "Юнона": "⚵", "Веста": "⚶", "Хирон": "⚷", "Фол": "φ",
            "Черная Луна Лилит": "⚸", "Белая Луна Селена": "W",
            "Асцендент": "AS", "Десцендент": "DS", "Средина Неба": "MC", "Надир": "IC",
            "Вертекс": "Vx", "Часть Фортуны": "X"
        }

        all_bodies = {
            **self.get_planets(),
            **self.get_asteroids(),
            **self.get_key_points()
        }

        print(f"📊 Всего тел в all_bodies: {len(all_bodies)}")

        result = {}

        for name, data in all_bodies.items():
            name_clean = name.replace(" (R)", "").strip()
            symbol = body_symbols.get(name_clean, name_clean[:2])

            absolute_degree = data.get("абс_долгота")
            if absolute_degree is None:
                absolute_degree = data.get("degree")
            if absolute_degree is None:
                print(f"⚠️ Нет градуса у тела: {name}")
                continue

            sign, _ = self._get_sign_and_degree(absolute_degree)
            degrees_in_sign = absolute_degree % 30
            rounded = round(degrees_in_sign)
            rounded_degree = f"{rounded}°"

            house = data.get("дом") or data.get("house")
            retro = data.get("ретроградный", False)

            result[symbol] = {
                "symbol": symbol,
                "label": name_clean,
                "degree": absolute_degree,
                "sign": sign,
                "house": house,
                "retrograde": retro,
                "roundedDegree": rounded_degree
            }

            retro_text = " (R)" if retro else ""
            print(f"✅ {symbol}{retro_text} — {absolute_degree:.2f}° ({sign}, {rounded_degree}), дом {house}")

        return result

    def get_bodies_for_interpretation(self) -> dict:
        """
        Возвращает очищенные данные небесных тел для интерпретации:
        - Ключи: только символы тел (например, ☉, ☽, ♃)
        - Значения: знак, дом, ретроградность
        """
        body_symbols = {
            "Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀", "Марс": "♂",
            "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅", "Нептун": "♆", "Плутон": "♇",
            "Северный Узел": "☊", "Южный Узел": "☋", "Церера": "⚳", "Паллада": "⚴",
            "Юнона": "⚵", "Веста": "⚶", "Хирон": "⚷", "Фол": "φ",
            "Черная Луна Лилит": "⚸", "Белая Луна Селена": "W",
            "Асцендент": "AS", "Десцендент": "DS", "Средина Неба": "MC", "Надир": "IC",
            "Вертекс": "Vx", "Часть Фортуны": "X"
        }

        all_bodies = {
            **self.get_planets(),
            **self.get_asteroids(),
            **self.get_key_points()
        }

        result = {}
        for name, data in all_bodies.items():
            name_clean = name.replace(" (R)", "").strip()
            symbol = body_symbols.get(name_clean)
            if symbol:
                result[symbol] = {
                    "знак": data.get("знак"),
                    "дом": data.get("дом"),
                    "ретроградный": data.get("ретроградный", False)
                }

        return result


