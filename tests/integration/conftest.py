"""Real Postgres, real RabbitMQ, the real worker, and the mock health plan over real HTTP.

Postgres and RabbitMQ come from Testcontainers (needs Docker), or from TEST_DATABASE_URL and
TEST_AMQP_URL when those are set (for machines without Docker).
"""

import os
import socket
import threading
import time
from collections.abc import Callable

import pika
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from priorauth import config, db
from priorauth.queue import DLQ, QUEUE, RETRY_QUEUE

RETRY_DELAY_MS = 300
PAYER_TIMEOUT = 0.5


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def infra():
    db_url, amqp_url = os.getenv("TEST_DATABASE_URL"), os.getenv("TEST_AMQP_URL")
    if db_url and amqp_url:
        yield db_url, amqp_url
        return
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.rabbitmq import RabbitMqContainer

    with (
        PostgresContainer("postgres:16-alpine", driver="psycopg") as pg,
        RabbitMqContainer("rabbitmq:3.13-alpine") as mq,
    ):
        amqp = f"amqp://guest:guest@{mq.get_container_host_ip()}:{mq.get_exposed_port(5672)}/%2F"
        yield pg.get_connection_url(), amqp


@pytest.fixture(scope="session")
def migrated(infra):
    db_url, amqp_url = infra
    engine = create_engine(db_url)
    with engine.begin() as conn:  # start clean, then prove the migrations build the schema
        conn.execute(text("DROP TABLE IF EXISTS status_history, prior_auth_requests, alembic_version CASCADE"))
    engine.dispose()
    cfg = Config("alembic.ini")
    cfg.attributes["url"] = db_url
    command.upgrade(cfg, "head")

    conn = pika.BlockingConnection(pika.URLParameters(amqp_url))
    ch = conn.channel()
    for q in (QUEUE, RETRY_QUEUE, DLQ):  # retry TTL is a queue argument, so redeclare fresh
        ch.queue_delete(q)
    conn.close()
    return db_url, amqp_url


@pytest.fixture(scope="session")
def mock_payer_url():
    from priorauth.mock_payer import main as mock

    mock.TIMEOUT_SLEEP_SECONDS = 2.0  # longer than PAYER_TIMEOUT
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mock.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


@pytest.fixture(scope="session")
def settings(migrated, mock_payer_url):
    db_url, amqp_url = migrated
    os.environ.update(
        DATABASE_URL=db_url,
        AMQP_URL=amqp_url,
        PAYER_BASE_URL=mock_payer_url,
        FHIR_BASE_URL=f"{mock_payer_url}/fhir",
        RETRY_DELAY_MS=str(RETRY_DELAY_MS),
        PAYER_TIMEOUT_SECONDS=str(PAYER_TIMEOUT),
        MAX_ATTEMPTS="3",
    )
    for fn in (config.get_settings, db.get_engine, db.get_session_factory):
        fn.cache_clear()
    return config.get_settings()


@pytest.fixture(scope="session")
def worker(settings):
    from priorauth.clients.fhir_server import FhirClient
    from priorauth.clients.payer import PayerClient
    from priorauth.worker.main import Consumer
    from priorauth.worker.processor import Processor

    processor = Processor(
        db.get_session_factory(),
        PayerClient(settings.payer_base_url, settings.payer_timeout_seconds),
        FhirClient(settings.fhir_base_url, settings.fhir_timeout_seconds),
        settings.max_attempts,
    )
    consumer = Consumer(settings, processor)
    t = threading.Thread(target=consumer.run, daemon=True)
    t.start()
    yield consumer
    consumer.stop()
    t.join(5)


@pytest.fixture(scope="session")
def api(settings, worker):
    from priorauth.api.main import create_app

    with TestClient(create_app(instrument=False)) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_tables(settings):
    with db.get_engine().begin() as conn:
        conn.execute(text("TRUNCATE status_history, prior_auth_requests CASCADE"))


@pytest.fixture
def token(api) -> Callable[[str, str], dict]:
    def _login(user: str, pw: str) -> dict:
        r = api.post("/auth/token", data={"username": user, "password": pw})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _login


def wait_for(fn, *, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(interval)
