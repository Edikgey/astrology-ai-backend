from sqlalchemy.orm import Session
from database.queries import NatalChart

def migrate_guest_data_to_user(db: Session, user_id: int, session_token: str):
    db.query(NatalChart).filter(
        NatalChart.session_token == session_token,
        NatalChart.user_id == None
    ).update({
        NatalChart.user_id: user_id,
        NatalChart.session_token: None
    }, synchronize_session=False)

    db.commit()
