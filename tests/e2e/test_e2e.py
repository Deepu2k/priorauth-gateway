"""End-to-end tests against a running stack, through the public endpoints only.

    docker compose up -d --build
    E2E_BASE_URL=http://localhost:8080 pytest -m e2e

Credentials default to the dev values in .env.example.
"""

import copy
import os
import time

import httpx
import pytest

from tests.conftest import VALID

pytestmark = pytest.mark.e2e

BASE = os.getenv("E2E_BASE_URL", "http://localhost:8080")
USER = (os.getenv("E2E_USER", "clinician"), os.getenv("E2E_PASSWORD", "clinician-dev-pw"))
ADMIN = (os.getenv("E2E_ADMIN_USER", "admin"), os.getenv("E2E_ADMIN_PASSWORD", "admin-dev-pw"))
API_KEY = os.getenv("E2E_API_KEY", "dev-partner-key-change-me")
TIMEOUT = float(os.getenv("E2E_TIMEOUT_SECONDS", "60"))


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE, timeout=10) as c:
        for _ in range(60):  # wait for the stack
            try:
                if c.get("/health/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        yield c


def login(http, user, pw) -> dict:
    r = http.post("/auth/token", data={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def auth(http):
    return login(http, *USER)


def body(**coverage):
    b = copy.deepcopy(VALID)
    b["coverage"].update(coverage)
    return b


def run(http, headers, payload) -> dict:
    r = http.post("/requests", json=payload, headers=headers)
    assert r.status_code == 202, r.text
    url = r.headers["Location"]
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        data = http.get(url, headers=headers).json()
        if data["status"] in ("approved", "denied", "failed"):
            return data
        time.sleep(0.5)
    raise AssertionError(f"{url} did not finish within {TIMEOUT}s: {data}")


def test_approved(http, auth):
    assert run(http, auth, body())["status"] == "approved"


def test_denied(http, auth):
    payload = body()
    payload["serviceRequest"].pop("reasonCode")
    assert run(http, auth, payload)["status"] == "denied"


def test_partner_api_key(http):
    data = run(http, {"X-API-Key": API_KEY}, body())
    assert (data["status"], data["submittedBy"]) == ("approved", "partner-ehr")


def test_timeout_ends_in_dlq(http, auth):
    data = run(http, auth, body(subscriberId="TIMEOUT-E2E-1"))
    assert (data["status"], data["attempts"]) == ("failed", 3)
    dlq = http.get("/admin/dlq", params={"limit": 200}, headers=login(http, *ADMIN)).json()
    assert data["id"] in {m["requestId"] for m in dlq["messages"]}


def test_validation_error_is_operation_outcome(http, auth):
    r = http.post("/requests", json={"coverage": {}}, headers=auth)
    assert r.status_code == 422
    assert r.json()["resourceType"] == "OperationOutcome"


def test_unauthenticated_is_rejected(http):
    assert http.post("/requests", json=body()).status_code == 401
