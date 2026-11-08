"""OpenTelemetry tracing. The trace context rides in AMQP message headers, so one trace covers
API -> queue -> worker -> health plan. Export is on only when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

import os

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def configure_tracing(service: str) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def instrument_httpx() -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def instrument_sqlalchemy(engine) -> None:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine)


def inject_headers() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(headers: dict | None):
    return propagate.extract({k: str(v) for k, v in (headers or {}).items()})


tracer = trace.get_tracer("priorauth")
