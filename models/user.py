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

class GoogleLogin(BaseModel):
    credential: str = Field(min_length=1, max_length=16384, repr=False)
    nonce: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$", repr=False)
    password: Optional[str] = Field(default=None, max_length=1024, repr=False)
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
