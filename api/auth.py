from modules.product_analytics import capture_business, context as analytics_context
from typing import Optional
from uuid import UUID, uuid4
from fastapi import APIRouter, HTTPException, Depends, Header, Request, Response, BackgroundTasks
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from models.user import UserCreate, UserVerify, UserLogin, Token, UserResponse
from models.natal_chart import NatalChartCreate
from database.queries import EmailVerificationCode, NatalChart
from database.connection import get_db
from database.queries import User, PasswordReset
from sqlalchemy.exc import IntegrityError
from fastapi.security import OAuth2PasswordBearer
from services.email_service import send_confirmation_email, EmailDeliveryError
import os
from starlette import status
from modules.migration import migrate_guest_data_to_user
from models.user import GoogleLogin
from services.google_identity import verify_google_credential
from sqlalchemy import func, inspect, or_
import secrets
from modules.auth_origins import AUTH_ORIGINS
from modules.security_limits import enforce
from modules.password_recovery import RESET_MESSAGE, deliver_reset, digest
from models.user import PasswordResetRequest, PasswordResetConfirm

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")  # если ещё не определён

router = APIRouter(prefix="/auth", tags=["Authentication"])

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
class OptionalOAuth2Scheme:
    def __init__(self, auto_error: bool = False):
        self.auto_error = auto_error

    async def __call__(self, request: Request) -> Optional[str]:
        authorization: str = request.headers.get("Authorization")
        scheme, param = get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "bearer":
            return None
        return param

# === Вспомогательные функции ===
def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta=None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# New accounts and codes are stored as email.lower() (as Google sign-in already does);
# accounts created earlier keep the spelling they were registered with.
DUPLICATE_EMAIL = "Этот email уже зарегистрирован. Войдите или восстановите пароль."


def email_taken(db: Session, email: str) -> bool:
    # Plain equality only: '_' and '%' are valid in an address, so like/ilike would match
    # other accounts. The exact comparison also covers capitals the database lower() keeps.
    return db.query(User.id).filter(
        or_(User.email == email, func.lower(User.email) == email.lower())
    ).first() is not None


def user_for_login(db: Session, email: str) -> Optional[User]:
    # The exact spelling wins, so every account that signs in today keeps its id. Otherwise
    # only a single case-insensitive match is used; an ambiguous address selects nobody.
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        matches = db.query(User).filter(func.lower(User.email) == email.lower()).limit(2).all()
        user = matches[0] if len(matches) == 1 else None
    return user


@router.post("/request-register")
async def request_register(
    user_data: UserCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    enforce(db, request, 'register', email=str(user_data.email))
    if email_taken(db, str(user_data.email)):
        raise HTTPException(status_code=400, detail=DUPLICATE_EMAIL)
    email = str(user_data.email).lower()

    # 📩 Генерация и сохранение кода
    code = str(100000 + secrets.randbelow(900000))
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    # Удаляем предыдущие коды
    db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).delete()
    db.add(EmailVerificationCode(
        email=email,
        code=code,
        expires_at=expires_at
    ))
    db.commit()

    try:
        await send_confirmation_email(email, code)
    except EmailDeliveryError:
        # Do not leave an undelivered code usable, or remove a newer request's code.
        db.query(EmailVerificationCode).filter(
            EmailVerificationCode.email == email,
            EmailVerificationCode.code == code,
            EmailVerificationCode.expires_at == expires_at,
        ).delete()
        db.commit()
        raise HTTPException(503, detail="Не удалось отправить код подтверждения. Попробуйте позже.") from None
    return {"message": "Код отправлен на почту"}


