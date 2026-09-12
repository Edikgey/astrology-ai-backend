"""One local-birth-time -> UTC conversion shared by every calculator entry point."""
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import HTTPException


def resolve_birth_time(data):
    if not data.timezone:
        raise HTTPException(422, detail={'code':'BIRTH_TIMEZONE_REQUIRED',
            'message':'Выберите место рождения из подсказок, чтобы определить часовой пояс.'})
    try:
        zone = ZoneInfo(data.timezone)
        if data.minute is not None:
            if data.hour != int(data.hour):
                raise ValueError('Use integer hour with separate minute')
            minutes = int(data.hour) * 60 + data.minute
        else:
            # Backward-compatible decimal hour for API clients supplying a timezone.
            minutes = round(data.hour * 60)
            if abs(data.hour * 60 - minutes) > 1e-7:
                raise ValueError('Birth time must have minute precision')
        local = datetime(data.year, data.month, data.day, minutes // 60, minutes % 60)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise HTTPException(422, detail='Некорректная дата, время или IANA timezone рождения.') from None
    candidates = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(utc_timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local and utc not in [c[1] for c in candidates]:
            candidates.append((fold, utc, aware.strftime('%z')))
    if not candidates:
        raise HTTPException(422, detail={'code':'NONEXISTENT_BIRTH_TIME',
            'message':'Такого местного времени не было из-за перевода часов. Уточните время рождения.'})
    if len(candidates) == 2 and data.time_fold is None:
        raise HTTPException(422, detail={'code':'AMBIGUOUS_BIRTH_TIME',
            'message':'Это время встречалось дважды при переводе часов. Выберите вариант по UTC-смещению.',
            'options':[{'fold':fold,'offset':offset} for fold, _, offset in candidates]})
    selected = next((c for c in candidates if c[0] == data.time_fold), candidates[0])
    return minutes / 60, selected[1].replace(tzinfo=None)


def calculator_args(chart):
    utc = getattr(chart, 'birth_utc', None)
    if utc is not None:
        hour = utc.hour + utc.minute / 60 + utc.second / 3600
        return utc.year, utc.month, utc.day, hour, chart.lon, chart.lat
    # Legacy provenance is unknown. Preserve the former calculation instant, never
    # guess a timezone or apply a second conversion to existing records.
    return chart.year, chart.month, chart.day, chart.hour, chart.lon, chart.lat
