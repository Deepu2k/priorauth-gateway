import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from priorauth.api.main import create_app
from priorauth.db import get_session, make_engine
from priorauth.models import Base


class FakePublisher:
    def __init__(self):
        self.published = []
        self.fail = False

    def publish(self, request_id):
        if self.fail:
            raise ConnectionError("broker down")
        self.published.append(request_id)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def publisher():
    return FakePublisher()


@pytest.fixture
def client(session_factory, publisher):
    app = create_app(publisher=publisher, instrument=False)

    def _session():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as c:
        yield c


def login(client, user, pw):
    r = client.post("/auth/token", data={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def alice(client):
    return login(client, "alice", "alice-pw")


@pytest.fixture
def carol(client):
    return login(client, "carol", "carol-pw")


@pytest.fixture
def viewer(client):
    return login(client, "vic", "vic-pw")


@pytest.fixture
def admin(client):
    return login(client, "root", "root-pw")
