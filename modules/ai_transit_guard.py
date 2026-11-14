"""Detect requests for dated sky data and decide, before the quota, whether the chat can answer them.

Calculated: transits of Jupiter..Pluto to a verified chart for 12 months (TRANSITS-1 DESIGN §4).
"""
import calendar
import re
from datetime import date, timedelta

from fastapi import HTTPException

from modules import transits


TRANSITS_LEGACY_CHART = (
    "Для этой карты транзиты не считаются: она построена в старой версии Lunaria, где часовой пояс "
    "рождения не проверялся. Постройте карту заново, выбрав место рождения из подсказок, и спросите "
    "о транзитах в её чате."
)
TRANSITS_RELATIONSHIP = (
    "В разборе пары транзиты не считаются. Спросите о транзитах в чате своей карты — там они "
    "рассчитаны на 12 месяцев вперёд."
)
TRANSITS_NOT_CALCULATED = (
    "Lunaria считает транзиты Юпитера, Сатурна, Урана, Нептуна и Плутона к натальной карте на "
    "12 месяцев вперёд. Положения Солнца, Луны, Меркурия, Венеры и Марса на дату и переходы планет "
    "в другой знак пока не считаются — спросите о транзитах к карте."
)
_MONTHS_GENITIVE = ("января", "февраля", "марта", "апреля", "мая", "июня",
                    "июля", "августа", "сентября", "октября", "ноября", "декабря")


def transits_beyond_horizon(last_day: date) -> str:
    return (f"Транзиты рассчитаны с сегодняшнего дня по {last_day.day} {_MONTHS_GENITIVE[last_day.month - 1]} "
            f"{last_day.year} года. Спросите о периоде в эти сроки.")


_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+|\n+")
_TRANSIT = re.compile(r"\b(?:транзит\w*|transits?|transiting)\b")
_TRANSITING = re.compile(r"\b(?:транзитн\w+|transiting)\b")
_TRANSIT_REQUEST = re.compile(
    r"\b(?:рассчита\w*|посчита\w*|покаж\w*|постро\w*|розраху\w*|порах\w*|"
    r"побуду\w*|calculate|compute|show|find)\b|"
    r"\b(?:мо[ийяеюх]|наш\w*|мій|мої|my|our)\s+(?:транзит\w*|transits?)\b|"
    r"\b(?:транзит\w*)\s+у\s+(?:меня|нас|мене)\b|"
    r"\b(?:когда|коли|when)\s+(?:будет|начн[её]тся|закончится|буде|will)\b"
)
_DATED_TRANSIT_REQUEST = re.compile(
    r"^\s*(?:транзит\w*|transits?)\b|"
    r"\b(?:какой|какие|які|який|what|which|как|як|how)\s+"
    r"(?:[\w-]+\s+){0,4}(?:транзит\w*|transits?)\b"
)
_BODY = re.compile(
    r"\b(?:солнц\w*|сонц\w*|лун[ауыеой]|місяц\w*|меркур\w*|венер\w*|марс\w*|юпітер\w*|"
    r"юпитер\w*|сатурн\w*|уран\w*|нептун\w*|плутон\w*|планет\w*|уз[её]л|узл\w*|вуз[оі]л|вузл\w*|"
    r"хирон\w*|хірон\w*|"
    r"sun|moon|mercury|venus|mars|jupiter|saturn|uranus|neptune|pluto|chiron|planets?|nodes?)\b"
)
_PLACEMENT = re.compile(
    r"\b(?:где|де|когда|коли|where|when|положени\w*|положенн\w*|"
    r"градус\w*|координат\w*|position\w*|degrees?|"
    r"в каком (?:знаке|доме)|какой знак|у якому (?:знаку|домі)|який знак|(?:which|what) (?:sign|house)|"
    r"находит\w*|перебува\w*|ид[её]т|йде|войд[её]т|перейд[её]т|увійде|перейде|enters?|moves? into)\b"
)
_TIME = re.compile(
    r"\b(?:сейчас|сегодня|вчера|завтра|послезавтра|зараз|сьогодні|вчора|післязавтра|"
    r"now|currently|today|tonight|yesterday|tomorrow|as of|"
    r"на (?:эту|ту|какую) дату|на (?:цю|ту|яку) дату|on (?:this|that) date|"
    r"в этом (?:месяце|году)|в наступному місяці|this (?:week|month|year)|"
    r"в следующем (?:месяце|году)|next (?:week|month|year)|"
    r"(?:19|20|21)\d{2}|"
    r"\d{1,2}[./]\d{1,2}[./]\d{2,4}|"
    r"\d{1,2}\s+(?:январ\w*|феврал\w*|март\w*|апрел\w*|мая|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|"
    r"січн\w*|лютого|березн\w*|квітн\w*|травн\w*|червн\w*|липн\w*|"
    r"серпн\w*|вересн\w*|жовтн\w*|листопад\w*|грудн\w*|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}|"
    r"войд[её]т|перейд[её]т|увійде|перейде|will enter|will move into)\b"
)
_NATAL = re.compile(
    r"\b(?:натальн\w*|natal|в моей карте|у моїй карті|in my chart|birth chart|"
    r"при рождении|в момент рождения|на момент рождения|at birth|при народженні)\b"
)


