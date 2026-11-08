import copy

import pytest
from fastapi.testclient import TestClient

from priorauth.clients.payer import build_bundle
from priorauth.mock_payer import main as mock
from tests.conftest import VALID


@pytest.fixture
def payer():
    mock._state["mode"] = "none"
    mock._flaky_calls.clear()
    return TestClient(mock.app)


def bundle(sub="FAKE-1", cpt="72148", diagnosis=True):
    sr = copy.deepcopy(VALID["serviceRequest"])
    sr["code"]["coding"][0]["code"] = cpt
    if not diagnosis:
        sr.pop("reasonCode")
    cov = {**VALID["coverage"], "subscriberId": sub}
    return build_bundle(VALID["patient"], cov, sr)


def test_approves_with_diagnosis(payer):
    r = payer.post("/prior-auth", json=bundle())
    assert r.json()["decision"] == "approved"
    assert r.json()["authorizationNumber"].startswith("MOCK-")


def test_denies_imaging_without_diagnosis(payer):
    r = payer.post("/prior-auth", json=bundle(diagnosis=False)).json()
    assert r["decision"] == "denied"
    assert "missing diagnosis" in r["reason"]
    assert r["authorizationNumber"] is None


def test_denies_cosmetic(payer):
    assert payer.post("/prior-auth", json=bundle(cpt="15823")).json()["decision"] == "denied"


def test_error_prefix(payer):
    assert payer.post("/prior-auth", json=bundle(sub="ERROR-1")).status_code == 503


def test_badreq_prefix(payer):
    assert payer.post("/prior-auth", json=bundle(sub="BADREQ-1")).status_code == 400


def test_flaky_recovers_on_third_call(payer):
    h = {"X-Correlation-ID": "r1"}
    codes = [payer.post("/prior-auth", json=bundle(sub="FLAKY-1"), headers=h).status_code for _ in range(3)]
    assert codes == [503, 503, 200]


def test_global_failure_mode(payer):
    payer.post("/admin/failure-mode", json={"mode": "error"})
    assert payer.post("/prior-auth", json=bundle()).status_code == 503
    payer.post("/admin/failure-mode", json={"mode": "none"})
    assert payer.post("/prior-auth", json=bundle()).status_code == 200


def test_incomplete_bundle_is_400(payer):
    assert payer.post("/prior-auth", json={"resourceType": "Bundle", "entry": []}).status_code == 400


def test_fake_fhir_patient(payer):
    assert payer.get("/fhir/Patient/fake-1001").json()["resourceType"] == "Patient"
    assert payer.get("/fhir/Patient/nope").status_code == 404
