"""Paid products the browser may name (PRICING-1, DESIGN §4).

The browser chooses only one of PRODUCTS. Amounts and the pack size are server constants approved by the
owner (DECISIONS №28) and are checked against the provider before every payment; the provider ids of the
new options come from the environment (names only in git). The month keeps its existing configuration.
"""
import os
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi import HTTPException

PRODUCTS = ("premium_month", "premium_year", "question_pack")
PACK_SIZE = 50
STRIPE_CENTS = {"premium_month": 999, "premium_year": 6999, "question_pack": 499}
LAVA_RUB = {"premium_month": Decimal("799"), "premium_year": Decimal("4790"), "question_pack": Decimal("399")}
LAVA_PERIODICITY = {"premium_month": "MONTHLY", "premium_year": "PERIOD_YEAR", "question_pack": "ONE_TIME"}
INTERVALS = {"premium_month": "month", "premium_year": "year"}
LAVA_SUBSCRIPTIONS = {"MONTHLY": "premium_month", "PERIOD_YEAR": "premium_year"}  # lava_checkouts.periodicity
VARIABLES = {
    ("stripe", "premium_year"): "STRIPE_PREMIUM_ANNUAL_PRICE_ID",
    ("stripe", "question_pack"): "STRIPE_QUESTION_PACK_PRICE_ID",
    ("lava", "premium_year"): "LAVA_ANNUAL_OFFER_ID",
    ("lava", "question_pack"): "LAVA_PACK_OFFER_ID",
}

UNAVAILABLE_OPTION = "Этот вариант оплаты пока недоступен. Выберите другой или попробуйте позже."
HAS_PREMIUM_YEAR = ("У вас уже есть Premium. Годовой тариф можно оформить, когда закончится текущий период: "
                    "отключите продление в «Мои карты».")
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
    """Refusals before any provider call. Returns the configured id of a new option (None for the month).

    The free beta does not refuse: the year and the pack are sold like the month (DECISIONS №41).
    """
    if product == "premium_month":
        return None
    value = option_id(provider, product)
    if value is None:
        raise HTTPException(503, UNAVAILABLE_OPTION)
    return value


def available(provider, product):
    if option_id(provider, product) is None:
        return False
    if provider == "stripe":
        from modules.stripe_billing import configured
    else:
        from modules.lava_client import configured
    return configured()


def with_product(url, product):
    """Return URL of one payment + exactly one closed `product` (DESIGN §13), before any `#`; the rest stays as is.

    The tab that comes back knows which confirmation to wait for; access is still decided by /account/usage.
    A variable that already names a product is a configuration error (ValueError).
    """
    if product not in PRODUCTS or "product" in parse_qs(urlsplit(url).query, keep_blank_values=True):
        raise ValueError("Return URL already names a product")
    base, hash_mark, fragment = url.partition("#")
    joiner = "" if base.endswith(("?", "&")) else "&" if "?" in base else "?"
    return base + joiner + "product=" + product + hash_mark + fragment


def public_offers():
    return {"premium_year": any(available(p, "premium_year") for p in ("stripe", "lava")),
            "question_pack": any(available(p, "question_pack") for p in ("stripe", "lava")),
            "pack_size": PACK_SIZE}
