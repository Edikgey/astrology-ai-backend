from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Dict, Literal, Optional, Union
from uuid import UUID


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
class NatalChartCreate(BaseModel):
    year: int
    month: int
    day: int
    hour: float
    lon: float
    lat: float
    city: str
    region: str
    country: str
 

class GPTInterpretationRequest(BaseModel):
    chart_id: int
    question: str = Field(...)

class GPTInterpretationResponse(BaseModel):
    chart_id: int
    response: str

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