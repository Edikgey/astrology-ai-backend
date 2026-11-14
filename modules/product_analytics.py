"""Consent-gated, explicit PostHog delivery. Never load a recorder in the SPA.

Only bounded, validated enum properties leave this module. Request headers,
URLs, user content, billing identifiers and exception strings are not exported.
Analytics failures must not change an application transaction's result.
"""
import hashlib
import hmac
import logging
import os
import queue
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import httpx
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from database.connection import Base, SessionLocal
from modules.product_analytics_schema import POLICY_VERSION, ROUTE_KEYS, checked_properties

log = logging.getLogger(__name__)
context = ContextVar('lunaria_product_analytics', default=None)
_stop = threading.Event()
_thread = None
_rate_lock = threading.Lock()
_rates = {}
_facts = queue.Queue(maxsize=500)


class AnalyticsAccountPreference(Base):
    __tablename__ = 'analytics_account_preferences'
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    accepted = Column(Boolean, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class AnalyticsConsent(Base):
    __tablename__ = 'analytics_consents'
    visitor_key = Column(String(64), primary_key=True)
    revision = Column(String(36), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=True, index=True)
    accepted = Column(Boolean, nullable=False)
    policy_version = Column(String(32), nullable=False)
    accepted_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class AnalyticsOutbox(Base):
    __tablename__ = 'analytics_outbox'
    event_id = Column(String(36), primary_key=True)
    visitor_key = Column(String(64), ForeignKey('analytics_consents.visitor_key', ondelete='CASCADE'), nullable=False, index=True)
    revision = Column(String(36), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=True, index=True)
    distinct_id = Column(String(80), nullable=False)
    event_name = Column(String(48), nullable=False)
    properties = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=False, index=True)
    delivered_at = Column(DateTime, nullable=True)


class AnalyticsMonthlyBudget(Base):
    __tablename__ = 'analytics_monthly_budget'
    month = Column(String(7), primary_key=True)
    events = Column(Integer, nullable=False)


def now():
    return datetime.utcnow()


def settings():
    host = os.getenv('POSTHOG_HOST', '')
    token = os.getenv('POSTHOG_PROJECT_TOKEN', '')
    secret = os.getenv('POSTHOG_IDENTITY_SECRET', '')
    valid = (host in ('https://eu.i.posthog.com', 'https://us.i.posthog.com') and
             token.startswith('phc_') and len(secret) >= 32)
    return dict(enabled=valid and os.getenv('POSTHOG_ENABLED') == 'true' and
                os.getenv('POSTHOG_PRIVACY_VERIFIED') == 'true', host=host, token=token, secret=secret)


def enabled():
    return settings()['enabled']


def digest(kind, value):
    secret = settings()['secret']
    if len(secret) < 32:
        raise ValueError('Analytics identity is not configured.')
    return hmac.new(secret.encode(), f'{kind}:{value}'.encode(), hashlib.sha256).hexdigest()


def visitor_key(visitor_id):
    value = UUID(str(visitor_id))
    if value.version != 4:
        raise ValueError('Invalid analytics identity.')
    return digest('visitor', value)


def distinct(consent):
    return 'account_' + digest('account', consent.user_id) if consent.user_id else 'visitor_' + consent.visitor_key


def eligible(db, consent):
    if (not consent or not consent.accepted or consent.policy_version != POLICY_VERSION or consent.expires_at <= now()):
        return False
    pref = db.get(AnalyticsAccountPreference, consent.user_id) if consent.user_id else None
    return not consent.user_id or bool(pref and pref.accepted)


def rate_allowed(client_host):
    """Coarse bounded abuse guard; no IP or IP-derived ID leaves the process."""
    key = digest('rate', client_host or 'unknown')
    tick = time.monotonic()
    with _rate_lock:
        for old, (_, until) in list(_rates.items()):
            if until <= tick:
                del _rates[old]
        if key not in _rates and len(_rates) >= 2048:
            return False
        count, until = _rates.get(key, (0, tick + 60))
        _rates[key] = (count + 1, until)
        return count < 120


