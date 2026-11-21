"""Sign test JWTs the way auth-service does (HS256, dev secret, issuer https://auth.granthan.com)."""

import time
import uuid

import jwt

from app.core.config import settings


def make_token(
    *,
    sub: uuid.UUID | str | None = None,
    email: str = "student@amity.edu",
    roles: list[str] | None = None,
    jti: str | None = None,
    iss: str | None = None,
    secret: str | None = None,
    algorithm: str = "HS256",
    expires_in: int = 900,
    omit: tuple[str, ...] = (),
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "sub": str(sub or uuid.uuid4()),
        "email": email,
        "roles": roles if roles is not None else [],
        "jti": jti or uuid.uuid4().hex,
        "iat": now,
        "exp": now + expires_in,
        "iss": iss if iss is not None else settings.JWT_ISSUER,
        **(extra or {}),
    }
    for name in omit:
        claims.pop(name, None)
    key = secret if secret is not None else settings.JWT_SECRET_KEY
    return jwt.encode(claims, None if algorithm == "none" else key, algorithm=algorithm)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
