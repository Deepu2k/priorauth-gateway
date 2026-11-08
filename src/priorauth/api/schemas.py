import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from priorauth.models import PriorAuthRequest, StatusHistory


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class SubmissionAccepted(CamelModel):
    id: uuid.UUID
    status: str
    status_url: str


class RequestView(CamelModel):
    id: uuid.UUID
    status: str
    service_code: str
    patient_reference: str | None
    submitted_by: str
    attempts: int
    decision_reason: str | None
    authorization_number: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, r: PriorAuthRequest) -> "RequestView":
        return cls.model_validate(r)


class HistoryEntry(CamelModel):
    from_status: str | None
    to_status: str
    event: str
    detail: str | None
    actor: str
    attempt: int | None
    created_at: datetime

    @classmethod
    def of(cls, h: StatusHistory) -> "HistoryEntry":
        return cls.model_validate(h)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 (OAuth2 token type, not a secret)
    expires_in: int


class DeadLetterView(CamelModel):
    request_id: str | None
    attempts: int | None
    last_error: str | None


class DeadLetterQueueView(CamelModel):
    depth: int
    messages: list[DeadLetterView]
