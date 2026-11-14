from sqlalchemy import JSON, Column, Integer, String, Text, Float, Boolean, ForeignKey, DateTime, CheckConstraint, Index, UniqueConstraint, case, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql.expression import Grouping
from datetime import datetime
from database.connection import Base
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    google_sub = Column(String(255), nullable=True, unique=True)
    auth_version = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.utcnow)
    plan = Column(String, nullable=False, default="free", server_default="free")
    current_period_start = Column(DateTime, nullable=True)  # UTC, as elsewhere in this schema
    current_period_end = Column(DateTime, nullable=True)
    payment_provider = Column(String, nullable=True)
    provider_customer_id = Column(String, nullable=True)
    provider_subscription_id = Column(String, nullable=True)
    subscription_status = Column(String, nullable=True)
    scheduled_cancel_at = Column(DateTime, nullable=True)
    current_period_interval = Column(String, nullable=True)  # month/year of the paid period; NULL = as before
    __table_args__ = (CheckConstraint("plan IN ('free', 'premium')", name="ck_users_plan"),
        CheckConstraint("current_period_interval IN ('month', 'year')", name="ck_users_period_interval"),
        CheckConstraint("payment_provider IS NOT NULL OR (provider_customer_id IS NULL AND provider_subscription_id IS NULL)", name="ck_users_billing_provider"),
        UniqueConstraint("payment_provider", "provider_customer_id", name="uq_users_provider_customer"),
        UniqueConstraint("payment_provider", "provider_subscription_id", name="uq_users_provider_subscription"))

    natal_charts = relationship("NatalChart", back_populates="user")


class SecurityRateLimit(Base):
    __tablename__ = "security_rate_limits"
    bucket_key = Column(String(64), primary_key=True)
    hits = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    auth_version = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class Feedback(Base):
    __tablename__ = "feedback"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_id = Column(String(36), nullable=False)
    category = Column(String(16), nullable=False)
    message = Column(Text, nullable=False)
    page_path = Column(String(200), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_feedback_request"),
        CheckConstraint("category IN ('idea', 'bug', 'feedback')", name="ck_feedback_category"),
        CheckConstraint("length(trim(message)) BETWEEN 1 AND 2000", name="ck_feedback_message"),
        Index("ix_feedback_user_created", "user_id", "created_at"),
        Index("ix_feedback_created_at", "created_at"),
    )


class StripeCheckout(Base):
    """Durable server-owned intent, including ambiguous network results."""
    __tablename__ = "stripe_checkouts"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(String, nullable=False)
    livemode = Column(Boolean, nullable=False)
    customer_id = Column(String, nullable=True)
    session_id = Column(String, unique=True, nullable=True)
    subscription_id = Column(String, unique=True, nullable=True)
    state = Column(String, nullable=False, default="creating")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    subscription_status = Column(String, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    price_id = Column(String, nullable=True)  # NULL = the monthly price


class BillingCustomer(Base):
    __tablename__ = "billing_customers"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider = Column(String, nullable=False)
    account_id = Column(String, nullable=False)
    livemode = Column(Boolean, nullable=False)
    customer_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("provider", "account_id", "livemode", "user_id", name="uq_billing_customer_user"),
        UniqueConstraint("provider", "account_id", "livemode", "customer_id", name="uq_billing_customer_external"))


class BillingPeriod(Base):
    __tablename__ = "billing_periods"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider = Column(String, nullable=False)
    account_id = Column(String, nullable=False)
    livemode = Column(Boolean, nullable=False)
    subscription_id = Column(String, nullable=False)
    invoice_id = Column(String, nullable=False)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    applied = Column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("provider", "account_id", "livemode", "invoice_id", name="uq_billing_period_invoice"),
        UniqueConstraint("provider", "account_id", "livemode", "subscription_id", "period_start", name="uq_billing_period_start"),
        CheckConstraint("period_start < period_end", name="ck_billing_period_dates"))