@router.post("/verify-code", response_model=Token)
def verify_code(
    user_data: UserVerify,
    request: Request,
    code: Optional[str] = None,  # Compatibility with old clients; new UI sends JSON.
    db: Session = Depends(get_db),
    session_token: Optional[UUID] = Header(None, alias="X-Session-Token")
):
    enforce(db, request, 'verify', email=str(user_data.email))
    code = user_data.code if user_data.code is not None else code
    email = str(user_data.email).lower()
    record = db.query(EmailVerificationCode).filter(
        EmailVerificationCode.email == email
    ).first()

    if not record or record.code != code or record.expires_at < datetime.utcnow() or record.used:
        raise HTTPException(status_code=400, detail="Неверный или просроченный код")
    # Up to 10 minutes pass between the code request and this point.
    if email_taken(db, str(user_data.email)):
        raise HTTPException(status_code=400, detail=DUPLICATE_EMAIL)

    user = User(email=email, password_hash=get_password_hash(user_data.password))
    db.add(user)

    try:
        db.flush()
        record.used = True
        migration = migrate_guest_data_to_user(
            db=db, user_id=user.id, session_token=session_token,
            guest_chart_id=user_data.guest_chart_id,
        )
        access_token = create_access_token(data={"sub": str(user.id), "ver": user.auth_version})
        db.commit()
    except IntegrityError:
        # A concurrent registration of the same address won ix_users_email. Drop the
        # database message: it contains the address.
        db.rollback()
        raise HTTPException(status_code=400, detail=DUPLICATE_EMAIL) from None
    except Exception:
        db.rollback()
        raise

    capture_business(db.get_bind(), inspect(user).identity[0], 'login_succeeded',
        {'method': (analytics_context.get() or {}).get('auth') or 'email'},
        'login:' + str(uuid4()), analytics_context.get())
    return {"access_token": access_token, "token_type": "bearer", "guest_chart_migration": migration}

# === Авторизация с миграцией гостевых данных ===
@router.post("/login", response_model=Token)
def login(
    credentials: UserLogin,
    request: Request,
    db: Session = Depends(get_db)
):
    enforce(db, request, 'login', email=str(credentials.email))
    user = user_for_login(db, str(credentials.email))
    if not user or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    session_token = request.headers.get("X-Session-Token")
    session_token_uuid = None
    if session_token:
        try:
            session_token_uuid = UUID(session_token)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Некорректный X-Session-Token"
            )

    try:
        migration = migrate_guest_data_to_user(
            db=db, user_id=user.id, session_token=session_token_uuid,
            guest_chart_id=credentials.guest_chart_id,
        )
        access_token = create_access_token(data={"sub": str(user.id), "ver": user.auth_version})
        db.commit()
    except Exception:
        db.rollback()
        raise
    capture_business(db.get_bind(), inspect(user).identity[0], 'login_succeeded',
        {'method': (analytics_context.get() or {}).get('auth') or 'email'},
        'login:' + str(uuid4()), analytics_context.get())
    return {"access_token": access_token, "token_type": "bearer", "guest_chart_migration": migration}
@router.post("/google", response_model=Token)
def google_login(credentials: GoogleLogin, request: Request, db: Session = Depends(get_db)):
    # GIS popup callback sends JSON, not Google's form-post/redirect flow. Reject
    # cross-site login CSRF explicitly; no cookies or application JWT are trusted here.
    if request.headers.get("origin") not in AUTH_ORIGINS:
        raise HTTPException(403, detail="Недопустимый источник входа Google.")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, detail="Требуется JSON.")
    enforce(db, request, 'google')
    identity = verify_google_credential(credentials.credential, credentials.nonce)
    try:
        session_token = UUID(request.headers["X-Session-Token"]) if request.headers.get("X-Session-Token") else None
    except ValueError:
        raise HTTPException(400, detail="Некорректный X-Session-Token") from None

    # Uniqueness handles concurrent first sign-ins. Retry the complete transaction
    # once after a competing insert/link commits, re-reading the stable subject.
    for attempt in range(2):
        try:
            user = db.query(User).filter(User.google_sub == identity['sub']).with_for_update().first()
            if user is None:
                matches = db.query(User).filter(func.lower(User.email) == identity['email']).with_for_update().all()
                if len(matches) > 1:
                    raise HTTPException(409, detail="Невозможно однозначно связать аккаунт. Используйте вход по email.")
                user = matches[0] if matches else None
                if user and user.google_sub not in (None, identity['sub']):
                    raise HTTPException(409, detail="Этот аккаунт уже связан с другим Google-профилем. Используйте вход по email.")
                if user and not identity['authoritative']:
                    if not credentials.password:
                        raise HTTPException(409, detail={"code": "google_link_confirmation_required", "message": "Подтвердите пароль существующего аккаунта, чтобы связать его с Google."})
                    if not verify_password(credentials.password, user.password_hash):
                        raise HTTPException(401, detail="Не удалось подтвердить пароль существующего аккаунта.")
                if user is None:
                    # Existing schema requires a password hash. Generate an unknown
                    # random password; no password is exposed or assigned to the user.
                    user = User(email=identity['email'], password_hash=get_password_hash(secrets.token_urlsafe(32)))
                    db.add(user)
                user.google_sub = identity['sub']
                db.flush()
            migration = migrate_guest_data_to_user(db=db, user_id=user.id, session_token=session_token,
                                                  guest_chart_id=credentials.guest_chart_id)
            access_token = create_access_token(data={"sub": str(user.id), "ver": user.auth_version})
            db.commit()
            capture_business(db.get_bind(), inspect(user).identity[0], 'login_succeeded',
                {'method': (analytics_context.get() or {}).get('auth') or 'email'},
                'login:' + str(uuid4()), analytics_context.get())
            return {"access_token": access_token, "token_type": "bearer", "guest_chart_migration": migration}
        except IntegrityError:
            db.rollback()
            if attempt:
                raise HTTPException(409, detail="Аккаунт изменился во время входа. Повторите вход Google.") from None
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise HTTPException(503, detail="Не удалось завершить вход. Попробуйте снова.") from None