def enqueue(db, consent, name, properties, *, event_id=None, session_id=None, route_key=None, source='server', viewport_class='unknown', campaign_source='unspecified', occurred_at=None):
    if not eligible(db, consent):
        return False
    # Internal identify has exactly one permitted pseudonymous property.
    if name == '$identify':
        if set(properties) != {'$anon_distinct_id'} or properties['$anon_distinct_id'] != 'visitor_' + consent.visitor_key:
            raise ValueError('Invalid analytics identification.')
    else:
        properties = checked_properties(name, properties, source=source)
    event_id = str(event_id or uuid4())
    if db.get(AnalyticsOutbox, event_id):
        return False
    # Bound analytics storage independently of product data. No unbounded replay.
    if db.query(AnalyticsOutbox).filter(AnalyticsOutbox.delivered_at.is_(None)).count() >= 10000:
        return False
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    insert = sqlite_insert if db.bind.dialect.name == 'sqlite' else postgres_insert
    try:
        limit = min(900000, max(1, int(os.getenv('POSTHOG_MONTHLY_EVENT_LIMIT', '100000'))))
    except ValueError:
        limit = 100000
    statement = insert(AnalyticsMonthlyBudget).values(month=now().strftime('%Y-%m'), events=1)
    statement = statement.on_conflict_do_update(index_elements=['month'],
        set_={'events': AnalyticsMonthlyBudget.events + 1}, where=AnalyticsMonthlyBudget.events < limit).returning(AnalyticsMonthlyBudget.events)
    if db.execute(statement).scalar_one_or_none() is None:
        return False
    safe = dict(properties)
    if viewport_class not in ('small', 'medium', 'large', 'unknown') or campaign_source not in ('unspecified', 'telegram', 'instagram', 'vk', 'youtube', 'other'):
        raise ValueError('Invalid analytics context.')
    safe.update(viewport_class=viewport_class, campaign_source=campaign_source)
    if route_key in ROUTE_KEYS:
        safe['route_key'] = route_key
    if session_id:
        parsed = UUID(str(session_id))
        if parsed.version != 4:
            raise ValueError('Invalid analytics session.')
        safe['$session_id'] = str(parsed)
    environment = os.getenv('POSTHOG_EVENT_ENVIRONMENT', 'production')
    if environment not in ('production', 'qa'):
        raise ValueError('Invalid analytics environment.')
    safe.update(schema_version=1, source=source, environment=environment,
                **{'$geoip_disable': True, '$ip': None, '$process_person_profile': bool(consent.user_id)})
    db.add(AnalyticsOutbox(event_id=event_id, visitor_key=consent.visitor_key,
        revision=consent.revision, user_id=consent.user_id, distinct_id=distinct(consent),
        event_name=name, properties=safe, created_at=occurred_at or now(), next_attempt_at=now()))
    db.flush()
    return True


def bind_account(db, consent, user_id, *, explicit=False, session_id=None):
    if consent.user_id not in (None, user_id):
        return False
    pref = db.query(AnalyticsAccountPreference).filter_by(user_id=user_id).with_for_update().first()
    if pref is None:
        pref = AnalyticsAccountPreference(user_id=user_id, accepted=True, updated_at=now())
        db.add(pref)
    elif not pref.accepted and not explicit:
        return False
    if explicit:
        pref.accepted = True
        pref.updated_at = now()
    first = consent.user_id is None
    consent.user_id = user_id
    db.flush()
    if first:
        enqueue(db, consent, '$identify', {'$anon_distinct_id': 'visitor_' + consent.visitor_key}, session_id=session_id)
    return True


def set_consent(db, visitor_id, revision, action, user_id=None):
    # New-device first accept can race first withdrawal, including account preference creation.
    for attempt in range(3):
        try:
            return _set_consent_once(db, visitor_id, revision, action, user_id)
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise


