"""Worker logic with the health plan and FHIR server mocked, and an in-memory database."""

from unittest.mock import Mock

import pytest

from priorauth import repository
from priorauth.clients import PermanentError, RetryableError
from priorauth.clients.payer import PayerDecision
from priorauth.fhir import PriorAuthSubmission
from priorauth.status import Status
from priorauth.worker.main import route
from priorauth.worker.processor import Outcome, Processor, Result

MAX = 3


@pytest.fixture
def payer():
    return Mock()


@pytest.fixture
def fhir():
    return Mock()


@pytest.fixture
def processor(session_factory, payer, fhir):
    return Processor(session_factory, payer, fhir, max_attempts=MAX)


@pytest.fixture
def make_request(session_factory, valid_body):
    def _make(**changes):
        body = {**valid_body, **changes}
        body = {k: v for k, v in body.items() if v is not None}
        with session_factory() as s:
            req = repository.create_request(s, PriorAuthSubmission.model_validate(body), "alice")
            s.commit()
            return req.id

    return _make


def load(session_factory, rid):
    with session_factory() as s:
        req = repository.get_request(s, rid, with_history=True)
        return req, [(h.from_status, h.to_status, h.event, h.attempt) for h in req.history]


def test_approved(processor, payer, make_request, session_factory):
    rid = make_request()
    payer.submit.return_value = PayerDecision("approved", "ok", "MOCK-1")
    assert processor.process(rid, 1) == Result(Outcome.DONE)
    req, hist = load(session_factory, rid)
    assert (req.status, req.authorization_number, req.attempts) == ("approved", "MOCK-1", 1)
    assert hist == [
        (None, "received", "received", None),
        ("received", "sent", "sent_to_payer", 1),
        ("sent", "approved", "decision", 1),
    ]
    bundle = payer.submit.call_args.args[0]
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == ["Patient", "Coverage", "ServiceRequest"]
    assert payer.submit.call_args.kwargs["correlation_id"] == str(rid)


def test_denied(processor, payer, make_request, session_factory):
    rid = make_request()
    payer.submit.return_value = PayerDecision("denied", "missing diagnosis", None)
    assert processor.process(rid, 1).outcome is Outcome.DONE
    req, _ = load(session_factory, rid)
    assert (req.status, req.decision_reason) == ("denied", "missing diagnosis")


def test_timeout_schedules_retry(processor, payer, make_request, session_factory):
    rid = make_request()
    payer.submit.side_effect = RetryableError("health plan timed out")
    result = processor.process(rid, 1)
    assert result == Result(Outcome.RETRY, "health plan timed out")
    req, hist = load(session_factory, rid)
    assert req.status == "sent"
    assert req.last_error == "health plan timed out"
    assert hist[-1] == ("sent", "sent", "retry_scheduled", 1)


def test_three_timeouts_end_in_dead_letter_queue(processor, payer, make_request, session_factory):
    """The interview test: a health plan that times out three times ends up in the DLQ."""
    rid = make_request()
    payer.submit.side_effect = RetryableError("health plan timed out")
    outcomes = [processor.process(rid, attempt).outcome for attempt in (1, 2, 3)]
    assert outcomes == [Outcome.RETRY, Outcome.RETRY, Outcome.DEAD]
    req, hist = load(session_factory, rid)
    assert (req.status, req.attempts) == ("failed", 3)
    assert [h[2] for h in hist] == [
        "received",
        "sent_to_payer",
        "retry_scheduled",
        "sent_to_payer",
        "retry_scheduled",
        "sent_to_payer",
        "retries_exhausted",
    ]
    assert payer.submit.call_count == 3


def test_retry_then_success(processor, payer, make_request, session_factory):
    rid = make_request()
    payer.submit.side_effect = [RetryableError("503"), PayerDecision("approved", "ok", "MOCK-2")]
    assert processor.process(rid, 1).outcome is Outcome.RETRY
    assert processor.process(rid, 2).outcome is Outcome.DONE
    req, _ = load(session_factory, rid)
    assert (req.status, req.attempts, req.last_error) == ("approved", 2, None)


def test_permanent_error_skips_retries(processor, payer, make_request, session_factory):
    rid = make_request()
    payer.submit.side_effect = PermanentError("HTTP 400")
    assert processor.process(rid, 1) == Result(Outcome.DEAD, "HTTP 400")
    req, hist = load(session_factory, rid)
    assert req.status == "failed"
    assert hist[-1][2] == "permanent_error"


def test_duplicate_message_for_decided_request_is_skipped(processor, payer, make_request):
    rid = make_request()
    payer.submit.return_value = PayerDecision("approved", "ok", "MOCK-1")
    processor.process(rid, 1)
    assert processor.process(rid, 1).outcome is Outcome.DONE
    assert payer.submit.call_count == 1  # the health plan is not called twice


def test_unknown_request_goes_to_dlq(processor):
    import uuid

    assert processor.process(uuid.uuid4(), 1).outcome is Outcome.DEAD


def test_patient_reference_resolved_on_fhir_server(processor, payer, fhir, make_request, session_factory):
    rid = make_request(patient=None, patientReference="Patient/fake-1001")
    fhir.get_patient.return_value = {"resourceType": "Patient", "id": "fake-1001", "name": [{"family": "Example"}]}
    payer.submit.return_value = PayerDecision("approved", "ok", "MOCK-3")
    assert processor.process(rid, 1).outcome is Outcome.DONE
    fhir.get_patient.assert_called_once_with("Patient/fake-1001")
    req, _ = load(session_factory, rid)
    assert req.patient["id"] == "fake-1001"  # stored, so a retry does not fetch it again
    assert payer.submit.call_args.args[0]["entry"][0]["resource"]["id"] == "fake-1001"


def test_patient_not_found_fails_permanently(processor, payer, fhir, make_request, session_factory):
    rid = make_request(patient=None, patientReference="Patient/missing")
    fhir.get_patient.side_effect = PermanentError("Patient/missing not found on the FHIR server")
    assert processor.process(rid, 1).outcome is Outcome.DEAD
    payer.submit.assert_not_called()
    assert load(session_factory, rid)[0].status == Status.FAILED


@pytest.mark.parametrize(
    ("result", "attempt", "expected"),
    [
        (Result(Outcome.DONE), 1, (None, 1)),
        (Result(Outcome.RETRY, "x"), 1, ("retry", 2)),
        (Result(Outcome.RETRY, "x"), 2, ("retry", 3)),
        (Result(Outcome.DEAD, "x"), 3, ("dead", 3)),
    ],
)
def test_routing(result, attempt, expected):
    assert route(result, attempt) == expected
