"""The queue consumer. Run with: python -m priorauth.worker.main"""

import json
import signal
import time
import uuid

import pika
import structlog
from opentelemetry import trace
from pika.exceptions import AMQPConnectionError, AMQPError
from prometheus_client import start_http_server

from priorauth import metrics, telemetry
from priorauth.clients.fhir_server import FhirClient
from priorauth.clients.payer import PayerClient
from priorauth.config import Settings, get_settings
from priorauth.db import get_engine, get_session_factory
from priorauth.log import configure_logging, get_logger
from priorauth.queue import (
    ATTEMPT_HEADER,
    EXCHANGE,
    LAST_ERROR_HEADER,
    QUEUE,
    RK_DEAD,
    RK_RETRY,
    build_properties,
    declare_topology,
    encode,
)
from priorauth.worker.processor import Outcome, Processor, Result

log = get_logger(__name__)


def route(result: Result, attempt: int) -> tuple[str | None, int]:
    """Where the message goes next: (routing key or None to just ack, attempt number to stamp)."""
    if result.outcome is Outcome.RETRY:
        return RK_RETRY, attempt + 1
    if result.outcome is Outcome.DEAD:
        return RK_DEAD, attempt
    return None, attempt


class Consumer:
    def __init__(self, settings: Settings, processor: Processor):
        self.settings = settings
        self.processor = processor
        self._channel = None
        self._stopping = False

    def on_message(self, ch, method, props, body: bytes) -> None:
        headers = props.headers or {}
        attempt = int(headers.get(ATTEMPT_HEADER, 1))
        structlog.contextvars.clear_contextvars()
        try:
            request_id = uuid.UUID(json.loads(body)["request_id"])
        except (ValueError, KeyError, TypeError):
            log.error("unparseable_message", body=body[:200].decode(errors="replace"))
            metrics.DEAD_LETTERED.labels("unparseable").inc()
            ch.basic_reject(method.delivery_tag, requeue=False)  # the work queue dead-letters it to the DLQ
            return

        structlog.contextvars.bind_contextvars(request_id=str(request_id), attempt=attempt)
        ctx = telemetry.extract_context(headers)
        with telemetry.tracer.start_as_current_span(
            "process prior_auth_request",
            context=ctx,
            kind=trace.SpanKind.CONSUMER,
            attributes={
                "messaging.system": "rabbitmq",
                "priorauth.request_id": str(request_id),
                "priorauth.attempt": attempt,
            },
        ) as span:
            log.info("message_received")
            try:
                result = self.processor.process(request_id, attempt)
            except Exception as exc:  # e.g. the database is down: no state was written, so retry the message
                log.exception("processing_crashed")
                outcome = Outcome.RETRY if attempt < self.settings.max_attempts else Outcome.DEAD
                result = Result(outcome, f"worker error: {type(exc).__name__}")
            span.set_attribute("priorauth.outcome", result.outcome.value)

            routing_key, next_attempt = route(result, attempt)
            if routing_key is not None:
                extra = {LAST_ERROR_HEADER: (result.error or "")[:500]}
                ch.basic_publish(
                    EXCHANGE, routing_key, encode(request_id), build_properties(request_id, next_attempt, extra)
                )
                log.info("message_routed", to=routing_key, next_attempt=next_attempt)
            # Ack only after the follow-up message is confirmed, so a crash in between causes a
            # duplicate (handled) rather than a lost request.
            ch.basic_ack(method.delivery_tag)

    def run(self) -> None:
        params = pika.URLParameters(self.settings.amqp_url)
        params.heartbeat = 60
        while not self._stopping:
            try:
                conn = pika.BlockingConnection(params)
                ch = conn.channel()
                ch.confirm_delivery()
                declare_topology(ch, self.settings.retry_delay_ms)
                ch.basic_qos(prefetch_count=1)
                ch.basic_consume(QUEUE, self.on_message)
                self._channel = ch
                log.info("worker_consuming", queue=QUEUE)
                ch.start_consuming()
                conn.close()
            except (AMQPConnectionError, AMQPError) as exc:
                if self._stopping:
                    break
                log.warning("broker_unavailable_reconnecting", error=repr(exc))
                time.sleep(3)

    def stop(self, *_):
        log.info("worker_stopping")
        self._stopping = True
        if self._channel is not None:
            self._channel.connection.add_callback_threadsafe(self._channel.stop_consuming)


def main() -> None:
    settings = get_settings()
    configure_logging("priorauth-worker", settings.log_level)
    telemetry.configure_tracing("priorauth-worker")
    telemetry.instrument_httpx()
    telemetry.instrument_sqlalchemy(get_engine())
    start_http_server(settings.worker_metrics_port)

    processor = Processor(
        get_session_factory(),
        PayerClient(settings.payer_base_url, settings.payer_timeout_seconds),
        FhirClient(settings.fhir_base_url, settings.fhir_timeout_seconds),
        settings.max_attempts,
    )
    consumer = Consumer(settings, processor)
    signal.signal(signal.SIGTERM, consumer.stop)
    signal.signal(signal.SIGINT, consumer.stop)
    consumer.run()


if __name__ == "__main__":
    main()