def current_session(payload, user):
    version = payload.get('ver', 0)  # Existing JWTs remain valid until a reset.
    return type(version) is int and version == user.auth_version


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Невалидный токен")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if not current_session(payload, user):
        raise HTTPException(401, detail='Сессия завершена. Войдите в аккаунт заново.')
    return user
optional_oauth2_scheme = OptionalOAuth2Scheme()

async def get_current_user_or_guest(
    token: Optional[str] = Depends(optional_oauth2_scheme),  # ← JWT из Authorization
    session_token: Optional[str] = Header(None, alias="X-Session-Token"),  # ← session_token из кастомного заголовка
    db: Session = Depends(get_db)
) -> dict:
    # 1. Авторизованный пользователь (JWT)
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_id = int(payload.get("sub"))
            user = db.query(User).filter(User.id == user_id).first()
            if user and current_session(payload, user):
                return {"user": user, "session_token": None}
        except Exception:
            pass  # если JWT плохой — идем дальше

    # 2. Гость с session_token
    if session_token:
        try:
            session_token_uuid = UUID(session_token)
            return {"user": None, "session_token": session_token_uuid}
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="❌ Некорректный формат session_token"
            )

    # 3. Никто
    raise HTTPException(
        status_code=401,
        detail="❌ Не аутентифицирован (нужен JWT или X-Session-Token)"
    )

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email
    }


@router.post('/request-password-reset')
def request_password_reset(body: PasswordResetRequest, request: Request, response: Response,
                           background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    enforce(db, request, 'reset_request', email=str(body.email))
    background_tasks.add_task(deliver_reset, db.get_bind(), str(body.email))
    return {'message': RESET_MESSAGE}


@router.post('/reset-password')
def reset_password(body: PasswordResetConfirm, request: Request, response: Response,
                   db: Session = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    enforce(db, request, 'reset_confirm')
    invalid = 'Ссылка недействительна или срок её действия истёк. Запросите новую ссылку.'
    try:
        token_hash = digest(body.token)
        record = db.query(PasswordReset).filter_by(token_hash=token_hash).first()
        if record is None:
            raise HTTPException(400, invalid)
        # Lock the user before any token: resets with different tokens serialize too.
        user = db.query(User).filter_by(id=record.user_id).with_for_update().first()
        record = db.query(PasswordReset).filter_by(token_hash=token_hash).populate_existing().with_for_update().first()
        if (not user or record is None or record.expires_at <= datetime.utcnow()
                or record.auth_version != user.auth_version):
            raise HTTPException(400, invalid)
        user.password_hash = get_password_hash(body.password)
        user.auth_version += 1
        db.query(PasswordReset).filter_by(user_id=user.id).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {'message': 'Пароль изменён. Войдите в аккаунт с новым паролем.'}
