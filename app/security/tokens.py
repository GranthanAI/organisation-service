"""Invite tokens.

An invite link carries a token DERIVED from the invite: HMAC-SHA256(INVITE_TOKEN_SECRET, "invite:<id>:<token_version>"),
base64url without padding. The raw token is never stored (not in invites, the outbox, jobs, logs, Kafka or API
responses); the invite row keeps only its SHA-256 (`token_hash`), which is how a link is looked up. A job that sends the
email recomputes the token at send time. Bumping `token_version` (a resend) gives a new token, so the old link dies at once.
"""

import base64
import hashlib
import hmac
import uuid

from app.core.config import settings


def invite_token(invite_id: uuid.UUID, token_version: int, secret: str | None = None) -> str:
    key = (settings.INVITE_TOKEN_SECRET if secret is None else secret).encode("utf-8")
    digest = hmac.new(key, f"invite:{invite_id}:{token_version}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invite_token_hash(invite_id: uuid.UUID, token_version: int, secret: str | None = None) -> str:
    """What `invites.token_hash` holds for this invite and version."""
    return hash_token(invite_token(invite_id, token_version, secret))


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison of two hex digests (or any two strings)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
