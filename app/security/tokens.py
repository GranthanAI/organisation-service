"""Invite tokens: shown once, only the SHA-256 is stored."""

import hashlib
import hmac
import secrets


def new_token() -> tuple[str, str]:
    """(raw, sha256_hex) from 32 random bytes. The raw token goes in the link and is never stored or logged."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison of two hex digests (or any two strings)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
