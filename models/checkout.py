from typing import Literal
from pydantic import BaseModel


class CheckoutRequest(BaseModel):
    """The browser names a product only; extra fields (old clients) are ignored, never used (DESIGN §12)."""
    product: Literal["premium_month", "premium_year", "question_pack"] = "premium_month"
