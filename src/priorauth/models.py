"""Tables: prior_auth_requests (current state) and status_history (append-only audit trail)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonType = JSON().with_variant(JSONB(), "postgresql")
STATUSES = "('received','sent','approved','denied','failed')"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class PriorAuthRequest(Base):
    __tablename__ = "prior_auth_requests"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUSES}", name="ck_requests_status"),
        CheckConstraint("patient IS NOT NULL OR patient_reference IS NOT NULL", name="ck_requests_patient"),
        Index("ix_requests_status", "status"),
        Index("ix_requests_service_code", "service_code"),
        Index("ix_requests_submitted_by_created", "submitted_by", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Either an inline FHIR Patient, or a reference ("Patient/123") the worker resolves on a FHIR server.
    patient: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    patient_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    coverage: Mapped[dict] = mapped_column(JsonType, nullable=False)
    service_request: Mapped[dict] = mapped_column(JsonType, nullable=False)
    service_code: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    history: Mapped[list["StatusHistory"]] = relationship(
        back_populates="request", order_by="StatusHistory.id", cascade="all, delete-orphan"
    )


class StatusHistory(Base):
    __tablename__ = "status_history"
    __table_args__ = (
        CheckConstraint(f"to_status IN {STATUSES}", name="ck_history_to_status"),
        Index("ix_history_request_id", "request_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("prior_auth_requests.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    request: Mapped[PriorAuthRequest] = relationship(back_populates="history")
