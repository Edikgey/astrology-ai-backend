import re
from fastapi import FastAPI, Response
from modules.free_beta import free_beta_enabled
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from modules.auth_origins import AUTH_ORIGINS
from api import endpoints, auth, stripe_payments, relationships, lava_payments, feedback
from modules.auth_body_limit import AuthBodyLimit

from contextlib import asynccontextmanager
from api import product_analytics as analytics_api
from modules import product_analytics as analytics
from modules.product_analytics_context import ProductAnalyticsContext


@asynccontextmanager
async def analytics_lifespan(app):
    analytics.start_worker()
    try:
        yield
    finally:
        analytics.stop_worker()


api_app = FastAPI(lifespan=analytics_lifespan)


@api_app.get("/public-config")
def public_config(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"free_beta_enabled": free_beta_enabled()}


FIELD_MESSAGES = {
    "email": "Введите email в формате name@example.com.",
    "code": "Введите 6 цифр кода из письма.",
    "token": auth.RESET_LINK_INVALID,
    "X-Session-Token": auth.GUEST_SESSION,
    "password": "Введите пароль.",
    "password_confirmation": "Введите подтверждение пароля.",
}
VALUE_ERROR_PREFIX = "Value error, "


def validation_message(error):
    # Every message is a fixed string: Pydantic and email_validator texts repeat user input.
    if error["type"] in ("password_policy", "password_confirmation"):
        return error["msg"]
    field = error["loc"][-1] if error["loc"] else None
    if field in FIELD_MESSAGES:
        return FIELD_MESSAGES[field]
    # Our model validators raise fixed Russian texts only; keep it so (no input in them).
    rest = error["msg"][len(VALUE_ERROR_PREFIX):]
    if error["type"] == "value_error" and error["msg"].startswith(VALUE_ERROR_PREFIX) and re.search("[А-Яа-яЁё]", rest):
        return rest
    return "Проверьте введённые данные и попробуйте ещё раз."


@api_app.exception_handler(RequestValidationError)
async def safe_validation_error(request, exc):
    # Pydantic's default errors include raw input (including passwords/Google tokens).
    errors = [{"loc": error["loc"], "type": error["type"], "msg": validation_message(error)} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})

api_app.include_router(auth.router)
api_app.include_router(endpoints.router)
api_app.include_router(stripe_payments.router)
api_app.include_router(lava_payments.router)
api_app.include_router(relationships.router)
api_app.include_router(feedback.router)
api_app.include_router(analytics_api.router)

# Wrap ServerErrorMiddleware too, so unhandled 500s retain CORS headers.
# Keep the existing explicit local/production origin allowlist.
app = CORSMiddleware(
    app=AuthBodyLimit(ProductAnalyticsContext(api_app)),
    allow_origins=AUTH_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)
