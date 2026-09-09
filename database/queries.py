from sqlalchemy import JSON, Column, Integer, String, Float, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from database.connection import Base
from sqlalchemy.dialects.postgresql import UUID
import uuid

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    natal_charts = relationship("NatalChart", back_populates="user")


class NatalChart(Base):
    __tablename__ = "natal_charts"

    id = Column(Integer, primary_key=True, index=True)

    # 🔑 Связь с пользователем, теперь может быть пустой (если гость)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # 🆕 Новый токен для гостей
    session_token = Column(UUID(as_uuid=True), default=uuid.uuid4, nullable=True, index=True)

    # 🌍 Данные о карте
    year = Column(Integer)
    month = Column(Integer)
    day = Column(Integer)
    hour = Column(Float)
    lon = Column(Float)
    lat = Column(Float)
    city = Column(String)
    region = Column(String)
    country = Column(String)

    created_at = Column(DateTime, default=datetime.utcnow)

    # 🔗 Связи
    user = relationship("User", back_populates="natal_charts")
    chart_data = relationship("ChartData", back_populates="chart", uselist=False)
    interpretation_data = relationship("ChartInterpretationData", back_populates="chart", uselist=False)
    messages = relationship("GPTMessage", back_populates="chart", cascade="all, delete-orphan")
class GPTMessage(Base):
    __tablename__ = "gpt_messages"

    id = Column(Integer, primary_key=True, index=True)
    chart_id = Column(Integer, ForeignKey("natal_charts.id", ondelete="CASCADE"), nullable=False)

    role = Column(String, nullable=False)  # 'user' или 'gpt'
    content = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Связь с картой
    chart = relationship("NatalChart", back_populates="messages")
class ChartData(Base):
    __tablename__ = "chart_data"

    id = Column(Integer, primary_key=True, index=True)
    chart_id = Column(Integer, ForeignKey("natal_charts.id"), nullable=False)

    bodies_for_circle = Column(JSON, nullable=False)     # Словарь: { "☉": {...}, "☽": {...} }
    aspects_for_circle = Column(JSON, nullable=False)    # Список: [ {from, to, aspect}, ... ]
    points_data = Column(JSON, nullable=True)            # Список строк: [ "☉ Солнце 15° Телец (10 Дом)", ... ]
    patterns_data = Column(JSON, nullable=True)          # Паттерны
    aspects_structured = Column(JSON, nullable=True)     # 🔹 Новый столбец для аспектов (маж/мин)

    chart = relationship("NatalChart", back_populates="chart_data")

class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    email = Column(String, primary_key=True)
    code = Column(String)
    expires_at = Column(DateTime)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class ChartInterpretationData(Base):
    __tablename__ = "chart_interpretation_data"

    id = Column(Integer, primary_key=True, index=True)
    chart_id = Column(Integer, ForeignKey("natal_charts.id"), unique=True, nullable=False)
    raw_text = Column(String, nullable=False)  # Сохраняем результат get_prompt_astrology_data
    created_at = Column(DateTime, default=datetime.utcnow)

    chart = relationship("NatalChart", back_populates="interpretation_data")