class BillingEvent(Base):
    __tablename__ = "billing_events"
    provider = Column(String, nullable=False)
    event_id = Column(String, primary_key=True)
    event_type = Column(String, nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    processed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    outcome = Column(String, nullable=False, default="processed")
    details = Column(JSON, nullable=True)  # minimal refund/dispute audit, no buyer or raw payload


class LavaCheckout(Base):
    """Immutable server-owned offer/buyer binding; no raw payment payloads."""
    __tablename__ = "lava_checkouts"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    contract_id = Column(String, unique=True, nullable=True)
    offer_id = Column(String, nullable=False)
    product_id = Column(String, nullable=False)
    buyer_email = Column(String, nullable=False)
    payment_url = Column(String, nullable=True)
    state = Column(String, nullable=False, default="creating")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)  # provider lifecycle timestamp
    paid_at = Column(DateTime, nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False, server_default="false")
    periodicity = Column(String, nullable=False, default="MONTHLY", server_default="MONTHLY")
    __table_args__ = (CheckConstraint("periodicity IN ('MONTHLY', 'PERIOD_YEAR')", name="ck_lava_checkouts_periodicity"),)


class QuestionPack(Base):
    """One purchase of extra questions. Only a signed, provider-verified webhook makes it paid."""
    __tablename__ = "question_packs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider = Column(String, nullable=False)
    account_id = Column(String, nullable=True)  # Stripe only
    livemode = Column(Boolean, nullable=True)  # Stripe only
    quantity = Column(Integer, nullable=False)
    state = Column(String, nullable=False, default="creating")
    customer_id = Column(String, nullable=True)
    session_id = Column(String, unique=True, nullable=True)
    payment_intent_id = Column(String, unique=True, nullable=True)
    contract_id = Column(String, unique=True, nullable=True)
    offer_id = Column(String, nullable=True)
    product_id = Column(String, nullable=True)
    buyer_email = Column(String, nullable=True)
    payment_url = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoke_reason = Column(String, nullable=True)
    __table_args__ = (
        CheckConstraint("provider IN ('stripe', 'lava')", name="ck_question_packs_provider"),
        CheckConstraint("quantity > 0", name="ck_question_packs_quantity"),
        CheckConstraint("state IN ('creating', 'ready', 'paid', 'failed', 'expired', 'rejected', 'revoked')", name="ck_question_packs_state"),
        CheckConstraint("revoke_reason IN ('refund', 'dispute')", name="ck_question_packs_revoke_reason"),
        Index("ix_question_packs_user_state", "user_id", "state"),
    )


class BillingConflict(Base):
    """Manual review only: one durable case per extra external subscription."""
    __tablename__ = "billing_conflicts"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String, nullable=False)
    subscription_id = Column(String, nullable=False)
    checkout_id = Column(String, nullable=False)
    payment_id = Column(String, nullable=True)
    primary_provider = Column(String, nullable=False)
    primary_subscription_id = Column(String, nullable=False)
    first_event_id = Column(String, nullable=False)
    last_event_id = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open", server_default="open")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("provider", "subscription_id", name="uq_billing_conflict_subscription"),
        CheckConstraint("provider IN ('lava', 'stripe')", name="ck_billing_conflict_provider"),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_billing_conflict_status"))


class GPTUsage(Base):
    """Durable quota ledger. Deleting messages clears the link, not the usage."""
    __tablename__ = "gpt_usage"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    chart_id = Column(Integer, nullable=True)  # durable subject references, not cascading FKs
    relationship_id = Column(Integer, nullable=True)
    source_message_id = Column(Integer, ForeignKey("gpt_messages.id", ondelete="SET NULL"), unique=True, nullable=True)
    source_relationship_message_id = Column(Integer, ForeignKey("relationship_messages.id", ondelete="SET NULL"), unique=True, nullable=True)
    status = Column(String, nullable=False)  # reserved / succeeded / released
    plan = Column(String, nullable=False)
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    model = Column(String, nullable=True)
    summary_usage = Column(JSON, nullable=True)  # separate provider calls, never product quota
    pack_id = Column(String, ForeignKey("question_packs.id"), nullable=True)  # NULL = a question of the plan
    __table_args__ = (
        CheckConstraint("(chart_id IS NOT NULL AND relationship_id IS NULL) OR (chart_id IS NULL AND relationship_id IS NOT NULL)", name="ck_gpt_usage_subject"),
        CheckConstraint("(source_message_id IS NULL OR chart_id IS NOT NULL) AND (source_relationship_message_id IS NULL OR relationship_id IS NOT NULL)", name="ck_gpt_usage_source_subject"),
        CheckConstraint("status IN ('reserved', 'succeeded', 'released')", name="ck_gpt_usage_status"),
        CheckConstraint("plan IN ('free', 'premium')", name="ck_gpt_usage_plan"),
        Index("ix_gpt_usage_account", "user_id", "status", "period_start"),
        Index("ix_gpt_usage_relationship", "user_id", "relationship_id", "status"),
        Index("ix_gpt_usage_pack", "pack_id", "status"),
    )


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
    timezone = Column(String, nullable=True)  # IANA zone of local birth date/hour
    birth_utc = Column(DateTime, nullable=True)  # frozen calculation instant, naive UTC

    created_at = Column(DateTime, default=datetime.utcnow)

    # 🔗 Связи
    user = relationship("User", back_populates="natal_charts")
    chart_data = relationship("ChartData", back_populates="chart", uselist=False)
    interpretation_data = relationship("ChartInterpretationData", back_populates="chart", uselist=False)
    messages = relationship("GPTMessage", back_populates="chart", cascade="all, delete-orphan")
