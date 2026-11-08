import copy
import hashlib
import os

import pytest


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# Test-only settings, set before anything reads them.
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-0123")
os.environ["DEMO_USERS"] = ",".join(
    [
        f"alice:{sha('alice-pw')}:submitter",
        f"carol:{sha('carol-pw')}:submitter",
        f"vic:{sha('vic-pw')}:viewer",
        f"root:{sha('root-pw')}:admin",
    ]
)
os.environ["API_KEYS"] = f"partner-ehr:{sha('pk-test-123')}:submitter"
os.environ.setdefault("LOG_LEVEL", "WARNING")

VALID = {
    "patient": {
        "resourceType": "Patient",
        "identifier": [{"system": "urn:fake:mrn", "value": "FAKE-MRN-0001"}],
        "name": [{"family": "Testpatient", "given": ["Jane"]}],
        "gender": "female",
        "birthDate": "1980-04-12",
    },
    "coverage": {
        "resourceType": "Coverage",
        "status": "active",
        "subscriberId": "FAKE-SUB-1001",
        "payor": [{"display": "Mock Health Plan"}],
    },
    "serviceRequest": {
        "resourceType": "ServiceRequest",
        "status": "active",
        "intent": "order",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "72148"}]},
        "reasonCode": [{"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "M54.50"}]}],
    },
}


@pytest.fixture
def valid_body():
    return copy.deepcopy(VALID)
