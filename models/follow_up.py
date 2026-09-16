"""Response-only suggestion contract shared by natal and relationship answers."""
from typing import Literal, get_args
from pydantic import BaseModel, ConfigDict, Field, field_validator

FollowUpType = Literal['deepen', 'personalize', 'explore']
FOLLOW_UP_TYPES = get_args(FollowUpType)


class FollowUpSuggestion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: FollowUpType
    text: str = Field(strict=True, min_length=1, max_length=100)

    @field_validator('text')
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value or any(char in value for char in '\r\n'):
            raise ValueError('A suggestion must be a short, single-line question.')
        if value.casefold().startswith(tuple(kind + ':' for kind in FOLLOW_UP_TYPES)):
            raise ValueError('Suggestion types belong outside the question text.')
        return value
