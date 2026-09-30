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
def checkout(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return lava_billing.create_checkout(db, user.id)


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
