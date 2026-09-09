from sqlalchemy import Column, String, DateTime, Boolean
from database.connection import Base
from datetime import datetime

class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    email = Column(String, primary_key=True, index=True)
    code = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

