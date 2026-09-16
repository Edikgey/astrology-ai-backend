from datetime import datetime
from pydantic import BaseModel, Field, model_validator
from typing import List, Dict, Literal, Optional, Union
from uuid import UUID
from models.follow_up import FollowUpSuggestion


class BodyData(BaseModel):
    symbol: str         # ⟵ символ тела (например, ☉)
    label: str          # ⟵ название тела (например, Солнце)
    degree: float       # ⟵ абсолютная эклиптическая долгота
    roundedDegree: str  # ⟵ округлённый градус в знаке (например, 15°)
    sign: str           # ⟵ знак зодиака (например, Лев)
    house: int          # ⟵ номер дома
    retrograde: bool    # ⟵ флаг ретроградности


class AspectData(BaseModel):
    from_body: str
    to_body: str
    aspect: Literal["☌", "☍", "△", "□", "⚹", "⚻", "∠", "∠∠", "Q", "bQ", "N", "S", "⚺"]



class PatternBodyResponse(BaseModel):
    symbol: str
    label: str
    degree: str
    sign: str

class PatternResponse(BaseModel):
    type: str
    bodies: List[PatternBodyResponse]

class HouseData(BaseModel):
    symbol: str
    degree: float

class NatalChartResponse(BaseModel):
    chart_id: int
    bodies_for_circle: Dict[str, BodyData]
    aspects_for_circle: List[AspectData]
    points_data: Optional[List[str]]  # ⬅️ Исправлено: теперь это список строк
    patterns_data: Optional[List[PatternResponse]]
    aspects_structured: Optional[Dict[str, List[str]]]
    houses: List[HouseData]
    timezone: Optional[str] = None
    birth_utc: Optional[datetime] = None
    house_system: Optional[str] = None
    time_provenance: str = 'legacy_unverified'
class SelectedLocation(BaseModel):
    city: str = Field(..., min_length=1)
    region: str
    country: str
    lat: float = Field(..., ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(..., ge=-180, le=180, allow_inf_nan=False)
    timezone: str = Field(..., min_length=1, max_length=100)


class NatalChartCreate(BaseModel):
    year: int
    month: int
    day: int
    hour: float = Field(..., ge=0, lt=24, allow_inf_nan=False)
    minute: Optional[int] = Field(None, ge=0, le=59)
    timezone: Optional[str] = Field(None, max_length=100)
    time_fold: Optional[int] = Field(None, ge=0, le=1)
    lon: float = Field(..., ge=-180, le=180, allow_inf_nan=False)
    lat: float = Field(..., ge=-90, le=90, allow_inf_nan=False)
    city: str
    region: str
    country: str
    # Browser selection snapshot catches stale/mixed fields; this is consistency
    # validation, not independent geocoding or provider authenticity verification.
    selected_location: Optional[SelectedLocation] = None

    @model_validator(mode='after')
    def location_fields_agree(self):
        if self.selected_location is not None:
            for field in ('city', 'region', 'country', 'lat', 'lon', 'timezone'):
                if getattr(self, field) != getattr(self.selected_location, field):
                    raise ValueError('Место рождения изменилось. Выберите его из подсказок заново.')
        return self
 

class GPTInterpretationRequest(BaseModel):
    chart_id: int
    question: str = Field(..., min_length=1, max_length=4000, pattern=r"\S")


class NatalChartSummary(BaseModel):
    chart_id: int
    year: Optional[int]
    month: Optional[int]
    day: Optional[int]
    hour: Optional[float]
    city: Optional[str]


class NatalChartListResponse(BaseModel):
    charts: List[NatalChartSummary]
    count: int
    limit: int

class GPTInterpretationResponse(BaseModel):
    chart_id: int
    response: str
    follow_up_suggestions: List[FollowUpSuggestion] = Field(default_factory=list)

class ChartIdRequest(BaseModel):
    chart_id: int


class GPTMessageResponse(BaseModel):
    id: int
    chart_id: int
    role: str
    content: str
    created_at: datetime

    class Config:
        orm_mode = True
