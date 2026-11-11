import json
import logging
import re
from typing import Literal, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
from api.auth import get_current_user
from database.connection import get_db
from database.queries import User
from modules import feedback

router = APIRouter(prefix='/feedback', tags=['Feedback'])


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    category: Literal['idea', 'bug', 'feedback']
    message: str = Field(strict=True, max_length=2000)
    page_path: Optional[str] = Field(default=None, max_length=200)

    @field_validator('message')
    @classmethod
    def meaningful(cls,value):
        value=value.strip()
        if not any(c.isalnum() for c in value) or any(ord(c)<32 and c not in '\n\r\t' for c in value):
            raise ValueError('Добавьте текст отзыва.')
        return value

    @field_validator('page_path')
    @classmethod
    def route_only(cls,value):
        if value is None: return None
        # Never store query strings, fragments, chart IDs or arbitrary user input as telemetry.
        value=value.split('?')[0].split('#')[0]
        if re.fullmatch(r'/(my-charts|daily-horoscope|natal-chart-result)(/\d+)?',value):
            return re.sub(r'/\d+$','/:id',value)
        if re.fullmatch(r'/relationships/\d+',value): return '/relationships/:id'
        raise ValueError('Недопустимая страница.')


@router.get('/limits')
def limits(response:Response,user:User=Depends(get_current_user),db=Depends(get_db)):
    response.headers['Cache-Control']='no-store'
    return feedback.allowance(db,user.id,feedback.server_now(db))


@router.post('')
async def submit(request:Request,response:Response,user:User=Depends(get_current_user),db=Depends(get_db)):
    response.headers['Cache-Control']='no-store'
    if request.headers.get('content-type','').split(';')[0].lower()!='application/json' or request.headers.get('content-encoding','identity')!='identity':
        raise HTTPException(415,'Отправьте текстовый отзыв без вложений.')
    size=request.headers.get('content-length')
    if size and (not size.isdigit() or int(size)>16384):
        raise HTTPException(413,'Отзыв слишком большой. Максимум — 2000 символов.')
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>16384: raise HTTPException(413,'Отзыв слишком большой. Максимум — 2000 символов.')
        raw.extend(chunk)
    try:
        body=FeedbackBody.model_validate(json.loads(raw))
    except (ValueError,ValidationError,RecursionError):
        raise HTTPException(422,'Проверьте категорию и текст: от 1 до 2000 символов, без вложений.') from None
    try:
        return await run_in_threadpool(feedback.submit,db,user.id,body)
    except HTTPException:
        raise
    except Exception as error:
        logging.getLogger(__name__).warning('Feedback submission failed (%s)',type(error).__name__)
        raise HTTPException(503,'Не удалось сохранить отзыв. Попробуйте позже.') from None
