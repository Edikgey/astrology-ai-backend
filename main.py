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


@api_app.exception_handler(RequestValidationError)
async def safe_validation_error(request, exc):
    # Pydantic's default errors include raw input (including passwords/Google tokens).
    errors = []
    for error in exc.errors():
        message = error["msg"]
        if error["loc"][-1:] in (("password",), ("password_confirmation",)) and error["type"] not in ("password_policy", "password_confirmation"):
            message = "Введите пароль." if error["loc"][-1] == "password" else "Введите подтверждение пароля."
        errors.append({"loc": error["loc"], "type": error["type"], "msg": message})
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
