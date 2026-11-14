from uuid import uuid4
from sqlalchemy import inspect
from typing import Optional
from models.checkout import CheckoutRequest
from modules.product_analytics import capture_business, capture_payment_failure, context as analytics_context
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from api.auth import get_current_user
from database.connection import get_db
from database.queries import User
from modules import stripe_billing as billing

router = APIRouter(prefix="/payments/stripe", tags=["Payments"])


@router.post("/checkout")
def checkout(response: Response, body: Optional[CheckoutRequest] = None, user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    product = body.product if body else "premium_month"  # no body: the old monthly request
    try:
        result = billing.create_checkout(db, user.id, product)
    except HTTPException as error:
        if error.status_code >= 500:
            capture_payment_failure(db, inspect(user).identity[0], 'stripe', 'checkout', 'payment_failed:checkout:' + str(uuid4()))
        raise
    capture_business(db.get_bind(), inspect(user).identity[0], 'checkout_initiated',
        {'provider': 'stripe'}, 'checkout:' + str(uuid4()), analytics_context.get())
    return result


@router.get("/checkout/{session_id}")
def status(session_id: str, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return billing.checkout_status(db, user.id, session_id)


@router.post("/portal")
def portal(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return billing.create_portal(db, user.id)


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 262144: raise HTTPException(413, "Webhook body too large")
    event = billing.verify_signature(bytes(body), request.headers)
    outcome = await run_in_threadpool(billing.process_event, db, event)
    return {"received": True, "outcome": outcome}
