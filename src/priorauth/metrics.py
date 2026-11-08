"""Prometheus metrics. Queue depth comes from RabbitMQ's own Prometheus plugin (port 15692)."""

from prometheus_client import Counter, Histogram

REQUESTS_RECEIVED = Counter("priorauth_requests_received_total", "Prior auth requests accepted by the API")
REQUESTS_REJECTED = Counter("priorauth_requests_rejected_total", "Submissions rejected before queueing", ["reason"])
REQUESTS_FAILED = Counter("priorauth_requests_failed_total", "Requests that ended in the failed state", ["reason"])
DECISIONS = Counter("priorauth_decisions_total", "Health plan decisions", ["decision"])
ATTEMPTS = Counter("priorauth_payer_attempts_total", "Calls to the health plan", ["outcome"])
RETRIES = Counter("priorauth_retries_scheduled_total", "Attempts that failed and were scheduled again")
DEAD_LETTERED = Counter("priorauth_dead_lettered_total", "Messages sent to the dead letter queue", ["reason"])
PAYER_LATENCY = Histogram(
    "priorauth_payer_call_seconds",
    "Health plan call latency",
    ["outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
HTTP_REQUESTS = Counter("priorauth_http_requests_total", "HTTP requests to the API", ["method", "route", "status"])
HTTP_LATENCY = Histogram("priorauth_http_request_seconds", "API latency", ["method", "route"])
