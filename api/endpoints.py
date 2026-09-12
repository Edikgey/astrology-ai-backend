from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, Response
from sqlalchemy.orm import Session
from database.connection import get_db
from modules.ephemeris import Ephemeris, HOUSE_SYSTEM_NAME
from modules.birth_time import calculator_args, resolve_birth_time
from modules.aspects import Aspects
from modules.patterns import AstrologicalPatterns
from models.natal_chart import NatalChartResponse, NatalChartCreate, GPTInterpretationRequest, GPTInterpretationResponse, ChartIdRequest, GPTMessageResponse, PatternResponse
from api.auth import get_current_user, get_current_user_or_guest
from models.natal_chart import NatalChartListResponse
from database.queries import User, ChartInterpretationData, NatalChart, ChartData, GPTMessage, GPTConversation
from modules.interpretation import ChartInterpreter
from uuid import UUID
from modules.chart_limits import saved_chart_limit
from modules import usage
import asyncio


router = APIRouter()

import numpy as np

def clean_json_data(data):
    if isinstance(data, dict):
        return {str(k): clean_json_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [clean_json_data(item) for item in data]
    elif isinstance(data, (np.integer, np.intc, np.intp, np.int8, np.int16, np.int32, np.int64)):
        return int(data)
    elif isinstance(data, (np.float16, np.float32, np.float64, float)):
        return float(data)
    elif isinstance(data, (bool, np.bool_)):
        return bool(data)
    elif isinstance(data, (str, np.str_)):
        return str(data)
    else:
        return data

@router.post("/natal-chart", response_model=NatalChartResponse, tags=["Natal Chart"])
def create_natal_chart(
    chart_data: NatalChartCreate,
    request: Request,
    identity: dict = Depends(get_current_user_or_guest),
    db: Session = Depends(get_db)
):
    # FastAPI runs sync endpoints in its thread pool, including database lock waits.
    # End failed transactions here, before dependency cleanup releases the session.
    try:
        return _create_natal_chart(chart_data, identity, db)
    except Exception:
        db.rollback()
        raise


def _create_natal_chart(chart_data: NatalChartCreate, identity: dict, db: Session):
    if identity["user"] is not None:
        # Serialize authenticated creates until both the chart and its data commit.
        user_id = identity["user"].id
        user = usage.locked_user(db, user_id)
        limit = saved_chart_limit(user)
        used = db.query(NatalChart).filter(NatalChart.user_id == user_id).count()
        if used >= limit:
            raise HTTPException(status_code=409, detail={
                "code": "CHART_LIMIT_REACHED",
                "message": f"Можно сохранить максимум {limit} карт. Удалите одну из карт, чтобы создать новую.",
                "plan": user.plan,
                "used": used,
                "limit": limit,
            })

    local_hour, birth_utc = resolve_birth_time(chart_data)
    calculation = (birth_utc.year, birth_utc.month, birth_utc.day,
                   birth_utc.hour + birth_utc.minute / 60 + birth_utc.second / 3600,
                   chart_data.lon, chart_data.lat)

    # 1. Эфемериды
    ephem = Ephemeris(*calculation)
    body_data = ephem.get_all_bodies_with_degrees()

    # ⬇️ Генерируем points_data как список строк
    combined_bodies = ephem.get_all_bodies_with_symbols()
    points_data = []
    for body, info in combined_bodies.items():
        if isinstance(info, dict):
            retro = " (R)" if info.get("ретроградный", False) else ""
            sign = info.get("знак", "Нет данных")
            degree = info.get("градус", "Нет данных")
            house = f"({info.get('дом', 'Нет данных')} Дом)"
            line = f"{body}{retro}  {degree} {sign} {house}"
            points_data.append(line)

    if not body_data or not isinstance(body_data, dict):
        raise HTTPException(status_code=400, detail="❌ Ошибка: не получены данные по телам")

    # 2. Аспекты
    aspects_module = Aspects(*calculation)
    raw_aspect_list = aspects_module.get_all_aspects_flat().split("\n")
    raw_aspect_list = [line.strip() for line in raw_aspect_list if line.strip()]
    aspects_for_chart = aspects_module.convert_aspects_for_chart(raw_aspect_list)
    aspect_dict = aspects_module.convert_aspects_to_symbols(raw_aspect_list)
    aspects_structured = aspects_module.get_all_aspects_structured()

    # 3. Паттерны
    patterns = AstrologicalPatterns(
        aspect_dict,
        ephem.get_all_bodies()
    ).get_patterns_structured()

    # 4. Купсиды
    houses = ephem.get_house_cusps()

    # 🔐 5. Определяем владельца карты
    user_id = identity["user"].id if identity["user"] else None
    session_token = identity["session_token"]

    # 6. Сохраняем карту
    natal_chart = NatalChart(
        year=chart_data.year,
        month=chart_data.month,
        day=chart_data.day,
        hour=local_hour,
        timezone=chart_data.timezone,
        birth_utc=birth_utc,
        lon=chart_data.lon,
        lat=chart_data.lat,
        city=chart_data.city,
        region=chart_data.region,
        country=chart_data.country,
        user_id=user_id,
        session_token=session_token
    )
    db.add(natal_chart)
    db.flush()

    # 7. Сохраняем связанные данные
    chart_data_entry = ChartData(
        chart_id=natal_chart.id,
        bodies_for_circle=clean_json_data(body_data),
        aspects_for_circle=clean_json_data(aspects_for_chart),
        points_data=clean_json_data(points_data),
        patterns_data=clean_json_data(patterns),
        aspects_structured=clean_json_data(aspects_structured),
        houses=clean_json_data(houses),
        house_system=HOUSE_SYSTEM_NAME
    )

    db.add(chart_data_entry)
    chart_id = natal_chart.id
    db.commit()

    # 8. Возвращаем результат
    return {
        "chart_id": chart_id,
        "bodies_for_circle": body_data,
        "aspects_for_circle": aspects_for_chart,
        "points_data": points_data,
        "patterns_data": patterns,
        "aspects_structured": aspects_structured,
        "houses": houses, "timezone": chart_data.timezone, "birth_utc": birth_utc,
        "house_system": HOUSE_SYSTEM_NAME, "time_provenance": "local_iana"
    }
@router.get("/natal-chart/{chart_id}", response_model=NatalChartResponse, tags=["Natal Chart"])
def get_natal_chart_by_id(
    chart_id: int,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_current_user_or_guest)
):
    """
    Возвращает натальную карту по ID, если она принадлежит текущему пользователю или гостю.
    """
    user = identity["user"]
    session_token = identity["session_token"]

    chart = db.query(NatalChart).filter(NatalChart.id == chart_id).first()

    if not chart:
        raise HTTPException(status_code=404, detail="❌ Карта не найдена")

    # A user-owned chart can never be accessed through its guest token.
    if chart.user_id is not None:
        allowed = user is not None and user.id == chart.user_id
    else:
        allowed = session_token is not None and chart.session_token == session_token
    if not allowed:
        raise HTTPException(status_code=404, detail="❌ Карта не найдена")

    # Получаем все связанные данные
    chart_data = db.query(ChartData).filter_by(chart_id=chart.id).first()
    if not chart_data:
        raise HTTPException(status_code=404, detail="Нет данных для этой карты")

    return {
        "chart_id": chart.id,
        "bodies_for_circle": chart_data.bodies_for_circle,
        "aspects_for_circle": chart_data.aspects_for_circle,
        "points_data": chart_data.points_data,
        "patterns_data": chart_data.patterns_data,
        "aspects_structured": chart_data.aspects_structured,
        "houses": chart_data.houses if chart_data.houses is not None else Ephemeris(*calculator_args(chart)).get_house_cusps(),
        "timezone": chart.timezone, "birth_utc": chart.birth_utc,
        "house_system": chart_data.house_system or HOUSE_SYSTEM_NAME,
        "time_provenance": "local_iana" if chart.birth_utc and chart.timezone else "legacy_unverified",
    }


