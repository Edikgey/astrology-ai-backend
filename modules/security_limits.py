"""Shared, atomic database limits; no raw email addresses/IPs in buckets."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import logging
import math
import os
import random
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

POLICIES = {
    'login': [('ip', 30, 300), ('pair', 10, 900), ('email', 30, 900)],
    'google': [('ip', 30, 300)],
    'register': [('ip', 20, 3600), ('email', 1, 60), ('email', 5, 3600)],
    'verify': [('ip', 50, 900), ('email', 10, 600)],
    'reset_request': [('ip', 20, 3600), ('email', 1, 60), ('email', 5, 3600)],
    'reset_confirm': [('ip', 20, 900)],
    'guest_chart': [('ip', 10, 3600), ('ip', 30, 86400), ('guest', 5, 3600)],
}
SECRET_KEY = os.environ.get('SECRET_KEY')


def client_address(request):
    # Railway ingress supplies X-Real-IP. Never trust arbitrary forwarded chains.
    value = request.client.host if request.client else 'unknown'
    if os.getenv('RAILWAY_ENVIRONMENT_ID') and request.headers.get('x-real-ip'):
        value = request.headers['x-real-ip']
    try:
        address = ipaddress.ip_address(value)
        return str(ipaddress.ip_network(f'{address}/64', strict=False)) if address.version == 6 else str(address)
    except ValueError:
        return 'unknown'


def bucket_key(action, kind, seconds, identity):
    secret = SECRET_KEY
    if not secret:
        raise RuntimeError('Security limiter secret is unavailable')
    value = f'auth-limit-v1\0{action}\0{kind}\0{seconds}\0{identity}'
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def server_now(db):
    if db.bind.dialect.name == 'postgresql':
        return db.execute(text('SELECT CURRENT_TIMESTAMP')).scalar_one().astimezone(timezone.utc).replace(tzinfo=None)
    return datetime.utcnow()


def enforce(db, request, action, *, email='', guest=''):
    address = client_address(request)
    normalized_email = email.strip().casefold()
    values = {'ip': address, 'email': normalized_email, 'pair': address+'\0'+normalized_email, 'guest': str(guest)}
    policies = [(bucket_key(action, kind, seconds, values[kind]), maximum, seconds)
                for kind, maximum, seconds in POLICIES[action]]
    retry = 0
    try:
        # A separate committed transaction survives failed auth/main rollback and
        # works across workers/restarts. Sorted keys avoid cross-policy deadlocks.
        with Session(db.get_bind()) as limits:
            now = server_now(limits)
            for key, maximum, seconds in sorted(policies):
                expiry = now + timedelta(seconds=seconds)
                row = limits.execute(text('''
                    INSERT INTO security_rate_limits (bucket_key, hits, expires_at)
                    VALUES (:key, 1, :expiry)
                    ON CONFLICT (bucket_key) DO UPDATE SET
                      hits = CASE WHEN security_rate_limits.expires_at <= :now THEN 1
                                  ELSE security_rate_limits.hits + 1 END,
                      expires_at = CASE WHEN security_rate_limits.expires_at <= :now THEN :expiry
                                        ELSE security_rate_limits.expires_at END
                    RETURNING hits, expires_at
                '''), {'key': key, 'expiry': expiry, 'now': now}).one()
                expires = row.expires_at
                if isinstance(expires, str):
                    expires = datetime.fromisoformat(expires)
                if row.hits > maximum:
                    retry = max(retry, max(1, math.ceil((expires-now).total_seconds())))
            if random.randrange(64) == 0:
                # Bounded opportunistic retention; no request/session data retained.
                limits.execute(text('''DELETE FROM security_rate_limits WHERE bucket_key IN
                    (SELECT bucket_key FROM security_rate_limits WHERE expires_at < :old LIMIT 1000)'''),
                    {'old': now-timedelta(days=1)})
            limits.commit()
    except Exception as error:
        logging.getLogger(__name__).warning('Security limiter unavailable (%s)', type(error).__name__)
        raise HTTPException(503, 'Сервис временно недоступен. Попробуйте позже.') from None
    if retry:
        raise HTTPException(429, 'Слишком много запросов. Попробуйте позже.', headers={'Retry-After': str(retry)})
