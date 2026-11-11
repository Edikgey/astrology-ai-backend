"""Opaque one-use reset tokens; delivery shares the existing SMTP adapter."""
from datetime import datetime, timedelta
import hashlib
import logging
import secrets
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from database.queries import PasswordReset, User
from services.email_service import send_password_reset_email

RESET_MESSAGE = 'Если аккаунт с таким email существует, мы отправим письмо со ссылкой для восстановления пароля.'


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def prepare_reset(bind, email):
    with Session(bind) as db:
        # Match the canonical stored address; refuse ambiguous legacy case variants.
        users = db.query(User).filter(func.lower(User.email) == email.lower()).limit(2).all()
        if len(users) != 1:
            return None
        user = users[0]
        token = secrets.token_urlsafe(32)
        db.query(PasswordReset).filter(PasswordReset.expires_at < datetime.utcnow()).delete()
        db.add(PasswordReset(token_hash=digest(token), user_id=user.id,
                             auth_version=user.auth_version, expires_at=datetime.utcnow()+timedelta(minutes=30)))
        recipient = user.email
        db.commit()
        return recipient, token


def remove_failed_reset(bind, token):
    with Session(bind) as db:
        db.query(PasswordReset).filter_by(token_hash=digest(token)).delete()
        db.commit()


async def deliver_reset(bind, email):
    # Background delivery keeps HTTP response/wording independent of account existence.
    token = None
    try:
        prepared = await run_in_threadpool(prepare_reset, bind, email)
        if prepared is None:
            return
        recipient, token = prepared
        await send_password_reset_email(recipient, token)
    except Exception as error:
        logging.getLogger(__name__).warning('Password recovery delivery failed (%s)', type(error).__name__)
        if token:
            try:
                await run_in_threadpool(remove_failed_reset, bind, token)
            except Exception as cleanup_error:
                logging.getLogger(__name__).warning('Password recovery cleanup failed (%s)', type(cleanup_error).__name__)
