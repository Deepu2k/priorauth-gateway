import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from priorauth import metrics, repository
from priorauth.api.schemas import (
    DeadLetterQueueView,
    DeadLetterView,
    HistoryEntry,
    RequestView,
    SubmissionAccepted,
    Token,
)
from priorauth.config import Settings, get_settings
from priorauth.db import get_session
from priorauth.fhir import PriorAuthSubmission
from priorauth.log import get_logger
from priorauth.models import PriorAuthRequest
from priorauth.queue import Publisher, peek_dlq
from priorauth.security import Principal, Role, authenticate_user, issue_token, require_role
from priorauth.status import Status

log = get_logger(__name__)
router = APIRouter()


def get_publisher(request: Request) -> Publisher:
    return request.app.state.publisher


@router.post("/auth/token", response_model=Token, tags=["auth"], summary="Log in as a demo user")
def login(form: OAuth2PasswordRequestForm = Depends(), settings: Settings = Depends(get_settings)) -> Token:
    principal = authenticate_user(settings, form.username, form.password)
    if principal is None:
        log.warning("login_failed", username=form.username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong username or password")
    token, ttl = issue_token(settings, principal)
    log.info("login_ok", username=principal.subject, roles=sorted(principal.roles))
    return Token(access_token=token, expires_in=ttl)


@router.post(
    "/requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmissionAccepted,
    response_model_by_alias=True,
    tags=["prior-auth"],
    summary="Submit a prior authorization request",
    responses={
        422: {"description": "Invalid request, returned as a FHIR OperationOutcome"},
        503: {"description": "Queue unavailable; the request is saved as failed"},
    },
)
def submit(
    body: PriorAuthSubmission,
    response: Response,
    principal: Principal = Depends(require_role(Role.SUBMITTER)),
    session: Session = Depends(get_session),
    publisher: Publisher = Depends(get_publisher),
) -> SubmissionAccepted:
    req = repository.create_request(session, body, submitted_by=principal.subject)
    session.commit()
    log.info("request_received", request_id=str(req.id), service_code=req.service_code, actor=principal.subject)

    try:
        publisher.publish(req.id)
    except Exception as exc:
        # The row is committed but no message exists, so nothing would ever process it. Mark it failed
        # so the client sees a clear answer and can resubmit. (A transactional outbox would remove
        # this window; see the README.)
        repository.transition(
            session, req, Status.FAILED, event="enqueue_failed", actor="api", detail=type(exc).__name__
        )
        req.last_error = f"could not enqueue: {type(exc).__name__}"
        session.commit()
        metrics.REQUESTS_FAILED.labels(reason="enqueue_failed").inc()
        log.error("enqueue_failed", request_id=str(req.id), error=repr(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable; request {req.id} was saved as failed, please resubmit",
        ) from None

    metrics.REQUESTS_RECEIVED.inc()
    log.info("request_enqueued", request_id=str(req.id))
    url = f"/requests/{req.id}"
    response.headers["Location"] = url
    return SubmissionAccepted(id=req.id, status=req.status, status_url=url)


def _load_visible(session: Session, request_id: uuid.UUID, principal: Principal, with_history=False):
    req = repository.get_request(session, request_id, with_history=with_history)
    # A submitter only sees its own requests. Answer 404, not 403, so ids of other partners' requests leak nothing.
    if req is None or not (principal.has_any(Role.VIEWER) or req.submitted_by == principal.subject):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="request not found")
    return req


@router.get("/requests/{request_id}", response_model=RequestView, response_model_by_alias=True, tags=["prior-auth"])
def get_one(
    request_id: uuid.UUID,
    principal: Principal = Depends(require_role(Role.SUBMITTER, Role.VIEWER)),
    session: Session = Depends(get_session),
) -> RequestView:
    return RequestView.of(_load_visible(session, request_id, principal))


@router.get(
    "/requests/{request_id}/history",
    response_model=list[HistoryEntry],
    response_model_by_alias=True,
    tags=["prior-auth"],
    summary="Audit trail of every status change",
)
def history(
    request_id: uuid.UUID,
    principal: Principal = Depends(require_role(Role.SUBMITTER, Role.VIEWER)),
    session: Session = Depends(get_session),
) -> list[HistoryEntry]:
    req = _load_visible(session, request_id, principal, with_history=True)
    return [HistoryEntry.of(h) for h in req.history]


@router.get("/requests", response_model=list[RequestView], response_model_by_alias=True, tags=["prior-auth"])
def list_requests(
    status_filter: Status | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    _: Principal = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> list[RequestView]:
    stmt = select(PriorAuthRequest).order_by(PriorAuthRequest.created_at.desc()).limit(limit)
    if status_filter is not None:
        stmt = stmt.where(PriorAuthRequest.status == status_filter)
    return [RequestView.of(r) for r in session.scalars(stmt)]


@router.get(
    "/admin/dlq",
    response_model=DeadLetterQueueView,
    response_model_by_alias=True,
    tags=["admin"],
    summary="Inspect the dead letter queue without removing messages",
)
def dead_letters(
    limit: int = Query(default=20, ge=1, le=200),
    _: Principal = Depends(require_role(Role.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> DeadLetterQueueView:
    depth, items = peek_dlq(settings.amqp_url, settings.retry_delay_ms, limit)
    return DeadLetterQueueView(
        depth=depth,
        messages=[DeadLetterView(request_id=i.request_id, attempts=i.attempts, last_error=i.last_error) for i in items],
    )


@router.get("/health/live", tags=["health"], include_in_schema=False)
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"], include_in_schema=False)
def ready(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}
