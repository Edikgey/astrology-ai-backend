import json
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from api.auth import get_current_user
from database.connection import get_db
from database.queries import User
from modules import paddle_billing as billing

router = APIRouter(prefix="/payments/paddle", tags=["Payments"])


@router.post("/checkout")
def checkout(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return billing.create_checkout(db, user.id)


@router.post("/portal")
def portal(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return billing.create_portal(db, user.id)


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    billing.verify_signature(body, request.headers)
    try:
        event = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON") from None
    outcome = await run_in_threadpool(billing.process_event, db, event)
    return {"received": True, "outcome": outcome}