def _set_consent_once(db, visitor_id, revision, action, user_id=None):
    key = visitor_key(visitor_id)
    row = db.query(AnalyticsConsent).filter_by(visitor_key=key).with_for_update().first()
    stamp = now()
    if action in ('withdraw', 'reset'):
        if row is None:
            row = AnalyticsConsent(visitor_key=key, revision=str(revision), accepted=False,
                policy_version=POLICY_VERSION, accepted_at=stamp, expires_at=stamp)
            db.add(row)
        row.accepted = False
        # Tombstone the in-flight lease: a late accept with the same lease cannot revive it.
        row.revision = str(revision)
        owners = {owner for owner in (user_id, row.user_id) if owner} if action == 'withdraw' else set()
        for owner in sorted(owners):
            pref = db.get(AnalyticsAccountPreference, owner)
            if pref is None:
                db.add(AnalyticsAccountPreference(user_id=owner, accepted=False, updated_at=stamp))
            else:
                pref.accepted = False
                pref.updated_at = stamp
            db.query(AnalyticsConsent).filter_by(user_id=owner).update({'accepted': False}, synchronize_session=False)
            db.query(AnalyticsOutbox).filter_by(user_id=owner).filter(AnalyticsOutbox.delivered_at.is_(None)).delete(synchronize_session=False)
        db.query(AnalyticsOutbox).filter_by(visitor_key=key).filter(AnalyticsOutbox.delivered_at.is_(None)).delete(synchronize_session=False)
        db.commit()
        return {'accepted': False}
    if row and row.user_id not in (None, user_id):
        return {'accepted': False, 'reset_identity': True}
    if row and not row.accepted and row.revision == str(revision):
        return {'accepted': False}
    if row and row.expires_at <= stamp and action != 'accept':
        return {'accepted': False}
    if row is None:
        row = AnalyticsConsent(visitor_key=key, revision=str(revision), user_id=None,
            accepted=True, policy_version=POLICY_VERSION, accepted_at=stamp, expires_at=stamp + timedelta(days=365))
        db.add(row)
    elif action == 'accept' or (row.accepted and row.revision == str(revision)):
        if action == 'accept' and row.revision != str(revision):
            # Reaccepting starts a fresh lease; never revive an old pending queue.
            db.query(AnalyticsOutbox).filter_by(visitor_key=key).filter(AnalyticsOutbox.delivered_at.is_(None)).delete(synchronize_session=False)
            row.accepted_at = stamp
            row.expires_at = stamp + timedelta(days=365)
        row.revision, row.accepted = str(revision), True
        row.policy_version = POLICY_VERSION
    else:
        return {'accepted': False}
    if user_id and not bind_account(db, row, user_id, explicit=action == 'accept'):
        db.rollback()
        return {'accepted': False}
    db.commit()
    return {'accepted': True}


def capture_business(bind, user_id, name, properties, key, request_context=None):
    # Do not acquire a second connection during a business commit.
    try:
        if request_context and request_context.get('interactive') and not request_context.get('visitor_id'):
            return
        if enabled():
            _facts.put_nowait((time.monotonic(), bind, user_id, name, dict(properties), key,
                dict(request_context) if request_context else None, now()))
    except Exception as error:
        log.warning('Analytics handoff unavailable (%s)', type(error).__name__)


def capture_payment_failure(db, user_id, provider, stage, key):
    """Call only after the payment outcome is final (commit/rollback). Never raises, never changes it."""
    try:
        capture_business(db.get_bind(), user_id, 'payment_failed', {'provider': provider, 'stage': stage}, key, context.get())
    except Exception as error:
        log.warning('Analytics handoff unavailable (%s)', type(error).__name__)


def shadow_limit(db, consent, user_id, name, properties, ctx, occurred_at):
    """Beta only: would this successful action have hit the free limit? Real limits are not consulted or changed."""
    from modules.free_beta import free_beta_enabled
    from modules.plans import PLAN_LIMITS, effective_plan
    from database.queries import User, NatalChart, GPTUsage
    if name == 'ai_answer_completed':
        resource = 'question'
    elif name == 'guest_chart_saved' or (name == 'chart_creation_completed' and properties.get('actor') == 'account'):
        resource = 'chart'
    else:
        return
    user = db.get(User, user_id) if user_id else None
    if not free_beta_enabled() or user is None or effective_plan(user) != 'free':
        return
    free = PLAN_LIMITS['free']
    if resource == 'chart':
        before, limit = db.query(NatalChart).filter(NatalChart.user_id == user_id).count() - 1, free.max_saved_charts
    else:
        before, limit = db.query(GPTUsage).filter(GPTUsage.user_id == user_id, GPTUsage.status == 'succeeded').count() - 1, free.gpt_message_limit
    if before >= limit:
        # One per account and resource while the outbox row is retained (7 days).
        event_id = str(UUID(digest('event', f'free_limit:{resource}:{user_id}')[:32], version=4))
        enqueue(db, consent, 'free_limit_would_block', {'resource': resource}, event_id=event_id,
            session_id=ctx.get('session_id'), route_key=ctx.get('route_key'), viewport_class=ctx.get('viewport_class', 'unknown'), occurred_at=occurred_at)


def drain_fact_queue(limit=20):
    for _ in range(limit):
        try:
            stamp, *fact = _facts.get_nowait()
        except queue.Empty:
            break
        try:
            if time.monotonic() - stamp < 300:
                persist_business(*fact)
        finally:
            _facts.task_done()