@router.get("/natal-charts", response_model=NatalChartListResponse, tags=["Natal Chart"])
def list_natal_charts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    charts = db.query(NatalChart).filter(
        NatalChart.user_id == current_user.id
    ).order_by(NatalChart.id.desc()).all()
    return {
        "charts": [{
            "chart_id": chart.id,
            "year": chart.year, "month": chart.month, "day": chart.day,
            "hour": chart.hour, "city": chart.city,
        } for chart in charts],
        "count": len(charts),
        "limit": saved_chart_limit(current_user),
    }


@router.delete("/natal-chart/{chart_id}", status_code=204, tags=["Natal Chart"])
def delete_natal_chart(
    chart_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Preserve legacy usage before deleting message history; use the same lock order.
    usage.locked_user(db, current_user.id)
    chart = db.query(NatalChart).filter(
        NatalChart.id == chart_id, NatalChart.user_id == current_user.id
    ).with_for_update().first()
    if chart is None:
        raise HTTPException(status_code=404, detail="❌ Карта не найдена")
    try:
        usage.backfill_user_history(db, current_user.id)
        for model in (GPTConversation, GPTMessage, ChartInterpretationData, ChartData):
            db.query(model).filter(model.chart_id == chart_id).delete(synchronize_session=False)
        db.query(NatalChart).filter(
            NatalChart.id == chart_id, NatalChart.user_id == current_user.id
        ).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(status_code=204)


from datetime import datetime

@router.post("/ask-gpt", response_model=GPTInterpretationResponse, tags=["Natal Chart"])
def ask_gpt_interpretation(
    data: GPTInterpretationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Получает интерпретацию натальной карты по ID.
    Доступна только авторизованному владельцу карты по JWT.
    """
    chart = db.query(NatalChart).filter_by(id=data.chart_id).first()
    if not chart:
        raise HTTPException(status_code=404, detail="❌ Натальная карта не найдена")

    if chart.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="⛔ Недоступно: карта не принадлежит пользователю")

    # Copy scalar values before reserve commits/expires the ORM instances.
    user_id, chart_id = current_user.id, chart.id
    chart_args = (chart.year, chart.month, chart.day, chart.hour, chart.lon, chart.lat)
    reservation_id = usage.reserve(db, user_id, chart_id)
    try:
        interpreter = ChartInterpreter(*chart_args, calculate=False)
        # Sync SQLAlchemy and lock waits stay in FastAPI's worker thread.
        interpretation = asyncio.run(interpreter.ask_gpt(chart_id, db, data.question, user_id))
        if not isinstance(interpretation, str) or not interpretation.strip():
            raise HTTPException(502, detail="GPT не вернул ответ. Квота не списана.")
        result = GPTInterpretationResponse(chart_id=chart_id, response=interpretation)
        usage.finalize(db, user_id, reservation_id, data.question, interpretation)
        return result
    except BaseException:
        usage.release(db, user_id, reservation_id)
        raise


@router.get("/account/usage", tags=["Account"])
def get_account_usage(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return usage.account_usage(db, current_user.id)

@router.get("/gpt-messages", response_model=List[GPTMessageResponse])
def get_gpt_messages(
    chart_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Получает список сообщений GPT, связанных с натальной картой.
    Доступ только авторизованному владельцу карты по JWT.
    """
    chart = db.query(NatalChart).filter_by(id=chart_id).first()
    if not chart:
        raise HTTPException(status_code=404, detail="❌ Натальная карта не найдена")

    if chart.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="⛔ Недоступно: карта не принадлежит пользователю")

    messages = db.query(GPTMessage).filter_by(chart_id=chart_id).order_by(GPTMessage.id).all()
    return messages
@router.post("/chart-bodies-info", tags=["Natal Chart"])
def get_bodies_info(
    data: ChartIdRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user_or_guest)
):
    chart = db.query(NatalChart).filter_by(id=data.chart_id, user_id=current_user.id).first()
    if not chart:
        raise HTTPException(status_code=404, detail="❌ Натальная карта не найдена")

    # Проверка: есть ли уже сохранённые данные
    chart_data = db.query(ChartData).filter_by(chart_id=chart.id).first()
    if chart_data and chart_data.points_data:
        return {"bodies": chart_data.points_data}

    # Если данных нет — считаем их
    ephem = Ephemeris(*calculator_args(chart))
    combined_bodies = ephem.get_all_bodies_with_symbols()

    result = []
    for body, info in combined_bodies.items():
        if isinstance(info, dict):
            retro = " (R)" if info.get("ретроградный", False) else ""
            sign = info.get("знак", "Нет данных")
            degree = info.get("градус", "Нет данных")
            house = f"({info.get('дом', 'Нет данных')} Дом)"
            line = f"{body}{retro}  {degree} {sign} {house}"
            result.append(line)

    # Сохраняем в chart_data.points_data
    if chart_data:
        chart_data.points_data = result
    else:
        chart_data = ChartData(
            chart_id=chart.id,
            bodies_for_circle=None,
            aspects_for_circle=None,
            points_data=result
        )
        db.add(chart_data)

    db.commit()

    return {"bodies": result}
