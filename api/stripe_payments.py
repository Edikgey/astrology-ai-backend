from uuid import uuid4
from sqlalchemy import inspect
from modules.product_analytics import capture_business, context as analytics_context
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from api.auth import get_current_user
from database.connection import get_db
from database.queries import User
from modules import stripe_billing as billing

router = APIRouter(prefix="/payments/stripe", tags=["Payments"])


@router.post("/checkout")
def checkout(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    result = billing.create_checkout(db, user.id)
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
