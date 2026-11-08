"""What the worker does with one message. No RabbitMQ code here, so all of it is unit-testable.

Three short database transactions per attempt, and none is held open during the network calls:
  1. lock the row, skip duplicates, mark SENT
  2. (no transaction) resolve the patient on the FHIR server if needed, call the health plan
  3. lock the row again and record the decision, a retry, or the failure
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from priorauth import metrics, repository
from priorauth.clients import PermanentError, RetryableError
from priorauth.clients.fhir_server import FhirClient
from priorauth.clients.payer import PayerClient, PayerDecision, build_bundle
from priorauth.log import get_logger
from priorauth.status import TERMINAL, Status

log = get_logger(__name__)
ACTOR = "worker"


class Outcome(StrEnum):
    DONE = "done"  # finished (approved / denied) or a duplicate: ack
    RETRY = "retry"  # failed, try again after the delay
    DEAD = "dead"  # give up: dead letter queue


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    error: str | None = None


class Processor:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        payer: PayerClient,
        fhir: FhirClient,
        max_attempts: int,
    ):
        self._sessions = session_factory
        self._payer = payer
        self._fhir = fhir
        self._max_attempts = max_attempts

    def process(self, request_id: uuid.UUID, attempt: int) -> Result:
        # 1. claim
        with self._sessions() as s, s.begin():
            req = repository.get_request_for_update(s, request_id)
            if req is None:
                log.error("unknown_request")
                metrics.DEAD_LETTERED.labels("unknown_request").inc()
                return Result(Outcome.DEAD, "request id not in the database")
            if Status(req.status) in TERMINAL:
                # At-least-once delivery means duplicates happen. Already decided: nothing to do.
                log.info("duplicate_skipped", status=req.status)
                return Result(Outcome.DONE)
            repository.transition(s, req, Status.SENT, event="sent_to_payer", actor=ACTOR, attempt=attempt)
            req.attempts = attempt
            patient, patient_ref = req.patient, req.patient_reference
            bundle_parts = (req.coverage, req.service_request)

        # 2. external calls
        fetched_patient = None
        decision: PayerDecision | None = None
        error: Exception | None = None
        try:
            if patient is None and patient_ref:
                fetched_patient = self._fhir.get_patient(patient_ref)
                patient = fetched_patient
                log.info("patient_resolved", patient_reference=patient_ref)
            decision = self._payer.submit(build_bundle(patient, *bundle_parts), correlation_id=str(request_id))
        except (RetryableError, PermanentError) as exc:
            error = exc

        # 3. record
        with self._sessions() as s, s.begin():
            req = repository.get_request_for_update(s, request_id)
            if req is None or Status(req.status) in TERMINAL:
                return Result(Outcome.DONE)
            if fetched_patient is not None:
                req.patient = fetched_patient
            if decision is not None:
                return self._record_decision(s, req, decision, attempt)
            assert error is not None
            return self._record_failure(s, req, error, attempt)

    def _record_decision(self, s, req, decision: PayerDecision, attempt: int) -> Result:
        target = Status.APPROVED if decision.decision == "approved" else Status.DENIED
        repository.transition(s, req, target, event="decision", actor=ACTOR, detail=decision.reason, attempt=attempt)
        req.decision_reason = decision.reason
        req.authorization_number = decision.authorization_number
        req.last_error = None
        metrics.DECISIONS.labels(decision.decision).inc()
        log.info("decision_recorded", decision=decision.decision, reason=decision.reason)
        return Result(Outcome.DONE)

    def _record_failure(self, s, req, error: Exception, attempt: int) -> Result:
        msg = str(error)
        req.last_error = msg
        if isinstance(error, RetryableError) and attempt < self._max_attempts:
            repository.transition(
                s, req, Status.SENT, event="retry_scheduled", actor=ACTOR, detail=msg, attempt=attempt
            )
            metrics.RETRIES.inc()
            log.warning("attempt_failed_will_retry", error=msg, max_attempts=self._max_attempts)
            return Result(Outcome.RETRY, msg)

        reason = "retries_exhausted" if isinstance(error, RetryableError) else "permanent_error"
        repository.transition(s, req, Status.FAILED, event=reason, actor=ACTOR, detail=msg, attempt=attempt)
        metrics.REQUESTS_FAILED.labels(reason).inc()
        metrics.DEAD_LETTERED.labels(reason).inc()
        log.error("request_failed", reason=reason, error=msg)
        return Result(Outcome.DEAD, msg)