def persist_business(bind, user_id, name, properties, key, request_context, occurred_at=None):
    """After business commit only. Uses a separate, best-effort analytics transaction."""
    if not enabled():
        return
    try:
        with Session(bind=bind) as db:
            ctx = request_context or {}
            tracked = ctx.get('visitor_id') and ctx.get('revision')
            consent = db.query(AnalyticsConsent).filter_by(visitor_key=visitor_key(ctx['visitor_id'])).with_for_update().first() if tracked else None
            if consent and consent.revision != ctx['revision']:
                return
            if consent and user_id and consent.user_id is None and ctx.get('auth'):
                if not bind_account(db, consent, user_id, session_id=ctx.get('session_id')):
                    return
            # Reject/nonconsent on an interactive device must not fall back to another device.
            if ctx.get('interactive') and not tracked:
                return
            if consent and user_id and consent.user_id != user_id:
                return
            if consent is None and user_id and not ctx.get('interactive'):
                consent = db.query(AnalyticsConsent).filter_by(user_id=user_id, accepted=True,
                    policy_version=POLICY_VERSION).filter(AnalyticsConsent.expires_at > now()).order_by(AnalyticsConsent.accepted_at.desc()).first()
            if not eligible(db, consent):
                return
            if occurred_at and consent.accepted_at > occurred_at:
                return
            event_id = str(UUID(digest('event', key)[:32], version=4))
            accepted = enqueue(db, consent, name, properties, event_id=event_id,
                session_id=ctx.get('session_id'), route_key=ctx.get('route_key'), viewport_class=ctx.get('viewport_class', 'unknown'), occurred_at=occurred_at)
            if name == 'billing_period_paid' and key.startswith('paid:'):
                # Query financial facts only on the worker's separate connection.
                from database.queries import BillingPeriod
                period = db.get(BillingPeriod, key[5:])
                if period:
                    first = db.query(BillingPeriod.id).filter_by(user_id=user_id, provider=period.provider,
                        subscription_id=period.subscription_id, applied=True).order_by(BillingPeriod.period_start).first()
                    if first and first[0] == period.id:
                        started_id = str(UUID(digest('event', f'subscription:{period.provider}:{period.subscription_id}')[:32], version=4))
                        enqueue(db, consent, 'subscription_started', {'provider': period.provider}, event_id=started_id,
                            session_id=ctx.get('session_id'), route_key=ctx.get('route_key'), occurred_at=occurred_at)
            db.commit()
            if accepted:
                # Own transaction after the main event is committed: a failed count cannot drop it.
                shadow_limit(db, consent, user_id, name, properties, ctx, occurred_at)
                db.commit()
    except Exception as error:
        log.warning('Analytics capture unavailable (%s)', type(error).__name__)


def observe_flush(db, _):
    if not enabled():
        return
    try:
        from database.queries import User, NatalChart, Relationship, Feedback, GPTUsage, BillingPeriod
        facts = db.info.setdefault('lunaria_analytics_facts', [])
        ctx = context.get()
        if ctx and ctx.get('interactive') and not ctx.get('visitor_id'):
            return
        def fact(owner, name, props, key):
            facts.append((owner, name, props, key, dict(ctx) if ctx else None))
        for row in list(db.new):
            if isinstance(row, User) and ctx and ctx.get('auth'):
                method = 'google' if ctx.get('auth') == 'google' else 'email'
                fact(row.id, 'registration_completed', {'method': method}, f'registration:{row.id}')
                if method == 'email':
                    fact(row.id, 'email_verification_completed', {}, f'verification:{row.id}')
            elif isinstance(row, NatalChart):
                fact(row.user_id, 'chart_creation_completed', {'actor': 'account' if row.user_id else 'guest'}, f'chart:{row.id}')
            elif isinstance(row, Relationship):
                fact(row.user_id, 'relationship_creation_completed', {}, f'relationship:{row.id}')
            elif isinstance(row, Feedback):
                fact(row.user_id, 'feedback_submitted', {'category': row.category}, f'feedback:{row.id}')
        for row in list(db.new) + list(db.dirty):
            state = inspect(row)
            if isinstance(row, GPTUsage) and row.status == 'succeeded' and state.attrs.status.history.has_changes():
                elapsed = max(0, (now() - row.created_at).total_seconds())
                bucket = 'under_5s' if elapsed < 5 else '5_to_15s' if elapsed < 15 else '15_to_30s' if elapsed < 30 else 'over_30s'
                fact(row.user_id, 'ai_answer_completed', {'subject': 'relationship' if row.relationship_id else 'natal', 'latency': bucket}, f'answer:{row.id}')
            elif (isinstance(row, NatalChart) and row not in db.new and row.user_id and ctx and ctx.get('auth')
                    and state.attrs.user_id.history.has_changes()):
                # Only migrate_guest_data_to_user assigns an owner to an existing chart (sign-in paths).
                fact(row.user_id, 'guest_chart_saved', {'method': 'google' if ctx.get('auth') == 'google' else 'email'}, f'guest_chart:{row.id}')
            elif isinstance(row, BillingPeriod) and row.applied and (row in db.new or state.attrs.applied.history.has_changes()):
                fact(row.user_id, 'billing_period_paid', {'provider': row.provider}, f'paid:{row.id}')
            elif isinstance(row, User):
                provider = row.payment_provider or next(iter(state.attrs.payment_provider.history.deleted), None)
                if provider in ('stripe', 'lava'):
                    if row.scheduled_cancel_at and state.attrs.scheduled_cancel_at.history.has_changes():
                        fact(row.id, 'subscription_cancel_scheduled', {'provider': provider}, f'cancel:{row.id}:{row.scheduled_cancel_at}')
                    if 'premium' in state.attrs.plan.history.deleted and row.plan == 'free':
                        fact(row.id, 'subscription_expired', {'provider': provider}, f'expired:{row.id}:{now().date()}')
                if ctx and ctx.get('reset') and state.attrs.auth_version.history.has_changes():
                    fact(row.id, 'password_reset_completed', {}, f'reset:{row.id}:{row.auth_version}')
    except Exception as error:
        db.info.pop('lunaria_analytics_facts', None)
        log.warning('Analytics observation unavailable (%s)', type(error).__name__)