# Bodies with calculated transits; any other named body is not calculated on a date.
_SLOW_BODY = re.compile(r"юпитер|юпітер|jupiter|сатурн|saturn|уран|uranus|нептун|neptune|плутон|pluto")
_GENERAL_BODY = re.compile(r"планет|planet")
# In a sentence about transits or aspects, a body after a possessive is the natal point a transit aims at
# («транзиты к моему Солнцу»), not the transiting body; elsewhere («где сейчас моя Луна») it is the body asked about.
_NATAL_POINT_BEFORE = re.compile(r"\b(?:мо[йяеёюиєї]\w*|мій|my|natal|натальн\w*)\s+$")
_INGRESS = re.compile(r"\b(?:войд[её]т|перейд[её]т|увійде|перейде|enters?|will enter|moves? into)\b")
_ASPECT_WORD = re.compile(
    r"\b(?:квадрат\w*|трин\w*|оппозиц\w*|опозиц\w*|соединени\w*|з['’]єднан\w*|секстил\w*|аспект\w*|"
    r"squares?|trines?|opposition|conjunction|sextile|aspects?)\b"
)
_BIRTH = re.compile(r"родил|рожд|born|народи|народж")
_MONTHS = (
    r"январ\w*|січ\w*|january", r"феврал\w*|лют\w*|february", r"март\w*|берез\w*|march",
    r"апрел\w*|квіт\w*|april", r"ма[йяе]|трав\w*|may", r"июн\w*|черв\w*|june",
    r"июл\w*|лип\w*|july", r"август\w*|серп\w*|august", r"сентябр\w*|верес\w*|september",
    r"октябр\w*|жовт\w*|october", r"ноябр\w*|листопад\w*|november", r"декабр\w*|груд\w*|december",
)
_MONTH = "(" + "|".join(_MONTHS) + ")"
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})\b")
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})\s+" + _MONTH + r"\s+(\d{4})\b")
_MONTH_DAY_YEAR = re.compile(r"\b" + _MONTH + r"\s+(\d{1,2}),?\s+(\d{4})\b")
_MONTH_YEAR = re.compile(r"\b" + _MONTH + r"\s+(\d{4})\b")
_YEAR = re.compile(r"\b((?:19|20|21)\d{2})\b")
_IN_YEARS = re.compile(
    r"\b(?:через|in)\s+(\d+|два|две|дві|три|четыре|чотири|пять|п['’]ять|шесть|шість|семь|сім|восемь|вісім|"
    r"девять|дев['’]ять|десять|пару|несколько|кілька|two|three|four|five|six|seven|eight|nine|ten|several)"
    r"\s+(?:лет|года|рок\w*|years)\b"
)


def _sentences(question):
    return _SENTENCE_END.split(question.casefold())