@router.post("/get-patterns", response_model=List[PatternResponse], tags=["Natal Chart"])
async def get_patterns(
    data: ChartIdRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_or_guest)
):
    """
    Эндпоинт для получения структурированных астрологических паттернов по заданной натальной карте.
    """
    # Получаем натальную карту пользователя
    chart = db.query(NatalChart).filter_by(id=data.chart_id, user_id=current_user.id).first()
    if not chart:
        raise HTTPException(status_code=404, detail="❌ Натальная карта не найдена или не принадлежит пользователю")

    # Проверяем, есть ли уже сохраненные паттерны в таблице
    chart_data = db.query(ChartData).filter_by(chart_id=chart.id).first()
    if chart_data and chart_data.patterns_data:
        # Возвращаем сохраненные паттерны, если они уже есть
        return chart_data.patterns_data

    # Создаём объект для работы с аспектами
    aspects_module = Aspects(*calculator_args(chart))
    raw_aspect_list = aspects_module.get_all_aspects_flat().split("\n")
    raw_aspect_list = [line.strip() for line in raw_aspect_list if line.strip()]
    aspect_dict = aspects_module.convert_aspects_for_chart(raw_aspect_list)
    
    # Создаём объект для получения тел с символами
    ephem = Ephemeris(*calculator_args(chart))
    bodies = ephem.get_all_bodies_with_symbols()

    # Создаём объект паттернов с телами и аспектами
    patterns = AstrologicalPatterns(aspect_dict, bodies)
    
    # Получаем структурированные паттерны
    structured_patterns = patterns.get_patterns_structured()

    # Сохраняем паттерны в таблицу ChartData
    if chart_data:
        chart_data.patterns_data = structured_patterns
    else:
        chart_data = ChartData(
            chart_id=chart.id,
            bodies_for_circle=None,
            aspects_for_circle=None,
            points_data=None,
            patterns_data=structured_patterns  # Сохраняем паттерны
        )
        db.add(chart_data)

    db.commit()

    # Возвращаем структурированные паттерны
    return structured_patterns
