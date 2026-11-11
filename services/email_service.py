import os
import logging
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """Sanitized delivery failure; provider details must not reach logs or clients."""


async def send_confirmation_email(email: str, code: str):
    await send_email(email, 'Код подтверждения регистрации в Lunaria',
                     f'Ваш код подтверждения: {code}\n\nКод действует 10 минут.\nЕсли вы не запрашивали регистрацию, проигнорируйте это письмо.')


async def send_password_reset_email(email: str, token: str):
    # Fixed trusted origin; fragment never reaches frontend/proxy access logs.
    link = 'https://mylunariaai.com/reset-password#token=' + token
    await send_email(email, 'Восстановление пароля Lunaria',
                     f'Чтобы задать новый пароль, откройте ссылку:\n{link}\n\n'
                     'Ссылка действует 30 минут и может быть использована один раз.\n'
                     'Если вы не запрашивали восстановление, проигнорируйте это письмо. Ваш пароль не изменится.')


async def send_email(email: str, subject: str, body: str):
    try:
        username, password = os.environ["MAIL_USERNAME"], os.environ["MAIL_PASSWORD"]
        if not username.strip() or not password.strip():
            raise ValueError("Missing SMTP credentials")
        port = int(os.getenv("MAIL_PORT", "587"))
        if port not in (587, 2525):
            raise ValueError("A STARTTLS SMTP port is required")
        conf = ConnectionConfig(
            MAIL_USERNAME=username,
            MAIL_PASSWORD=password,
            MAIL_FROM=os.getenv("MAIL_FROM", "noreply@mylunariaai.com"),
            MAIL_FROM_NAME=os.getenv("MAIL_FROM_NAME", "Lunaria"),
            MAIL_PORT=port,
            MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp-relay.brevo.com"),
            MAIL_STARTTLS=True,
            MAIL_SSL_TLS=False,
            MAIL_DEBUG=0,
            SUPPRESS_SEND=0,
            USE_CREDENTIALS=True,
            VALIDATE_CERTS=True,
            TIMEOUT=20,
        )
        message = MessageSchema(
            subject=subject,
            recipients=[email],
            body=body,
            subtype="plain",
        )
        await FastMail(conf).send_message(message)
    except Exception as error:
        # SMTP exceptions can contain credentials, addresses or provider replies.
        logger.warning("Email delivery failed (%s)", type(error).__name__)
        raise EmailDeliveryError("Email delivery failed") from None
