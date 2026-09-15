"""Authenticated saved-chart pairs. No quota or AI operations."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from api.auth import get_current_user
from database.connection import get_db
from database.queries import User, NatalChart, ChartData, Relationship
from models.relationship import RelationshipCreate, RelationshipResponse, RelationshipListResponse
from modules.synastry import build_synastry_snapshot, SynastryInputError

router = APIRouter(prefix="/relationships", tags=["Relationships"])


def pair_query(db, user_id, a, b):
    return db.query(Relationship).filter(Relationship.user_id == user_id, or_(
        (Relationship.chart_a_id == a) & (Relationship.chart_b_id == b),
        (Relationship.chart_a_id == b) & (Relationship.chart_b_id == a)))


@router.post("", response_model=RelationshipResponse, status_code=201)
def create_relationship(data: RelationshipCreate, response: Response,
                        current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user_id = current_user.id
    try:
        # Same lock order as natal creation/deletion, without mutating plan/usage.
        if db.query(User).filter_by(id=user_id).with_for_update().first() is None:
            raise HTTPException(401, "Войдите в аккаунт.")
        charts = db.query(NatalChart).filter(NatalChart.id.in_([data.chart_a_id, data.chart_b_id]),
                                             NatalChart.user_id == user_id).order_by(NatalChart.id).with_for_update().all()
        if len(charts) != 2:
            raise HTTPException(404, "Одна из карт не найдена или недоступна. Выберите свои сохранённые карты.")
        existing = pair_query(db, user_id, data.chart_a_id, data.chart_b_id).first()
        if existing is not None:
            result = RelationshipResponse.model_validate(existing)
            db.commit()
            response.status_code = 200
            return result
        by_id = {chart.id: chart for chart in charts}
        saved = db.query(ChartData).filter(ChartData.chart_id.in_(by_id)).all()
        # ChartData's one-to-one ORM relationship has no DB unique constraint.
        # Ambiguous stored calculations must not be picked arbitrarily.
        if len(saved) != 2 or len({row.chart_id for row in saved}) != 2:
            raise HTTPException(409, detail={"code": "SYNASTRY_DATA_UNAVAILABLE",
                "message": "Нужны однозначные сохранённые расчёты двух карт. Создайте актуальные карты через обычную форму."})
        stored = {row.chart_id: row for row in saved}
        try:
            snapshot = build_synastry_snapshot(by_id[data.chart_a_id], stored[data.chart_a_id],
                                              by_id[data.chart_b_id], stored[data.chart_b_id])
        except SynastryInputError as error:
            raise HTTPException(409, detail={"code": "SYNASTRY_DATA_UNAVAILABLE", "participant": error.participant,
                "reason": error.reason, "message": "Карта не содержит проверенных данных для анализа отношений. Создайте актуальную карту через обычную форму."}) from None
        row = Relationship(user_id=user_id, **data.model_dump(), calculation=snapshot,
                           ruleset_version=snapshot["ruleset_version"])
        db.add(row)
        db.flush()
        result = RelationshipResponse.model_validate(row)
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        # DB uniqueness remains authoritative even for a writer bypassing our lock.
        existing = pair_query(db, user_id, data.chart_a_id, data.chart_b_id).first()
        if existing is None:
            db.rollback()
            raise HTTPException(409, detail={"code": "RELATIONSHIP_CONFLICT",
                                            "message": "Карты изменились. Обновите список и повторите действие."}) from None
        result = RelationshipResponse.model_validate(existing)
        db.rollback()
        response.status_code = 200
        return result
    except BaseException:
        db.rollback()
        raise


@router.get("", response_model=RelationshipListResponse)
def list_relationships(chart_id: int | None = Query(default=None, gt=0),
                       current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(Relationship).filter_by(user_id=current_user.id)
    if chart_id is not None:
        query = query.filter(or_(Relationship.chart_a_id == chart_id, Relationship.chart_b_id == chart_id))
    rows = query.order_by(Relationship.id.desc()).all()
    return {"relationships": rows, "count": len(rows)}


@router.get("/{relationship_id}", response_model=RelationshipResponse)
def get_relationship(relationship_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(Relationship).filter_by(id=relationship_id, user_id=current_user.id).first()
    if row is None:
        raise HTTPException(404, "Анализ отношений не найден или недоступен.")
    return row


@router.delete("/{relationship_id}", status_code=204)
def delete_relationship(relationship_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        db.query(User).filter_by(id=current_user.id).with_for_update().one()
        row = db.query(Relationship).filter_by(id=relationship_id, user_id=current_user.id).with_for_update().first()
        if row is None:
            raise HTTPException(404, "Анализ отношений не найден или недоступен.")
        db.delete(row)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return Response(status_code=204)
