"""Reads Patient resources from a FHIR R4 server, by default the public HAPI test server.

Short in-call retries (tenacity) absorb a blip; if the server stays down the worker's queue-level
retry takes over, so a slow FHIR server never blocks the worker for long.
"""

import httpx
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from priorauth.clients import PermanentError, RetryableError
from priorauth.fhir import Patient


class FhirClient:
    def __init__(self, base_url: str, timeout_seconds: float, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, headers={"Accept": "application/fhir+json"}
        )

    @retry(
        retry=retry_if_exception_type(RetryableError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, max=2),
        reraise=True,
    )
    def get_patient(self, reference: str) -> dict:
        """`reference` looks like "Patient/123"."""
        try:
            resp = self._client.get(f"/{reference}")
        except httpx.TransportError as exc:
            raise RetryableError(f"FHIR server unreachable ({type(exc).__name__})") from exc
        if resp.status_code in (404, 410):
            raise PermanentError(f"{reference} not found on the FHIR server")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableError(f"FHIR server returned HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"FHIR server returned HTTP {resp.status_code}")
        try:
            patient = Patient.model_validate(resp.json())
        except (ValidationError, ValueError) as exc:
            raise PermanentError(f"{reference} from the FHIR server is not a usable Patient: {exc}") from exc
        return patient.model_dump(mode="json", exclude_none=True)

    def close(self) -> None:
        self._client.close()