def observe_commit(db):
    if db.in_nested_transaction():
        return
    facts = db.info.pop('lunaria_analytics_facts', [])
    if not facts:
        return
    try:
        bind = db.get_bind()
        for args in facts:
            capture_business(bind, *args)
    except Exception as error:
        log.warning('Analytics commit observation unavailable (%s)', type(error).__name__)


def observe_rollback(db, *args):
    db.info.pop('lunaria_analytics_facts', None)


event.listen(Session, 'after_flush', observe_flush)
event.listen(Session, 'after_commit', observe_commit)
event.listen(Session, 'after_rollback', observe_rollback)


def deliver_once(factory=SessionLocal, post=None):
    """Bounded delivery lease. Do not hold any business/analytics DB lock over HTTP."""
    if not enabled():
        return False
    config = settings()
    with factory() as db:
        row = db.query(AnalyticsOutbox).filter(AnalyticsOutbox.delivered_at.is_(None),
            AnalyticsOutbox.attempts < 3, AnalyticsOutbox.created_at > now() - timedelta(hours=24),
            AnalyticsOutbox.next_attempt_at <= now()).order_by(AnalyticsOutbox.created_at).with_for_update(skip_locked=True).first()
        if row is None:
            return False
        consent = db.get(AnalyticsConsent, row.visitor_key)
        if not eligible(db, consent) or row.revision != consent.revision:
            db.delete(row)
            db.commit()
            return True
        row.attempts += 1
        row.next_attempt_at = now() + timedelta(seconds=30 * row.attempts)
        event_id, key, revision = row.event_id, row.visitor_key, row.revision
        payload = {'api_key': config['token'], 'event': row.event_name, 'distinct_id': row.distinct_id,
            'uuid': row.event_id, 'timestamp': row.created_at.replace(tzinfo=timezone.utc).isoformat(),
            'properties': row.properties}
        db.commit()
    # Recheck right before transmission. An already in-flight request cannot be recalled.
    with factory() as db:
        consent = db.get(AnalyticsConsent, key)
        if not eligible(db, consent) or consent.revision != revision:
            return True
    try:
        response = (post or httpx.post)(config['host'] + '/i/v0/e/', json=payload,
            timeout=5, follow_redirects=False, headers={'Content-Type': 'application/json'})
        success = 200 <= response.status_code < 300
    except Exception:
        success = False
    with factory() as db:
        row = db.get(AnalyticsOutbox, event_id)
        if row and success:
            row.delivered_at = now()
            db.commit()
    if not success:
        log.warning('Analytics delivery unavailable')
    return True


def worker():
    next_cleanup = 0
    while not _stop.is_set():
        try:
            if not enabled():
                _stop.wait(1)
                continue
            drain_fact_queue()
            if time.monotonic() >= next_cleanup:
                with SessionLocal() as db:
                    # Analytics-only retention: never touch application/billing/usage tables.
                    db.query(AnalyticsOutbox).filter(AnalyticsOutbox.created_at < now() - timedelta(days=7)).delete(synchronize_session=False)
                    db.commit()
                next_cleanup = time.monotonic() + 3600
            if not deliver_once():
                _stop.wait(1)
        except Exception as error:
            log.warning('Analytics worker unavailable (%s)', type(error).__name__)
            _stop.wait(30)


def start_worker():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=worker, name='lunaria-product-analytics', daemon=True)
    _thread.start()


def stop_worker():
    _stop.set()
    if _thread:
        _thread.join(timeout=6)
