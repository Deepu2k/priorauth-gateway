import pytest
from pydantic import ValidationError

from priorauth.fhir import PriorAuthSubmission


def test_valid_submission_parses(valid_body):
    sub = PriorAuthSubmission.model_validate(valid_body)
    assert sub.serviceRequest.procedure_code() == "72148"
    assert sub.serviceRequest.diagnosis_codes() == ["M54.50"]


def test_patient_reference_instead_of_inline_patient(valid_body):
    valid_body.pop("patient")
    valid_body["patientReference"] = "Patient/fake-1001"
    assert PriorAuthSubmission.model_validate(valid_body).patientReference == "Patient/fake-1001"


def test_both_patient_forms_rejected(valid_body):
    valid_body["patientReference"] = "Patient/1"
    with pytest.raises(ValidationError, match="exactly one of patient or patientReference"):
        PriorAuthSubmission.model_validate(valid_body)


def test_neither_patient_form_rejected(valid_body):
    valid_body.pop("patient")
    with pytest.raises(ValidationError, match="exactly one"):
        PriorAuthSubmission.model_validate(valid_body)


@pytest.mark.parametrize("ref", ["Patient/", "Observation/1", "Patient/../etc", "patient/1"])
def test_bad_patient_reference(valid_body, ref):
    valid_body.pop("patient")
    valid_body["patientReference"] = ref
    with pytest.raises(ValidationError):
        PriorAuthSubmission.model_validate(valid_body)


def test_service_request_needs_procedure_code(valid_body):
    valid_body["serviceRequest"]["code"] = {"coding": [{"system": "http://snomed.info/sct", "code": "123"}]}
    with pytest.raises(ValidationError, match="procedure system"):
        PriorAuthSubmission.model_validate(valid_body)


def test_inactive_coverage_rejected(valid_body):
    valid_body["coverage"]["status"] = "cancelled"
    with pytest.raises(ValidationError, match="coverage must be active"):
        PriorAuthSubmission.model_validate(valid_body)


def test_future_birth_date_rejected(valid_body):
    valid_body["patient"]["birthDate"] = "2999-01-01"
    with pytest.raises(ValidationError, match="future"):
        PriorAuthSubmission.model_validate(valid_body)


def test_wrong_resource_type_rejected(valid_body):
    valid_body["coverage"]["resourceType"] = "Claim"
    with pytest.raises(ValidationError):
        PriorAuthSubmission.model_validate(valid_body)


def test_unknown_fhir_fields_are_kept(valid_body):
    valid_body["patient"]["meta"] = {"versionId": "3"}
    sub = PriorAuthSubmission.model_validate(valid_body)
    assert sub.patient.model_dump()["meta"] == {"versionId": "3"}
