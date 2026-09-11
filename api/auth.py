from typing import Optional
from uuid import UUID
from fastapi import APIRouter, HTTPException, Depends, Header, Request
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from models.user import UserCreate, UserVerify, UserLogin, Token, UserResponse
from models.natal_chart import NatalChartCreate
from database.queries import EmailVerificationCode, NatalChart
from database.connection import get_db
from database.queries import User
from sqlalchemy.exc import IntegrityError
from fastapi.security import OAuth2PasswordBearer
from services.email_service import send_confirmation_email
import random
import os
from starlette import status
from modules.migration import migrate_guest_data_to_user

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

@router.post("/request-register")
async def request_register(
    user_data: UserCreate,
    db: Session = Depends(get_db)
):
    # 🔒 Проверяем, существует ли пользователь
    existing_user = db.query(User).filter(User.email == user_data.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="❌ Пользователь с таким email уже зарегистрирован. Попробуйте войти.")

    # 📩 Генерация и сохранение кода
    code = str(random.randint(100000, 999999))
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    # Удаляем предыдущие коды
    db.query(EmailVerificationCode).filter(EmailVerificationCode.email == user_data.email).delete()
    db.add(EmailVerificationCode(
        email=user_data.email,
        code=code,
        expires_at=expires_at
    ))
    db.commit()

    await send_confirmation_email(user_data.email, code)
    return {"message": "Код отправлен на почту"}


@router.post("/verify-code", response_model=Token)
def verify_code(
    user_data: UserVerify,
    code: str,
    db: Session = Depends(get_db),
    session_token: Optional[UUID] = Header(None, alias="X-Session-Token")
):
    record = db.query(EmailVerificationCode).filter(
        EmailVerificationCode.email == user_data.email
    ).first()

    if not record or record.code != code or record.expires_at < datetime.utcnow() or record.used:
        raise HTTPException(status_code=400, detail="Неверный или просроченный код")

    user = User(email=user_data.email, password_hash=get_password_hash(user_data.password))
    db.add(user)

    try:
        db.flush()
        record.used = True
        migration = migrate_guest_data_to_user(
            db=db, user_id=user.id, session_token=session_token,
            guest_chart_id=user_data.guest_chart_id,
        )
        access_token = create_access_token(data={"sub": str(user.id)})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")
    except Exception:
        db.rollback()
        raise

    return {"access_token": access_token, "token_type": "bearer", "guest_chart_migration": migration}

# === Авторизация с миграцией гостевых данных ===
@router.post("/login", response_model=Token)
def login(
    credentials: UserLogin,
    request: Request,
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.email == credentials.email).first()
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
        access_token = create_access_token(data={"sub": str(user.id)})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"access_token": access_token, "token_type": "bearer", "guest_chart_migration": migration}
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Невалидный токен")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
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
            if user:
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
