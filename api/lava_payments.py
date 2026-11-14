from uuid import uuid4
from sqlalchemy import inspect
from typing import Optional
from models.checkout import CheckoutRequest
from modules.product_analytics import capture_business, capture_payment_failure, context as analytics_context
import json
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from api.auth import get_current_user
from database.connection import get_db
from database.queries import User
from modules import lava_billing

router = APIRouter(prefix="/payments/lava", tags=["Payments"])


@router.post("/checkout")
def checkout(response: Response, body: Optional[CheckoutRequest] = None, user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    product = body.product if body else "premium_month"  # no body: the old monthly request
    try:
        result = lava_billing.create_checkout(db, user.id, product)
    except HTTPException as error:
        if error.status_code >= 500:
            capture_payment_failure(db, inspect(user).identity[0], 'lava', 'checkout', 'payment_failed:checkout:' + str(uuid4()))
        raise
    capture_business(db.get_bind(), inspect(user).identity[0], 'checkout_initiated',
        {'provider': 'lava'}, 'checkout:' + str(uuid4()), analytics_context.get())
    return result


@router.post("/cancel")
def cancel(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return lava_billing.cancel_subscription(db, user.id)


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    lava_billing.verify(request.headers)
    try:
        raw = await request.body()
        if len(raw) > 65536:
            raise ValueError()
        event = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid webhook body") from None
    outcome = await run_in_threadpool(lava_billing.process_event, db, event)
    return {"received": True, "outcome": outcome}
