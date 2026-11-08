"""Client for the health plan's prior auth endpoint (the mock payer in this project)."""

import time
from dataclasses import dataclass

import httpx

from priorauth import metrics
from priorauth.clients import PermanentError, RetryableError


@dataclass(frozen=True)
class PayerDecision:
    decision: str  # "approved" | "denied"
    reason: str | None
    authorization_number: str | None


def build_bundle(patient: dict, coverage: dict, service_request: dict) -> dict:
    """The three resources travel to the health plan as one FHIR collection Bundle."""
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": patient}, {"resource": coverage}, {"resource": service_request}],
    }


def classify(response: httpx.Response) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise RetryableError(f"health plan returned HTTP {response.status_code}")
    if response.status_code >= 400:
        raise PermanentError(f"health plan rejected the request: HTTP {response.status_code} {response.text[:200]}")


class PayerClient:
    def __init__(self, base_url: str, timeout_seconds: float, client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_seconds)

    def submit(self, bundle: dict, *, correlation_id: str) -> PayerDecision:
        start = time.perf_counter()
        outcome = "error"
        try:
            try:
                resp = self._client.post("/prior-auth", json=bundle, headers={"X-Correlation-ID": correlation_id})
            except httpx.TimeoutException as exc:
                outcome = "timeout"
                raise RetryableError(f"health plan timed out ({type(exc).__name__})") from exc
            except httpx.TransportError as exc:
                outcome = "unreachable"
                raise RetryableError(f"health plan unreachable ({type(exc).__name__})") from exc
            classify(resp)
            data = resp.json()
            decision = data.get("decision")
            if decision not in ("approved", "denied"):
                raise PermanentError(f"health plan sent an unknown decision: {decision!r}")
            outcome = decision
            return PayerDecision(decision, data.get("reason"), data.get("authorizationNumber"))
        finally:
            elapsed = time.perf_counter() - start
            metrics.PAYER_LATENCY.labels(outcome).observe(elapsed)
            metrics.ATTEMPTS.labels(outcome).inc()

    def close(self) -> None:
        self._client.close()
