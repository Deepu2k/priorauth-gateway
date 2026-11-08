"""FastAPI app factory. Run with: uvicorn priorauth.api.main:app"""

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from priorauth import __version__, metrics, telemetry
from priorauth.api.routes import router
from priorauth.config import get_settings
from priorauth.db import get_engine
from priorauth.log import configure_logging, get_logger
from priorauth.queue import Publisher, RabbitPublisher

log = get_logger(__name__)

DESCRIPTION = """
Submit a FHIR-shaped prior authorization request, then poll its status. The API saves the request
and queues it; a worker sends it to the (mock) health plan, retries failures and moves requests
that keep failing to a dead letter queue.

**All data is fake.** Never send real patient information.

Authenticate with **Authorize**: either a Bearer token from `POST /auth/token`, or a partner
`X-API-Key`.
"""


def operation_outcome(exc: RequestValidationError) -> dict:
    """Validation errors as a FHIR OperationOutcome, the format FHIR clients already parse."""
    issues = []
    for err in exc.errors():
        loc = [str(p) for p in err["loc"] if p != "body"]
        issues.append(
            {
                "severity": "error",
                "code": "required" if err["type"] == "missing" else "invalid",
                "expression": [".".join(loc)] if loc else [],
                "diagnostics": err["msg"].removeprefix("Value error, "),
            }
        )
    return {"resourceType": "OperationOutcome", "issue": issues}


def create_app(publisher: Publisher | None = None, *, instrument: bool = True) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.service_name, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.publisher is None:
            app.state.publisher = RabbitPublisher(settings.amqp_url, settings.retry_delay_ms)
        log.info("api_started", version=__version__)
        yield
        close = getattr(app.state.publisher, "close", None)
        if close:
            close()

    app = FastAPI(title="PriorAuth Gateway", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    app.state.publisher = publisher
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        metrics.REQUESTS_REJECTED.labels(reason="validation").inc()
        outcome = operation_outcome(exc)
        log.info("request_rejected", issues=len(outcome["issue"]))
        return JSONResponse(outcome, status_code=422, media_type="application/fhir+json")

    @app.middleware("http")
    async def observe(request: Request, call_next):
        correlation_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route != "/metrics":
            metrics.HTTP_REQUESTS.labels(request.method, route, response.status_code).inc()
            metrics.HTTP_LATENCY.labels(request.method, route).observe(elapsed)
            if not route.startswith("/health"):
                log.info(
                    "http_request",
                    method=request.method,
                    route=route,
                    status=response.status_code,
                    duration_ms=round(elapsed * 1000, 1),
                )
        response.headers["X-Request-ID"] = correlation_id
        return response

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if instrument:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        telemetry.configure_tracing(settings.service_name)
        telemetry.instrument_sqlalchemy(get_engine())
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/.*,metrics")
    return app


def __getattr__(name: str):
    # `uvicorn priorauth.api.main:app` builds the app lazily, so importing this module in tests
    # does not need a database or broker.
    if name == "app":
        return create_app()
    raise AttributeError(name)
