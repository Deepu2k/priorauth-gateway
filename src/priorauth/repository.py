"""All writes to the two tables go through here, so every status change lands in status_history."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from priorauth.fhir import PriorAuthSubmission
from priorauth.models import PriorAuthRequest, StatusHistory
from priorauth.status import Status, check_transition


def create_request(session: Session, submission: PriorAuthSubmission, submitted_by: str) -> PriorAuthRequest:
    req = PriorAuthRequest(
        id=uuid.uuid4(),
        status=Status.RECEIVED,
        patient=submission.patient.model_dump(mode="json", exclude_none=True) if submission.patient else None,
        patient_reference=submission.patientReference,
        coverage=submission.coverage.model_dump(mode="json", exclude_none=True),
        service_request=submission.serviceRequest.model_dump(mode="json", exclude_none=True),
        service_code=submission.serviceRequest.procedure_code(),
        submitted_by=submitted_by,
        attempts=0,
    )
    session.add(req)
    session.add(
        StatusHistory(
            request_id=req.id, from_status=None, to_status=Status.RECEIVED, event="received", actor=submitted_by
        )
    )
    return req


def get_request(session: Session, request_id: uuid.UUID, *, with_history: bool = False) -> PriorAuthRequest | None:
    stmt = select(PriorAuthRequest).where(PriorAuthRequest.id == request_id)
    if with_history:
        stmt = stmt.options(selectinload(PriorAuthRequest.history))
    return session.scalars(stmt).one_or_none()


def get_request_for_update(session: Session, request_id: uuid.UUID) -> PriorAuthRequest | None:
    """Row lock, so two workers holding duplicate messages cannot both move the same request."""
    stmt = select(PriorAuthRequest).where(PriorAuthRequest.id == request_id).with_for_update()
    return session.scalars(stmt).one_or_none()


def transition(
    session: Session,
    req: PriorAuthRequest,
    target: Status,
    *,
    event: str,
    actor: str,
    detail: str | None = None,
    attempt: int | None = None,
) -> None:
    current = Status(req.status)
    check_transition(current, target)
    req.status = target
    session.add(
        StatusHistory(
            request_id=req.id,
            from_status=current,
            to_status=target,
            event=event,
            detail=detail,
            actor=actor,
            attempt=attempt,
        )
    )
