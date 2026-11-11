"""Small private feedback inbox. No provider calls or product quota mutations."""
from datetime import datetime, timedelta
import math
from fastapi import HTTPException
from sqlalchemy import func, text
from database.queries import Feedback, User

LIMIT = 3


def server_now(db):
    # Read after acquiring the user lock; PostgreSQL now() is transaction-start time.
    if db.bind.dialect.name == 'postgresql':
        return db.execute(text("SELECT clock_timestamp() AT TIME ZONE 'UTC'")).scalar_one()
    return datetime.utcnow()


def allowance(db, user_id, now):
    count, first = db.query(func.count(Feedback.id), func.min(Feedback.created_at)).filter(
        Feedback.user_id == user_id, Feedback.created_at > now - timedelta(hours=24)).one()
    return {'remaining': max(0, LIMIT-count), 'limit': LIMIT,
        'retry_after_seconds': max(1, math.ceil((first+timedelta(hours=24)-now).total_seconds())) if count >= LIMIT else 0}


def submit(db, user_id, body):
    try:
        user = db.query(User).filter_by(id=user_id).with_for_update().one_or_none()
        if user is None: raise HTTPException(401, 'Войдите в аккаунт заново.')
        now = server_now(db)
        old = db.query(Feedback).filter_by(user_id=user_id, request_id=str(body.request_id)).first()
        if old and (old.category != body.category or old.message != body.message or old.page_path != body.page_path):
            raise HTTPException(409, 'Этот запрос уже отправлен. Закройте форму и попробуйте снова.')
        duplicate = old or db.query(Feedback).filter(
            Feedback.user_id == user_id, Feedback.category == body.category, Feedback.message == body.message,
            Feedback.created_at > now-timedelta(hours=24)).first()
        if duplicate:
            result = {'accepted': True, 'duplicate': True, **allowance(db,user_id,now)}
            db.commit()
            return result
        limits = allowance(db,user_id,now)
        if not limits['remaining']:
            raise HTTPException(429, {'message':'Можно отправить до 3 отзывов за 24 часа. Попробуйте позже.', **limits},
                headers={'Retry-After':str(limits['retry_after_seconds'])})
        db.add(Feedback(user_id=user_id, request_id=str(body.request_id), category=body.category,
            message=body.message, page_path=body.page_path, created_at=now))
        db.flush()
        result = {'accepted': True, 'duplicate': False, **allowance(db,user_id,now)}
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