class GPTMessage(Base):
    __tablename__ = "gpt_messages"
    __table_args__ = (Index("ix_gpt_messages_chart_id_id", "chart_id", "id"),)

    id = Column(Integer, primary_key=True, index=True)
    chart_id = Column(Integer, ForeignKey("natal_charts.id", ondelete="CASCADE"), nullable=False)

    role = Column(String, nullable=False)  # 'user' или 'gpt'
    content = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Связь с картой
    chart = relationship("NatalChart", back_populates="messages")


class GPTConversation(Base):
    """One compact memory per owner/chart; raw messages remain the UI history."""
    __tablename__ = "gpt_conversations"
    chart_id = Column(Integer, ForeignKey("natal_charts.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    summary = Column(String, nullable=False)
    through_message_id = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class ChartData(Base):
    __tablename__ = "chart_data"

    id = Column(Integer, primary_key=True, index=True)
    chart_id = Column(Integer, ForeignKey("natal_charts.id"), nullable=False)

    bodies_for_circle = Column(JSON, nullable=False)     # Словарь: { "☉": {...}, "☽": {...} }
    aspects_for_circle = Column(JSON, nullable=False)    # Список: [ {from, to, aspect}, ... ]
    points_data = Column(JSON, nullable=True)            # Список строк: [ "☉ Солнце 15° Телец (10 Дом)", ... ]
    patterns_data = Column(JSON, nullable=True)          # Паттерны
    aspects_structured = Column(JSON, nullable=True)     # 🔹 Новый столбец для аспектов (маж/мин)
    houses = Column(JSON, nullable=True)
    house_system = Column(String, nullable=True)

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


class Relationship(Base):
    __tablename__ = "relationships"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    chart_a_id = Column(Integer, ForeignKey("natal_charts.id", ondelete="RESTRICT"), nullable=False)
    chart_b_id = Column(Integer, ForeignKey("natal_charts.id", ondelete="RESTRICT"), nullable=False)
    person_a_label = Column(String(100), nullable=False)
    person_b_label = Column(String(100), nullable=False)
    speaker_person = Column(String(1), nullable=True)
    calculation = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    ruleset_version = Column(String(50), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        CheckConstraint("chart_a_id <> chart_b_id", name="ck_relationships_distinct_charts"),
        CheckConstraint("speaker_person IN ('A', 'B')", name="ck_relationships_speaker"),
        CheckConstraint("length(trim(person_a_label)) BETWEEN 1 AND 100 AND length(trim(person_b_label)) BETWEEN 1 AND 100",
                        name="ck_relationships_labels"),
        Index("ix_relationships_owner", "user_id", "id"),
        Index("ix_relationships_chart_a", "chart_a_id"),
        Index("ix_relationships_chart_b", "chart_b_id"),
        Index("uq_relationships_owner_pair", user_id,
              Grouping(case((chart_a_id < chart_b_id, chart_a_id), else_=chart_b_id)),
              Grouping(case((chart_a_id < chart_b_id, chart_b_id), else_=chart_a_id)), unique=True),
    )


class RelationshipMessage(Base):
    __tablename__ = "relationship_messages"
    id = Column(Integer, primary_key=True)
    relationship_id = Column(Integer, ForeignKey("relationships.id", ondelete="CASCADE"), nullable=False)
    role = Column(String, nullable=False)
    content = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (
        CheckConstraint("role IN ('user', 'gpt', 'assistant')", name="ck_relationship_messages_role"),
        Index("ix_relationship_messages_subject_id", "relationship_id", "id"),
    )


class RelationshipConversation(Base):
    __tablename__ = "relationship_conversations"
    relationship_id = Column(Integer, ForeignKey("relationships.id", ondelete="CASCADE"), primary_key=True)
    summary = Column(String, nullable=False)
    through_message_id = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
