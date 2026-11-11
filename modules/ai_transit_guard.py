"""Detect explicit requests for chart facts that require dated transit data."""
import re

from fastapi import HTTPException


TRANSIT_UNAVAILABLE = (
    "Lunaria пока не рассчитывает транзиты. Могу объяснить, как они работают, "
    "или разобрать положения в натальной карте."
)

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
    r"sun|moon|mercury|venus|mars|jupiter|saturn|uranus|neptune|pluto|planets?|nodes?)\b"
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


def requires_transit_data(question: str) -> bool:
    """Keep general questions about transits and explicitly natal requests in chat."""
    for sentence in _SENTENCE_END.split(question.casefold()):
        dated = _TIME.search(sentence)
        asks_position = _PLACEMENT.search(sentence)
        if _TRANSIT.search(sentence):
            if _TRANSIT_REQUEST.search(sentence):
                return True
            if dated and (asks_position or _DATED_TRANSIT_REQUEST.search(sentence)):
                return True
            if asks_position and _TRANSITING.search(sentence) and _BODY.search(sentence):
                return True
        if dated and asks_position and _BODY.search(sentence) and not _NATAL.search(sentence):
            return True
    return False


def require_supported_question(question: str) -> None:
    if requires_transit_data(question):
        raise HTTPException(status_code=422, detail=TRANSIT_UNAVAILABLE)
