from datetime import datetime
from typing import Literal
import unicodedata
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_a_id: int = Field(gt=0, strict=True)
    chart_b_id: int = Field(gt=0, strict=True)
    person_a_label: str = Field(min_length=1, max_length=100)
    person_b_label: str = Field(min_length=1, max_length=100)
    speaker_person: Literal["A", "B"] | None = None

    @field_validator("person_a_label", "person_b_label")
    @classmethod
    def clean_label(cls, value):
        value = value.strip()
        if not value or any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Укажите имя от 1 до 100 символов без управляющих символов.")
        return value

    @model_validator(mode="after")
    def distinct_charts(self):
        if self.chart_a_id == self.chart_b_id:
            raise ValueError("Выберите две разные натальные карты.")
        return self


class RelationshipSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    chart_a_id: int
    chart_b_id: int
    person_a_label: str
    person_b_label: str
    speaker_person: Literal["A", "B"] | None
    ruleset_version: str
    created_at: datetime


class RelationshipResponse(RelationshipSummary):
    calculation: dict


class RelationshipListResponse(BaseModel):
    relationships: list[RelationshipSummary]
    count: int


class RelationshipAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class RelationshipAnswer(BaseModel):
    relationship_id: int
    response: str
    follow_up_suggestions: list[str] = Field(default_factory=list)


class RelationshipMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    relationship_id: int
    role: str
    content: str
    created_at: datetime
