"""Authentication (JWT for people, API keys for partner systems) and role checks.

Roles:
  submitter  may submit prior auth requests and read the ones it submitted
  viewer     may read any request and its history
  admin      may do everything, including inspecting the dead letter queue

The token endpoint with demo users stands in for an OIDC provider (Keycloak, Okta, Cognito). The
API only needs a signed token with `sub` and `roles`, so swapping in a real provider changes
`decode_token` and nothing else.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from priorauth.config import Settings, get_settings


class Role(StrEnum):
    SUBMITTER = "submitter"
    VIEWER = "viewer"
    ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    method: str = "jwt"

    def has_any(self, *roles: str) -> bool:
        return Role.ADMIN in self.roles or any(r in self.roles for r in roles)


@dataclass(frozen=True)
class Credential:
    name: str
    secret_hash: str
    roles: frozenset[str]


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_credentials(spec: str) -> list[Credential]:
    """Parse 'name:sha256hex:role1|role2,name2:...'."""
    creds = []
    for raw in filter(None, (s.strip() for s in spec.split(","))):
        name, secret_hash, roles = raw.split(":", 2)
        creds.append(Credential(name, secret_hash.lower(), frozenset(filter(None, roles.split("|")))))
    return creds


def _match(creds: list[Credential], secret: str, name: str | None = None) -> Credential | None:
    digest = hash_secret(secret)
    found = None
    for c in creds:  # compare against every entry so timing does not reveal which one matched
        if hmac.compare_digest(c.secret_hash, digest) and (name is None or c.name == name):
            found = c
    return found


def authenticate_user(settings: Settings, username: str, password: str) -> Principal | None:
    cred = _match(parse_credentials(settings.demo_users), password, name=username)
    return Principal(cred.name, cred.roles, "jwt") if cred else None


def issue_token(settings: Settings, principal: Principal) -> tuple[str, int]:
    now = datetime.now(UTC)
    ttl = settings.jwt_ttl_minutes * 60
    claims = {
        "sub": principal.subject,
        "roles": sorted(principal.roles),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256"), ttl


def decode_token(settings: Settings, token: str) -> Principal:
    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],  # pinned: never trust the alg in the token header
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["sub", "exp", "iss", "aud"]},
    )
    return Principal(claims["sub"], frozenset(claims.get("roles", [])), "jwt")


bearer_scheme = HTTPBearer(auto_error=False, description="JWT from POST /auth/token")
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, description="Partner system API key")


def _unauthorized(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=msg, headers={"WWW-Authenticate": "Bearer"})


def current_principal(
    bearer: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    api_key: str | None = Depends(api_key_scheme),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if bearer is not None:
        try:
            return decode_token(settings, bearer.credentials)
        except jwt.ExpiredSignatureError:
            raise _unauthorized("token expired") from None
        except jwt.InvalidTokenError:
            raise _unauthorized("invalid token") from None
    if api_key is not None:
        cred = _match(parse_credentials(settings.api_keys), api_key)
        if cred is None:
            raise _unauthorized("invalid API key")
        return Principal(cred.name, cred.roles, "api_key")
    raise _unauthorized("missing credentials: send a Bearer token or an X-API-Key header")


def require_role(*roles: Role):
    """Dependency factory: the role check runs before the endpoint body does anything."""

    def checker(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_any(*roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"requires one of the roles: {', '.join(roles)}")
        return principal

    return checker
