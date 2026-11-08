from datetime import UTC, datetime, timedelta

import jwt
import pytest

from priorauth.config import get_settings
from priorauth.security import Principal, decode_token, issue_token, parse_credentials


def test_no_credentials_is_401(client, valid_body):
    r = client.post("/requests", json=valid_body)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_password_is_401(client):
    assert client.post("/auth/token", data={"username": "alice", "password": "nope"}).status_code == 401


def test_password_of_other_user_is_401(client):
    assert client.post("/auth/token", data={"username": "alice", "password": "root-pw"}).status_code == 401


def test_viewer_cannot_submit(client, viewer, valid_body):
    r = client.post("/requests", json=valid_body, headers=viewer)
    assert r.status_code == 403
    assert "submitter" in r.json()["detail"]


def test_role_check_runs_before_validation(client, viewer):
    # An invalid body from someone without the role gets 403, not a 422 describing the schema.
    assert client.post("/requests", json={"junk": 1}, headers=viewer).status_code == 403


def test_submitter_cannot_list_all(client, alice):
    assert client.get("/requests", headers=alice).status_code == 403


def test_submitter_sees_only_own_requests(client, alice, carol, viewer, valid_body):
    rid = client.post("/requests", json=valid_body, headers=alice).json()["id"]
    assert client.get(f"/requests/{rid}", headers=carol).status_code == 404  # not 403: no id probing
    assert client.get(f"/requests/{rid}/history", headers=carol).status_code == 404
    assert client.get(f"/requests/{rid}", headers=viewer).status_code == 200


def test_admin_has_every_role(client, admin, valid_body):
    assert client.post("/requests", json=valid_body, headers=admin).status_code == 202
    assert client.get("/requests", headers=admin).status_code == 200


def test_only_admin_reads_dlq(client, viewer):
    assert client.get("/admin/dlq", headers=viewer).status_code == 403


def test_api_key_for_partner_system(client, valid_body):
    r = client.post("/requests", json=valid_body, headers={"X-API-Key": "pk-test-123"})
    assert r.status_code == 202
    rid = r.json()["id"]
    got = client.get(f"/requests/{rid}", headers={"X-API-Key": "pk-test-123"}).json()
    assert got["submittedBy"] == "partner-ehr"


def test_bad_api_key_is_401(client, valid_body):
    assert client.post("/requests", json=valid_body, headers={"X-API-Key": "guess"}).status_code == 401


def test_expired_token_is_401(client, valid_body):
    s = get_settings()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "alice",
            "roles": ["submitter"],
            "iss": s.jwt_issuer,
            "aud": s.jwt_audience,
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        s.jwt_secret,
        algorithm="HS256",
    )
    r = client.post("/requests", json=valid_body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["detail"] == "token expired"


def test_token_signed_with_other_key_is_401(client, valid_body):
    s = get_settings()
    token = jwt.encode(
        {
            "sub": "mallory",
            "roles": ["admin"],
            "iss": s.jwt_issuer,
            "aud": s.jwt_audience,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "x" * 40,
        algorithm="HS256",
    )
    assert client.get("/requests", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_unsigned_alg_none_token_rejected():
    s = get_settings()
    token = jwt.encode(
        {"sub": "mallory", "roles": ["admin"], "iss": s.jwt_issuer, "aud": s.jwt_audience}, None, algorithm="none"
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(s, token)


def test_token_roundtrip():
    s = get_settings()
    token, ttl = issue_token(s, Principal("alice", frozenset({"submitter"})))
    p = decode_token(s, token)
    assert (p.subject, p.roles, ttl) == ("alice", frozenset({"submitter"}), s.jwt_ttl_minutes * 60)


def test_parse_credentials():
    creds = parse_credentials(" a:ABC:submitter|viewer , b:def: ")
    assert [(c.name, c.secret_hash, c.roles) for c in creds] == [
        ("a", "abc", frozenset({"submitter", "viewer"})),
        ("b", "def", frozenset()),
    ]