def _needs_dated_data(sentence):
    dated = _TIME.search(sentence) or _MONTH_YEAR.search(sentence)   # month + any 4-digit year, e.g. «январе 0000»
    asks_position = _PLACEMENT.search(sentence)
    if _TRANSIT.search(sentence):
        if _TRANSIT_REQUEST.search(sentence):
            return True
        if dated and (asks_position or _DATED_TRANSIT_REQUEST.search(sentence)):
            return True
        if asks_position and _TRANSITING.search(sentence) and _BODY.search(sentence):
            return True
    return bool(dated and asks_position and _BODY.search(sentence) and not _NATAL.search(sentence))


def requires_transit_data(question: str) -> bool:
    """Keep general questions about transits and explicitly natal requests in chat."""
    return any(_needs_dated_data(sentence) for sentence in _sentences(question))


def _month_number(word):
    return next(i + 1 for i, pattern in enumerate(_MONTHS) if re.fullmatch(pattern, word))


def _date_outside(sentence, hz):
    """True if the sentence names a date, month, year or "in N years" outside today-1 .. last_day+1."""
    if _BIRTH.search(sentence):
        return False
    first, last = hz.today - timedelta(days=1), hz.last_day + timedelta(days=1)
    found = []

    def full_date(year, month, day):
        if not 1 <= year <= 9999:      # no such calendar year (e.g. 0000): outside the range
            found.append(True)
            return " "
        try:
            found.append(not first <= date(year, month, day) <= last)
        except ValueError:      # not a calendar date, e.g. 31.02
            pass
        return " "

    sentence = _NUMERIC_DATE.sub(
        lambda m: full_date(int(m[3]) + (2000 if len(m[3]) == 2 else 0), int(m[2]), int(m[1])), sentence)
    sentence = _DAY_MONTH_YEAR.sub(lambda m: full_date(int(m[3]), _month_number(m[2]), int(m[1])), sentence)
    sentence = _MONTH_DAY_YEAR.sub(lambda m: full_date(int(m[3]), _month_number(m[1]), int(m[2])), sentence)
    for match in _MONTH_YEAR.finditer(sentence):
        year, month = int(match[2]), _month_number(match[1])
        if not 1 <= year <= 9999:
            found.append(True)
            continue
        found.append(date(year, month, calendar.monthrange(year, month)[1]) < first or date(year, month, 1) > last)
    sentence = _MONTH_YEAR.sub(" ", sentence)
    found += [not hz.today.year <= int(year) <= hz.last_day.year for year in _YEAR.findall(sentence)]
    found += [not m[1].isdigit() or int(m[1]) >= 2 for m in _IN_YEARS.finditer(sentence)]
    return any(found)


def _answerable(sentence):
    """A slow body, the planets in general or no body is named, and it is not a sign or house ingress."""
    if _INGRESS.search(sentence) and not _ASPECT_WORD.search(sentence):
        return False
    targeted = _TRANSIT.search(sentence) or _ASPECT_WORD.search(sentence)
    named = [m[0] for m in _BODY.finditer(sentence)
             if not (targeted and _NATAL_POINT_BEFORE.search(sentence[:m.start()]))]
    return not named or any(_SLOW_BODY.match(word) or _GENERAL_BODY.match(word) for word in named)


def require_supported_question(question: str) -> None:
    """Pair chat: no transits there (DECISIONS №25)."""
    if requires_transit_data(question):
        raise HTTPException(status_code=422, detail=TRANSITS_RELATIONSHIP)


def require_supported_chart_question(question: str, chart, now=None) -> None:
    """Chart chat, before usage.reserve: answerable transit questions pass, the rest get an honest refusal."""
    flagged = [sentence for sentence in _sentences(question) if _needs_dated_data(sentence)]
    if not flagged:
        return
    zone = transits.verified_zone(chart)
    if zone is None:
        raise HTTPException(status_code=422, detail=TRANSITS_LEGACY_CHART)
    hz = transits.horizon(zone, now or transits.server_now())
    outside = [_date_outside(sentence, hz) for sentence in flagged]
    if any(not out and _answerable(sentence) for sentence, out in zip(flagged, outside)):
        return
    if any(outside):
        raise HTTPException(status_code=422, detail=transits_beyond_horizon(hz.last_day))
    raise HTTPException(status_code=422, detail=TRANSITS_NOT_CALCULATED)
