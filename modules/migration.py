from sqlalchemy.orm import Session
from typing import Optional
from uuid import UUID
from database.queries import NatalChart, User
from modules.chart_limits import saved_chart_limit

def migrate_guest_data_to_user(
    db: Session, user_id: int, session_token: Optional[UUID],
    guest_chart_id: Optional[int] = None,
):
    """Transfer only the requested chart. The auth caller owns commit/rollback."""
    if guest_chart_id is None or session_token is None:
        return {"status": "not_requested", "chart_id": guest_chart_id}

    # Match creation's lock order: user first, then the individual guest chart.
    user = db.query(User).filter(User.id == user_id).populate_existing().with_for_update().one()
    chart = db.query(NatalChart).filter(
        NatalChart.id == guest_chart_id,
        NatalChart.session_token == session_token,
        NatalChart.user_id.is_(None),
    ).populate_existing().with_for_update().first()
    if chart is None:
        return {"status": "not_found", "chart_id": guest_chart_id}

    if db.query(NatalChart).filter(NatalChart.user_id == user_id).count() >= saved_chart_limit(user):
        return {"status": "limit_reached", "chart_id": guest_chart_id}

    chart.user_id = user_id
    chart.session_token = None
    db.flush()
    return {"status": "migrated", "chart_id": guest_chart_id}
