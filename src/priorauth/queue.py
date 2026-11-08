"""RabbitMQ topology and publishing.

    priorauth (direct exchange)
      "request" -> priorauth.requests        work queue; rejected messages dead-letter to "dead"
      "retry"   -> priorauth.requests.retry  no consumer; messages wait RETRY_DELAY_MS, then
                                             dead-letter back to "request" for the next attempt
      "dead"    -> priorauth.requests.dlq    the dead letter queue, kept for inspection

The worker counts attempts in the `x-attempt` header. After MAX_ATTEMPTS failures it publishes to
"dead" with the last error in `x-last-error`.

Note: the retry delay is a queue argument. Changing RETRY_DELAY_MS on an existing broker needs the
retry queue deleted first (RabbitMQ refuses to redeclare a queue with different arguments).
"""

import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError

from priorauth import telemetry

EXCHANGE = "priorauth"
QUEUE = "priorauth.requests"
RETRY_QUEUE = "priorauth.requests.retry"
DLQ = "priorauth.requests.dlq"
RK_REQUEST, RK_RETRY, RK_DEAD = "request", "retry", "dead"

ATTEMPT_HEADER = "x-attempt"
LAST_ERROR_HEADER = "x-last-error"


def declare_topology(channel: BlockingChannel, retry_delay_ms: int) -> None:
    channel.exchange_declare(EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(
        QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": EXCHANGE, "x-dead-letter-routing-key": RK_DEAD},
    )
    channel.queue_declare(
        RETRY_QUEUE,
        durable=True,
        arguments={
            "x-message-ttl": retry_delay_ms,
            "x-dead-letter-exchange": EXCHANGE,
            "x-dead-letter-routing-key": RK_REQUEST,
        },
    )
    channel.queue_declare(DLQ, durable=True)
    channel.queue_bind(QUEUE, EXCHANGE, RK_REQUEST)
    channel.queue_bind(RETRY_QUEUE, EXCHANGE, RK_RETRY)
    channel.queue_bind(DLQ, EXCHANGE, RK_DEAD)


def build_properties(request_id: uuid.UUID | str, attempt: int, extra: dict[str, Any] | None = None):
    headers = {ATTEMPT_HEADER: attempt, **telemetry.inject_headers(), **(extra or {})}
    return pika.BasicProperties(
        content_type="application/json",
        delivery_mode=pika.DeliveryMode.Persistent,
        message_id=f"{request_id}:{attempt}",
        headers=headers,
    )


def encode(request_id: uuid.UUID | str) -> bytes:
    return json.dumps({"request_id": str(request_id)}).encode()


class Publisher(Protocol):
    def publish(self, request_id: uuid.UUID) -> None: ...


class RabbitPublisher:
    """Thread-safe publisher with publisher confirms, so a 202 means the broker has the message."""

    def __init__(self, amqp_url: str, retry_delay_ms: int):
        self._params = pika.URLParameters(amqp_url)
        self._retry_delay_ms = retry_delay_ms
        self._lock = threading.Lock()
        self._conn: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None

    def _connect(self) -> BlockingChannel:
        if self._channel is None or self._channel.is_closed:
            self._conn = pika.BlockingConnection(self._params)
            self._channel = self._conn.channel()
            self._channel.confirm_delivery()
            declare_topology(self._channel, self._retry_delay_ms)
        return self._channel

    def _reset(self) -> None:
        try:
            if self._conn and self._conn.is_open:
                self._conn.close()
        except AMQPError:
            pass
        self._conn = self._channel = None

    def publish(self, request_id: uuid.UUID) -> None:
        with self._lock:
            for attempt in (1, 2):  # one reconnect covers a connection the broker dropped while idle
                try:
                    ch = self._connect()
                    ch.basic_publish(
                        EXCHANGE, RK_REQUEST, encode(request_id), build_properties(request_id, 1), mandatory=True
                    )
                    return
                except AMQPError:
                    self._reset()
                    if attempt == 2:
                        raise

    def close(self) -> None:
        with self._lock:
            self._reset()


@dataclass
class DeadLetter:
    request_id: str | None
    attempts: int | None
    last_error: str | None
    body: str


def peek_dlq(amqp_url: str, retry_delay_ms: int, limit: int = 50) -> tuple[int, list[DeadLetter]]:
    """Read up to `limit` DLQ messages without removing them: they are never acked, so closing the
    connection returns them to the queue."""
    conn = pika.BlockingConnection(pika.URLParameters(amqp_url))
    try:
        ch = conn.channel()
        declare_topology(ch, retry_delay_ms)
        depth = ch.queue_declare(DLQ, passive=True).method.message_count
        items: list[DeadLetter] = []
        for _ in range(min(limit, depth)):
            method, props, body = ch.basic_get(DLQ, auto_ack=False)
            if method is None:
                break
            headers = props.headers or {}
            try:
                rid = json.loads(body).get("request_id")
            except (ValueError, AttributeError):
                rid = None
            last_error = headers.get(LAST_ERROR_HEADER)
            if isinstance(last_error, bytes):
                last_error = last_error.decode(errors="replace")
            items.append(DeadLetter(rid, headers.get(ATTEMPT_HEADER), last_error, body.decode(errors="replace")))
        return depth, items
    finally:
        conn.close()
