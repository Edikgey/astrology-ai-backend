import os
import logging
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """Sanitized delivery failure; provider details must not reach logs or clients."""


async def send_confirmation_email(email: str, code: str):
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
            subject="Код подтверждения регистрации в Lunaria",
            recipients=[email],
            body=f"Ваш код подтверждения: {code}\n\nКод действует 10 минут.\nЕсли вы не запрашивали регистрацию, проигнорируйте это письмо.",
            subtype="plain",
        )
        await FastMail(conf).send_message(message)
    except Exception as error:
        # SMTP exceptions can contain credentials, addresses or provider replies.
        logger.warning("Confirmation email delivery failed (%s)", type(error).__name__)
        raise EmailDeliveryError("Email delivery failed") from None
