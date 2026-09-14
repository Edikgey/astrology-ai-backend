"""Verify GIS ID tokens with Google's library; never log credentials or claims."""
import hmac
import os
import time
from fastapi import HTTPException
from google.oauth2 import id_token
from google.auth.transport.requests import Request
from google.auth.exceptions import TransportError, GoogleAuthError
from email_validator import validate_email, EmailNotValidError


def verify_google_credential(credential, nonce):
    client_id = os.getenv('GOOGLE_CLIENT_ID', '').strip()
    if not client_id:
        raise HTTPException(503, detail='Вход через Google пока не настроен. Используйте email и пароль.')
    try:
        # Only Google's fixed certificate endpoint is used. Bound network waiting.
        request = Request()
        claims = id_token.verify_oauth2_token(
            credential, lambda *args, **kwargs: request(*args, **{**kwargs, 'timeout': 10}),
            audience=client_id,
        )
        if claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
            raise ValueError('issuer')
        if claims.get('aud') != client_id or claims.get('azp', client_id) != client_id:
            raise ValueError('audience')
        if not isinstance(claims.get('exp'), (int, float)) or claims['exp'] <= time.time():
            raise ValueError('expiry')
        sub = claims.get('sub')
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise ValueError('subject')
        if claims.get('email_verified') is not True:
            raise ValueError('email not verified')
        claim_nonce = claims.get('nonce')
        if not isinstance(claim_nonce, str) or not hmac.compare_digest(claim_nonce, nonce):
            raise ValueError('nonce')
        email = validate_email(claims.get('email', ''), check_deliverability=False).normalized.lower()
        authoritative = email.endswith('@gmail.com') or bool(claims.get('hd'))
        return {'sub': sub, 'email': email, 'authoritative': authoritative}
    except TransportError:
        raise HTTPException(503, detail='Google временно недоступен. Попробуйте снова.') from None
    except (ValueError, TypeError, KeyError, EmailNotValidError, GoogleAuthError):
        raise HTTPException(401, detail='Не удалось подтвердить вход Google. Войдите через Google ещё раз.') from None
