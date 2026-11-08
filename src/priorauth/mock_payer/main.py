"""A mock health plan. Fake rules, fake data, and switches for breaking it on purpose.

Run with: uvicorn priorauth.mock_payer.main:app --port 8081

Decision rules (all invented):
  * CPT 15823 (cosmetic eyelid surgery)          -> denied, not medically necessary
  * advanced imaging (70551, 72148, 73721) with
    no ICD-10 diagnosis in reasonCode             -> denied, missing diagnosis
  * anything else                                 -> approved

Failure switches, per request (by Coverage.subscriberId prefix):
  TIMEOUT-...  sleeps longer than the gateway's timeout
  ERROR-...    answers 503
  FLAKY-...    answers 503 twice, then decides normally (shows a retry that recovers)
  BADREQ-...   answers 400 (a permanent error: no retry)

Or for every request: POST /admin/failure-mode {"mode": "timeout" | "error" | "none"}.
Also serves a few fake FHIR Patients at /fhir/Patient/{id}, for running without internet.
"""

import asyncio
import os
import secrets
from collections import defaultdict
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from priorauth.log import configure_logging, get_logger

CPT = "http://www.ama-assn.org/go/cpt"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
DENY_ALWAYS = {"15823": "not medically necessary (mock rule: cosmetic procedure)"}
NEEDS_DIAGNOSIS = {"70551", "72148", "73721"}
TIMEOUT_SLEEP_SECONDS = float(os.getenv("MOCK_TIMEOUT_SLEEP_SECONDS", "10"))

FAKE_PATIENTS = {
    "fake-1001": {
        "resourceType": "Patient",
        "id": "fake-1001",
        "identifier": [{"system": "urn:fake:mrn", "value": "FAKE-MRN-1001"}],
        "name": [{"family": "Example", "given": ["Alex"]}],
        "gender": "other",
        "birthDate": "1975-09-30",
    },
    "fake-1002": {
        "resourceType": "Patient",
        "id": "fake-1002",
        "name": [{"family": "Sample", "given": ["Robin"]}],
        "gender": "female",
        "birthDate": "1990-02-14",
    },
}

configure_logging("mock-payer", os.getenv("LOG_LEVEL", "INFO"))
log = get_logger(__name__)
app = FastAPI(title="Mock Health Plan", description="Fake prior auth decisions. Fake data only.")

_state = {"mode": "none"}
_flaky_calls: dict[str, int] = defaultdict(int)


class FailureMode(BaseModel):
    mode: Literal["none", "timeout", "error"]


def _resources(bundle: dict) -> dict[str, dict]:
    return {e["resource"]["resourceType"]: e["resource"] for e in bundle.get("entry", []) if "resource" in e}


def decide(service_request: dict) -> tuple[str, str]:
    codings = service_request.get("code", {}).get("coding", [])
    cpt = next((c["code"] for c in codings if c.get("system") == CPT), None)
    diagnoses = [
        c for rc in service_request.get("reasonCode", []) for c in rc.get("coding", []) if c.get("system") == ICD10
    ]
    if cpt in DENY_ALWAYS:
        return "denied", DENY_ALWAYS[cpt]
    if cpt in NEEDS_DIAGNOSIS and not diagnoses:
        return "denied", "missing diagnosis: advanced imaging needs an ICD-10 code in reasonCode"
    return "approved", "meets mock medical necessity criteria"


@app.post("/prior-auth")
async def prior_auth(bundle: dict, x_correlation_id: str = Header(default="none")):
    res = _resources(bundle)
    if not {"Patient", "Coverage", "ServiceRequest"} <= res.keys():
        raise HTTPException(400, "bundle needs Patient, Coverage and ServiceRequest")
    sub = res["Coverage"].get("subscriberId", "")
    mode = _state["mode"]

    if mode == "timeout" or sub.startswith("TIMEOUT-"):
        log.warning("simulating_timeout", correlation_id=x_correlation_id)
        await asyncio.sleep(TIMEOUT_SLEEP_SECONDS)
    if mode == "error" or sub.startswith("ERROR-"):
        log.warning("simulating_error", correlation_id=x_correlation_id)
        raise HTTPException(503, "mock health plan is unavailable")
    if sub.startswith("BADREQ-"):
        raise HTTPException(400, "mock health plan rejects this subscriber")
    if sub.startswith("FLAKY-"):
        _flaky_calls[x_correlation_id] += 1
        if _flaky_calls[x_correlation_id] <= 2:
            log.warning("simulating_flaky_error", correlation_id=x_correlation_id, call=_flaky_calls[x_correlation_id])
            raise HTTPException(503, "mock health plan hiccup")

    decision, reason = decide(res["ServiceRequest"])
    auth_no = f"MOCK-{secrets.token_hex(4).upper()}" if decision == "approved" else None
    log.info("decision", correlation_id=x_correlation_id, decision=decision)
    return {"decision": decision, "reason": reason, "authorizationNumber": auth_no}


@app.post("/admin/failure-mode")
def set_failure_mode(body: FailureMode):
    _state["mode"] = body.mode
    log.warning("failure_mode_changed", mode=body.mode)
    return _state


@app.get("/fhir/Patient/{patient_id}")
def fhir_patient(patient_id: str):
    if patient_id not in FAKE_PATIENTS:
        raise HTTPException(404, f"Patient/{patient_id} not found")
    return FAKE_PATIENTS[patient_id]


@app.get("/health")
def health():
    return {"status": "ok", "mode": _state["mode"]}
