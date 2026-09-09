import os
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig


async def send_confirmation_email(email: str, code: str):
    # Локальная разработка: письмо не отправляем,
    # код показываем в консоли backend
    if os.getenv("DEV_MODE", "true").lower() == "true":
        print(f"\n[DEV] Код подтверждения для {email}: {code}\n")
        return

    conf = ConnectionConfig(
        MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
        MAIL_FROM=os.getenv("MAIL_FROM"),
        MAIL_PORT=587,
        MAIL_SERVER="smtp.zoho.eu",
        MAIL_STARTTLS=True,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=True,
        VALIDATE_CERTS=True,
    )

    message = MessageSchema(
        subject="Код подтверждения регистрации",
        recipients=[email],
        body=f"Ваш код подтверждения: {code}",
        subtype="plain",
    )

    fm = FastMail(conf)
    await fm.send_message(message)