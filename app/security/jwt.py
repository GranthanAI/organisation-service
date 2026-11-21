"""Verify an access token issued by auth-service: HS256 with the shared secret.

Signature, `exp` (10 s leeway) and issuer are ALWAYS verified, and sub, email, jti, exp, iat and iss must be present.
Any failure raises Unauthenticated, which says nothing about why. Tokens are never logged.
"""

from typing import Any

import jwt

from app.core.config import settings
from app.core.constants import JWT_LEEWAY_SECONDS, JWT_REQUIRED_CLAIMS
from app.core.exceptions import Unauthenticated


def verify_jwt_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],   # a fixed list: "none" and other algorithms are refused
            issuer=settings.JWT_ISSUER,
            leeway=JWT_LEEWAY_SECONDS,
            options={"require": list(JWT_REQUIRED_CLAIMS), "verify_signature": True, "verify_exp": True,
                     "verify_iss": True},
        )
    except jwt.PyJWTError as exc:
        raise Unauthenticated() from exc
