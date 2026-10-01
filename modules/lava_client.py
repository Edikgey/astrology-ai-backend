"""Direct Lava API: bounded calls, sanitized diagnostics, no automatic retries."""
import os
import json
import logging
import time
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException

BASE_URL = "https://gate.lava.top"
PREMIUM_RUB = Decimal("799")
logger = logging.getLogger(__name__)


def request_operation(method, path):
    # Fixed labels only: never log a URL, query string or external identifier.
    if method == "POST" and path == "/api/v3/invoice":
        return "invoice_create"
    if method == "GET" and path.startswith("/api/v2/products"):
        return "catalog_read"
    if method == "GET" and path.startswith("/api/v1/subscriptions/"):
        return "subscription_read"
    if method == "GET" and path.startswith("/api/v2/invoices/"):
        return "invoice_read"
    if method == "DELETE" and path == "/api/v1/subscriptions":
        return "subscription_cancel"
    return "provider_request"


def external_id(value):
    return str(UUID(value))


def https_url(value):
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or len(value) > 2048:
        raise ValueError("Invalid payment URL")
    return value


def config():
    try:
        offer = external_id(os.environ["LAVA_OFFER_ID"])
        secret = os.environ["LAVA_WEBHOOK_SECRET"]
        if not os.environ.get("LAVA_API_KEY") or not 32 <= len(secret) <= 80:
            raise ValueError()
        returns = {}
        for field, variable in (("successful_return_url", "LAVA_SUCCESS_RETURN_URL"),
                                ("failure_return_url", "LAVA_FAILURE_RETURN_URL"),
                                ("cancel_return_url", "LAVA_CANCEL_RETURN_URL")):
            value = os.environ[variable]
            if len(value) > 512:
                raise ValueError()
            returns[field] = https_url(value)
        return offer, returns
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503, "Оплата в RUB пока не настроена.") from None


def configured():
    try:
        config()
        return True
    except HTTPException:
        return False


def request(method, path, **kwargs):
    key = os.getenv("LAVA_API_KEY")
    if not key:
        raise HTTPException(503, "Оплата в RUB пока не настроена.")
    operation = request_operation(method, path)
    timeout = (httpx.Timeout(connect=10, read=30, write=10, pool=5)
               if operation == "invoice_create" else httpx.Timeout(4, connect=2))
    started, stage, response = time.monotonic(), "request", None

    def log_failure(error):
        logger.warning("Lava request failed %s", json.dumps({
            "operation": operation, "stage": stage,
            "exception_class": type(error).__name__,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "http_status": response.status_code if response is not None else None,
        }))

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.request(method, BASE_URL + path,
                                      headers={"X-Api-Key": key, "Accept": "application/json"}, **kwargs)
        stage = "http_status"
        if response.status_code == 429:
            raise HTTPException(503, "Платёжный сервис занят. Попробуйте позже.", headers={"Retry-After": "60"})
        if not 200 <= response.status_code < 300:
            raise HTTPException(502, "Платёжный сервис временно недоступен.")
        if response.status_code == 204:
            return None
        stage = "decode_response"
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except HTTPException as error:
        log_failure(error)
        raise
    except (httpx.HTTPError, ValueError, TypeError) as error:
        log_failure(error)
        raise HTTPException(502, "Не удалось подтвердить ответ платёжного сервиса.") from None


def verified_offer(offer_id):
    """Reject catalog drift before creating a chargeable invoice; never set custom amount."""
    path = "/api/v2/products?showAllSubscriptionPeriods=true"
    for _ in range(20):
        catalog = request("GET", path)
        for item in catalog.get("items", []):
            # Current live response is flat; Swagger also describes a data wrapper.
            product = item.get("data", item)
            for offer in product.get("offers") or []:
                if offer.get("id") != offer_id:
                    continue
                prices = [p for p in offer.get("prices", []) if p.get("currency") == "RUB"
                          and p.get("periodicity") == "MONTHLY"]
                if (product.get("type") != "SUBSCRIPTION" or product.get("isDynamicPrice") is True
                        or len(prices) != 1 or Decimal(str(prices[0].get("amount"))) != PREMIUM_RUB):
                    raise HTTPException(503, "Цена подписки в RUB требует проверки. Оплата недоступна.")
                return external_id(product["id"])
        following = catalog.get("nextPage")
        if not following:
            break
        url = urlsplit(following)
        if url.scheme != "https" or url.netloc != "gate.lava.top" or url.path != "/api/v2/products":
            raise HTTPException(502, "Некорректный ответ каталога оплаты.")
        path = url.path + "?" + url.query
    raise HTTPException(503, "Тариф оплаты в RUB не найден.")
