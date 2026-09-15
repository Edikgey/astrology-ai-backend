"""Two explicit conversation subjects; existing natal entry points remain defaults."""
from fastapi import HTTPException
from database.queries import (NatalChart, Relationship, GPTMessage, GPTConversation,
                              RelationshipMessage, RelationshipConversation)


def subject_fields(chart_id=None, relationship_id=None):
    if (chart_id is None) == (relationship_id is None):
        raise ValueError("Exactly one conversation subject is required")
    return {"chart_id": chart_id} if relationship_id is None else {"relationship_id": relationship_id}


def conversation_tables(relationship_id=None):
    return (GPTMessage, GPTConversation) if relationship_id is None else (RelationshipMessage, RelationshipConversation)


def owned_subject(db, user_id, chart_id=None, relationship_id=None, *, lock=False):
    subject_fields(chart_id, relationship_id)
    if user_id is None:
        raise HTTPException(401, "Войдите в аккаунт.")
    if relationship_id is None:
        query = db.query(NatalChart).filter_by(id=chart_id, user_id=user_id)
        row = (query.populate_existing().with_for_update() if lock else query).first()
    else:
        query = db.query(Relationship).filter_by(id=relationship_id, user_id=user_id)
        row = query.first()
        if row is not None:
            # Caller owns user lock for mutations. Match create: user -> sorted charts -> relationship.
            charts = db.query(NatalChart).filter(NatalChart.id.in_([row.chart_a_id, row.chart_b_id]),
                                                 NatalChart.user_id == user_id).order_by(NatalChart.id)
            if len((charts.populate_existing().with_for_update() if lock else charts).all()) != 2:
                row = None
            elif lock:
                row = query.populate_existing().with_for_update().first()
    if row is None:
        raise HTTPException(404, "Карта больше недоступна." if relationship_id is None else "Анализ отношений больше недоступен.")
    return row
