import json
import logging

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from priorauth import telemetry
from priorauth.log import configure_logging, get_logger
from priorauth.queue import ATTEMPT_HEADER, build_properties


def test_trace_context_travels_in_message_headers():
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("api") as span:
        props = build_properties("rid-1", 2)
        sent_trace = span.get_span_context().trace_id
    assert props.headers[ATTEMPT_HEADER] == 2
    assert "traceparent" in props.headers
    assert props.message_id == "rid-1:2"

    ctx = telemetry.extract_context(props.headers)
    assert trace.get_current_span(ctx).get_span_context().trace_id == sent_trace


def test_logs_are_json_with_trace_id(capsys):
    configure_logging("test-svc", "INFO")
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        get_logger("t").info("hello", request_id="r-1")
        trace_id = format(span.get_span_context().trace_id, "032x")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "hello"
    assert line["service"] == "test-svc"
    assert line["request_id"] == "r-1"
    assert line["trace_id"] == trace_id
    logging.getLogger().setLevel("WARNING")


def test_retry_queue_name_carries_the_delay():
    from priorauth.queue import retry_queue, retry_routing_key

    # Changing RETRY_DELAY_MS must create a new queue, not clash with the old one's x-message-ttl.
    assert retry_queue(5000) == "priorauth.requests.retry.5000ms"
    assert retry_routing_key(5000) == "retry.5000ms"
    assert retry_queue(1000) != retry_queue(5000)
