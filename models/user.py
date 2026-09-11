from pydantic import BaseModel, EmailStr, Field
from typing import Literal, Optional

class UserCreate(BaseModel):
    email: EmailStr
    password: str

class UserVerify(UserCreate):
    guest_chart_id: Optional[int] = Field(default=None, gt=0)

class UserLogin(BaseModel):
    email: EmailStr
    password: str
    guest_chart_id: Optional[int] = Field(default=None, gt=0)

class GuestChartMigrationResult(BaseModel):
    status: Literal["migrated", "limit_reached", "not_requested", "not_found"]
    chart_id: Optional[int] = None

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    guest_chart_migration: GuestChartMigrationResult

class UserResponse(BaseModel):
    id: int
    email: str
