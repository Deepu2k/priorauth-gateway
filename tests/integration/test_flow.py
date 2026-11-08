"""The whole flow: API -> Postgres -> RabbitMQ -> worker -> mock health plan -> Postgres."""

import json
import uuid

import pika
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from priorauth import db
from priorauth.queue import DLQ, EXCHANGE, RK_REQUEST
from tests.integration.conftest import wait_for

pytestmark = pytest.mark.integration

TERMINAL = {"approved", "denied", "failed"}


@pytest.fixture
def alice(token):
    return token("alice", "alice-pw")


@pytest.fixture
def root(token):
    return token("root", "root-pw")


def submit(api, headers, body) -> str:
    r = api.post("/requests", json=body, headers=headers)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def wait_terminal(api, headers, rid, timeout=15.0) -> dict:
    def done():
        data = api.get(f"/requests/{rid}", headers=headers).json()
        return data if data["status"] in TERMINAL else None

    return wait_for(done, timeout=timeout)


def events(api, headers, rid) -> list[str]:
    return [h["event"] for h in api.get(f"/requests/{rid}/history", headers=headers).json()]


def test_submitted_request_is_approved(api, alice, valid_body):
    rid = submit(api, alice, valid_body)
    final = wait_terminal(api, alice, rid)
    assert final["status"] == "approved"
    assert final["authorizationNumber"].startswith("MOCK-")
    assert events(api, alice, rid) == ["received", "sent_to_payer", "decision"]


def test_imaging_without_diagnosis_is_denied(api, alice, valid_body):
    valid_body["serviceRequest"].pop("reasonCode")
    final = wait_terminal(api, alice, submit(api, alice, valid_body))
    assert final["status"] == "denied"
    assert "missing diagnosis" in final["decisionReason"]


def test_health_plan_timing_out_three_times_ends_in_dlq(api, alice, root, valid_body):
    """The failure test: three timeouts, two delayed retries, then the dead letter queue."""
    valid_body["coverage"]["subscriberId"] = "TIMEOUT-SUB-1"
    rid = submit(api, alice, valid_body)
    final = wait_terminal(api, alice, rid, timeout=20)
    assert (final["status"], final["attempts"]) == ("failed", 3)
    assert "timed out" in final["lastError"]
    assert events(api, alice, rid) == [
        "received",
        "sent_to_payer",
        "retry_scheduled",
        "sent_to_payer",
        "retry_scheduled",
        "sent_to_payer",
        "retries_exhausted",
    ]
    dlq = wait_for(lambda: (d := api.get("/admin/dlq", headers=root).json()) and d["depth"] >= 1 and d)
    msg = next(m for m in dlq["messages"] if m["requestId"] == rid)
    assert msg["attempts"] == 3
    assert "timed out" in msg["lastError"]
    # Peeking did not consume it.
    assert api.get("/admin/dlq", headers=root).json()["depth"] == dlq["depth"]


def test_flaky_health_plan_recovers_on_retry(api, alice, valid_body):
    valid_body["coverage"]["subscriberId"] = "FLAKY-SUB-1"
    final = wait_terminal(api, alice, submit(api, alice, valid_body))
    assert (final["status"], final["attempts"]) == ("approved", 3)


def test_permanent_error_is_not_retried(api, alice, valid_body):
    valid_body["coverage"]["subscriberId"] = "BADREQ-SUB-1"
    rid = submit(api, alice, valid_body)
    final = wait_terminal(api, alice, rid)
    assert (final["status"], final["attempts"]) == ("failed", 1)
    assert events(api, alice, rid)[-1] == "permanent_error"


def test_patient_reference_resolved_from_fhir_server(api, alice, valid_body):
    valid_body.pop("patient")
    valid_body["patientReference"] = "Patient/fake-1001"
    rid = submit(api, alice, valid_body)
    assert wait_terminal(api, alice, rid)["status"] == "approved"
    with db.get_engine().connect() as conn:
        stored = conn.execute(text("SELECT patient FROM prior_auth_requests WHERE id = :id"), {"id": rid}).scalar_one()
    assert stored["id"] == "fake-1001"


def test_unknown_fhir_patient_fails(api, alice, valid_body):
    valid_body.pop("patient")
    valid_body["patientReference"] = "Patient/does-not-exist"
    final = wait_terminal(api, alice, submit(api, alice, valid_body))
    assert final["status"] == "failed"
    assert "not found" in final["lastError"]


def _publish_raw(settings, body: bytes, headers=None):
    conn = pika.BlockingConnection(pika.URLParameters(settings.amqp_url))
    conn.channel().basic_publish(EXCHANGE, RK_REQUEST, body, pika.BasicProperties(headers=headers or {}))
    conn.close()


def _dlq_depth(settings) -> int:
    conn = pika.BlockingConnection(pika.URLParameters(settings.amqp_url))
    depth = conn.channel().queue_declare(DLQ, passive=True).method.message_count
    conn.close()
    return depth


def test_duplicate_message_does_not_reprocess(api, alice, settings, valid_body):
    rid = submit(api, alice, valid_body)
    wait_terminal(api, alice, rid)
    before = events(api, alice, rid)
    _publish_raw(settings, json.dumps({"request_id": rid}).encode())
    wait_for(lambda: _queue_empty(settings), timeout=5)
    assert events(api, alice, rid) == before


def _queue_empty(settings) -> bool:
    from priorauth.queue import QUEUE

    conn = pika.BlockingConnection(pika.URLParameters(settings.amqp_url))
    q = conn.channel().queue_declare(QUEUE, passive=True).method
    conn.close()
    return q.message_count == 0 and q.consumer_count >= 1


def test_poison_message_goes_to_dlq(settings):
    before = _dlq_depth(settings)
    _publish_raw(settings, b"this is not json")
    wait_for(lambda: _dlq_depth(settings) == before + 1)


def test_unknown_request_id_goes_to_dlq(settings):
    before = _dlq_depth(settings)
    _publish_raw(settings, json.dumps({"request_id": str(uuid.uuid4())}).encode())
    wait_for(lambda: _dlq_depth(settings) == before + 1)


def test_database_rejects_invalid_status(settings):
    with pytest.raises(IntegrityError), db.get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO prior_auth_requests (id, status, patient, coverage, service_request, service_code,"
                " submitted_by) VALUES (:id, 'lost', '{}', '{}', '{}', 'X', 'test')"
            ),
            {"id": str(uuid.uuid4())},
        )
