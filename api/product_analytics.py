"""First-party collector. Strict consent leases; no browser SDK or arbitrary proxy."""
import json
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, ConfigDict, UUID4, ValidationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from api.auth import get_current_user, optional_oauth2_scheme
from database.connection import get_db
from modules.auth_origins import AUTH_ORIGINS
from modules import product_analytics as analytics
from modules.product_analytics_schema import ClientBatch, POLICY_VERSION

router = APIRouter(prefix='/analytics', tags=['Analytics'])


class ConsentBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy_version: Literal['posthog-v1']
    visitor_id: UUID4
    revision: UUID4
    action: Literal['accept', 'resume', 'withdraw', 'reset']


async def body(request, model):
    if request.headers.get('origin') not in AUTH_ORIGINS:
        raise HTTPException(403, 'Недопустимый источник аналитики.')
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json' or request.headers.get('content-encoding', 'identity') != 'identity':
        raise HTTPException(415, 'Требуется JSON.')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 16384:
            raise HTTPException(413, 'Запрос аналитики слишком большой.')
        raw.extend(chunk)
    try:
        return model.model_validate(json.loads(raw))
    except (ValueError, ValidationError, RecursionError):
        raise HTTPException(422, 'Недопустимое событие аналитики.') from None


def identity(token, db):
    # Never downgrade an invalid JWT to a guest analytics identity.
    return get_current_user(token, db).id if token else None


def ingest(db, batch, revision, user_id):
    consent = db.query(analytics.AnalyticsConsent).filter_by(visitor_key=analytics.visitor_key(batch.visitor_id)).with_for_update().first()
    if (not analytics.eligible(db, consent) or consent.revision != str(revision) or consent.user_id != user_id):
        raise HTTPException(403, 'Нет действующего согласия на аналитику.')
    accepted = 0
    for record in batch.events:
        accepted += analytics.enqueue(db, consent, record.name, record.properties, event_id=record.event_id,
            session_id=record.session_id, route_key=record.route_key, source='client',
            viewport_class=record.viewport_class, campaign_source=record.campaign_source)
    db.commit()
    return {'accepted': accepted}


def available(request):
    if not analytics.enabled():
        raise HTTPException(503, 'Аналитика отключена.')
    if not analytics.rate_allowed(request.client.host if request.client else None):
        raise HTTPException(429, 'Повторите позже.', headers={'Retry-After': '60'})


@router.post('/consent')
async def consent(request: Request, response: Response, token=Depends(optional_oauth2_scheme), db=Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    available(request)
    value = await body(request, ConsentBody)
    user_id = await run_in_threadpool(identity, token, db)
    try:
        return await run_in_threadpool(analytics.set_consent, db, value.visitor_id, value.revision, value.action, user_id)
    except Exception as error:
        db.rollback()
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(503, 'Аналитика временно недоступна.') from None


@router.post('/events')
async def events(request: Request, response: Response, token=Depends(optional_oauth2_scheme), db=Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    available(request)
    batch = await body(request, ClientBatch)
    try:
        revision = UUID(request.headers.get('x-lunaria-analytics-revision', ''))
        if revision.version != 4:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Недопустимое согласие аналитики.') from None
    user_id = await run_in_threadpool(identity, token, db)
    try:
        return await run_in_threadpool(ingest, db, batch, revision, user_id)
    except Exception as error:
        db.rollback()
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(503, 'Аналитика временно недоступна.') from None
