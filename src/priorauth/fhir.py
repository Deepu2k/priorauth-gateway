"""FHIR R4-shaped resources: the small subset this gateway needs.

These follow the FHIR JSON field names (camelCase, `resourceType`) so a real FHIR Patient from a
server such as hapi.fhir.org validates as-is. Unknown fields are kept, as FHIR clients expect.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"
HCPCS_SYSTEM = "https://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
PROCEDURE_SYSTEMS = {CPT_SYSTEM, HCPCS_SYSTEM}


class FhirModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Coding(FhirModel):
    system: str | None = None
    code: str = Field(min_length=1, max_length=32)
    display: str | None = None


class CodeableConcept(FhirModel):
    coding: list[Coding] = Field(default_factory=list)
    text: str | None = None


class Identifier(FhirModel):
    system: str | None = None
    value: str = Field(min_length=1, max_length=128)


class HumanName(FhirModel):
    family: str | None = None
    given: list[str] = Field(default_factory=list)
    text: str | None = None


class Reference(FhirModel):
    reference: str | None = None
    display: str | None = None


class Patient(FhirModel):
    resourceType: Literal["Patient"]
    id: str | None = None
    identifier: list[Identifier] = Field(default_factory=list)
    name: list[HumanName] = Field(min_length=1)
    gender: Literal["male", "female", "other", "unknown"] | None = None
    birthDate: date | None = None

    @field_validator("birthDate")
    @classmethod
    def not_in_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise ValueError("birthDate cannot be in the future")
        return v


class Coverage(FhirModel):
    resourceType: Literal["Coverage"]
    status: Literal["active", "cancelled", "draft", "entered-in-error"]
    subscriberId: str = Field(min_length=1, max_length=64)
    payor: list[Reference] = Field(min_length=1)

    @field_validator("status")
    @classmethod
    def must_be_active(cls, v: str) -> str:
        if v != "active":
            raise ValueError("coverage must be active to request prior authorization")
        return v


class ServiceRequest(FhirModel):
    resourceType: Literal["ServiceRequest"]
    status: Literal["draft", "active", "on-hold", "revoked", "completed", "entered-in-error", "unknown"]
    intent: Literal["proposal", "plan", "directive", "order", "original-order", "reflex-order"]
    priority: Literal["routine", "urgent", "asap", "stat"] | None = None
    code: CodeableConcept
    reasonCode: list[CodeableConcept] = Field(default_factory=list)
    occurrenceDateTime: datetime | None = None

    @field_validator("code")
    @classmethod
    def needs_procedure_code(cls, v: CodeableConcept) -> CodeableConcept:
        if not any(c.system in PROCEDURE_SYSTEMS for c in v.coding):
            raise ValueError(f"code needs at least one coding from a procedure system: {sorted(PROCEDURE_SYSTEMS)}")
        return v

    def procedure_code(self) -> str:
        return next(c.code for c in self.code.coding if c.system in PROCEDURE_SYSTEMS)

    def diagnosis_codes(self) -> list[str]:
        return [c.code for cc in self.reasonCode for c in cc.coding if c.system == ICD10_SYSTEM]


class PriorAuthSubmission(BaseModel):
    """POST /requests body. Give either an inline `patient` or a `patientReference` to a FHIR server."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
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
                    "priority": "routine",
                    "code": {
                        "coding": [{"system": CPT_SYSTEM, "code": "72148", "display": "MRI lumbar spine w/o contrast"}]
                    },
                    "reasonCode": [{"coding": [{"system": ICD10_SYSTEM, "code": "M54.50"}]}],
                },
            }
        }
    )

    patient: Patient | None = None
    patientReference: str | None = Field(default=None, pattern=r"^Patient/[A-Za-z0-9\-][A-Za-z0-9\-.]{0,63}$")
    coverage: Coverage
    serviceRequest: ServiceRequest

    @model_validator(mode="after")
    def exactly_one_patient(self) -> "PriorAuthSubmission":
        if (self.patient is None) == (self.patientReference is None):
            raise ValueError("give exactly one of patient or patientReference")
        return self
