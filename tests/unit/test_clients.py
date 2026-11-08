import httpx
import pytest

from priorauth.clients import PermanentError, RetryableError
from priorauth.clients.fhir_server import FhirClient
from priorauth.clients.payer import PayerClient

BUNDLE = {"resourceType": "Bundle", "type": "collection", "entry": []}


def payer_with(handler) -> PayerClient:
    return PayerClient(
        "http://payer", 1, client=httpx.Client(base_url="http://payer", transport=httpx.MockTransport(handler))
    )


def fhir_with(handler) -> FhirClient:
    return FhirClient(
        "http://fhir", 1, client=httpx.Client(base_url="http://fhir", transport=httpx.MockTransport(handler))
    )


def test_payer_decision_parsed():
    def handler(req: httpx.Request):
        assert req.url.path == "/prior-auth"
        assert req.headers["X-Correlation-ID"] == "rid-1"
        return httpx.Response(200, json={"decision": "approved", "reason": "ok", "authorizationNumber": "MOCK-9"})

    d = payer_with(handler).submit(BUNDLE, correlation_id="rid-1")
    assert (d.decision, d.authorization_number) == ("approved", "MOCK-9")


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_payer_server_errors_are_retryable(code):
    with pytest.raises(RetryableError, match=str(code)):
        payer_with(lambda r: httpx.Response(code)).submit(BUNDLE, correlation_id="x")


@pytest.mark.parametrize("code", [400, 404, 422])
def test_payer_client_errors_are_permanent(code):
    with pytest.raises(PermanentError):
        payer_with(lambda r: httpx.Response(code, text="bad")).submit(BUNDLE, correlation_id="x")


def test_payer_timeout_is_retryable():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(RetryableError, match="timed out"):
        payer_with(handler).submit(BUNDLE, correlation_id="x")


def test_payer_connection_refused_is_retryable():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(RetryableError, match="unreachable"):
        payer_with(handler).submit(BUNDLE, correlation_id="x")


def test_payer_unknown_decision_is_permanent():
    with pytest.raises(PermanentError, match="unknown decision"):
        payer_with(lambda r: httpx.Response(200, json={"decision": "maybe"})).submit(BUNDLE, correlation_id="x")


PATIENT = {"resourceType": "Patient", "id": "p1", "name": [{"family": "Doe"}], "birthDate": "1970-01-01"}


def test_fhir_patient_fetched():
    def handler(req):
        assert req.url.path == "/Patient/p1"
        return httpx.Response(200, json=PATIENT)

    assert fhir_with(handler).get_patient("Patient/p1")["id"] == "p1"


def test_fhir_retries_blips_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json=PATIENT)

    assert fhir_with(handler).get_patient("Patient/p1")["id"] == "p1"
    assert len(calls) == 3


def test_fhir_gives_up_after_three_tries():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(RetryableError):
        fhir_with(handler).get_patient("Patient/p1")
    assert len(calls) == 3


def test_fhir_404_is_permanent_and_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404)

    with pytest.raises(PermanentError, match="not found"):
        fhir_with(handler).get_patient("Patient/zzz")
    assert len(calls) == 1


def test_fhir_non_patient_is_permanent():
    with pytest.raises(PermanentError, match="not a usable Patient"):
        fhir_with(lambda r: httpx.Response(200, json={"resourceType": "Observation"})).get_patient("Patient/p1")
