"""Paid products the browser may name (PRICING-1, DESIGN §4).

The browser chooses only one of PRODUCTS. Amounts and the pack size are server constants approved by the
owner (DECISIONS №28) and are checked against the provider before every payment; the provider ids of the
new options come from the environment (names only in git). The month keeps its existing configuration.
"""
import os
import re
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException

from modules.free_beta import free_beta_enabled

PRODUCTS = ("premium_month", "premium_year", "question_pack")
PACK_SIZE = 50
STRIPE_CENTS = {"premium_month": 999, "premium_year": 6999, "question_pack": 499}
LAVA_RUB = {"premium_month": Decimal("799"), "premium_year": Decimal("4790"), "question_pack": Decimal("399")}
LAVA_PERIODICITY = {"premium_month": "MONTHLY", "premium_year": "PERIOD_YEAR", "question_pack": "ONE_TIME"}
INTERVALS = {"premium_month": "month", "premium_year": "year"}
VARIABLES = {
    ("stripe", "premium_year"): "STRIPE_PREMIUM_ANNUAL_PRICE_ID",
    ("stripe", "question_pack"): "STRIPE_QUESTION_PACK_PRICE_ID",
    ("lava", "premium_year"): "LAVA_ANNUAL_OFFER_ID",
    ("lava", "question_pack"): "LAVA_PACK_OFFER_ID",
}

UNAVAILABLE_OPTION = "Этот вариант оплаты пока недоступен. Выберите другой или попробуйте позже."
BETA_REFUSALS = {
    "premium_year": "Годовой тариф появится после окончания бесплатной беты.",
    "question_pack": "Сейчас идёт бесплатная бета: вопросы без лимита, пакет не нужен.",
}
PACK_PENDING = ("Проверяем оплату пакета. Не оплачивайте повторно — обновите страницу через минуту. "
                "Если сообщение повторится, напишите в поддержку: lunariaai.support@gmail.com.")


def option_id(provider, product):
    """The configured provider id of a new option, or None when it is absent or malformed."""
    value = os.getenv(VARIABLES[(provider, product)], "")
    if provider == "stripe":
        return value if re.fullmatch(r"price_[A-Za-z0-9]+", value) else None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def require(provider, product):
    """Refusals before any provider call. Returns the configured id of a new option (None for the month)."""
    if product == "premium_month":
        return None
    if free_beta_enabled():
        raise HTTPException(409, BETA_REFUSALS[product])
    value = option_id(provider, product)
    if value is None:
        raise HTTPException(503, UNAVAILABLE_OPTION)
    return value


def available(provider, product):
    if free_beta_enabled() or option_id(provider, product) is None:
        return False
    if provider == "stripe":
        from modules.stripe_billing import configured
    else:
        from modules.lava_client import configured
    return configured()


def public_offers():
    return {"premium_year": any(available(p, "premium_year") for p in ("stripe", "lava")),
            "question_pack": any(available(p, "question_pack") for p in ("stripe", "lava")),
            "pack_size": PACK_SIZE}
