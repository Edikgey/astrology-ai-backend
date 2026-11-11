from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from modules.password_policy import password_errors
from typing import Literal, Optional

class NewPassword(BaseModel):
    password: str = Field(repr=False)
    password_confirmation: Optional[str] = Field(default=None, repr=False)

    @field_validator("password", mode="before")
    @classmethod
    def validate_new_password(cls, value):
        errors = password_errors(value)
        if errors:
            raise PydanticCustomError("password_policy", " ".join(errors))
        return value

    @model_validator(mode="after")
    def confirm_password(self):
        # Older clients need not send a confirmation; the policy is always enforced.
        if self.password_confirmation is not None and self.password_confirmation != self.password:
            raise PydanticCustomError("password_confirmation", "Пароли не совпадают.")
        return self

class UserCreate(NewPassword):
    email: EmailStr


class UserVerify(UserCreate):
    guest_chart_id: Optional[int] = Field(default=None, gt=0)
    code: Optional[str] = Field(default=None, min_length=6, max_length=6, pattern=r'^\d{6}$', repr=False)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(NewPassword):
    password_confirmation: str = Field(repr=False)
    token: str = Field(min_length=43, max_length=43, pattern=r'^[A-Za-z0-9_-]{43}$', repr=False)

class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(repr=False)
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
