"""Constants shared across phases: audit action names, rate-limit buckets, idempotency settings."""

from dataclasses import dataclass

# --- Audit action names (audit_log.action) ---
AUDIT_MEMBER_JOINED = "member.joined"
AUDIT_MEMBER_LEFT = "member.left"
AUDIT_MEMBER_REMOVED = "member.removed"
AUDIT_MEMBER_ROLE_CHANGED = "member.role_changed"
AUDIT_ORG_CREATED = "org.created"
AUDIT_ORG_STATUS_CHANGED = "org.status_changed"
AUDIT_SEATS_SET = "seats.set"
AUDIT_INVITE_CREATED = "invite.created"
AUDIT_INVITE_REVOKED = "invite.revoked"
AUDIT_DOMAIN_VERIFIED = "domain.verified"
AUDIT_POLICY_CHANGED = "policy.changed"
AUDIT_ORG_OWNER_MISSING = "org.owner_missing"

AUDIT_ACTIONS = frozenset({
    AUDIT_MEMBER_JOINED, AUDIT_MEMBER_LEFT, AUDIT_MEMBER_REMOVED, AUDIT_MEMBER_ROLE_CHANGED, AUDIT_ORG_CREATED,
    AUDIT_ORG_STATUS_CHANGED, AUDIT_SEATS_SET, AUDIT_INVITE_CREATED, AUDIT_INVITE_REVOKED, AUDIT_DOMAIN_VERIFIED,
    AUDIT_POLICY_CHANGED, AUDIT_ORG_OWNER_MISSING,
})

AUDIT_DETAILS_MAX_BYTES = 8 * 1024
AUDIT_FORBIDDEN_KEY_PARTS = ("token", "secret", "password")


# --- Rate limits (Redis fixed windows) ---
@dataclass(frozen=True)
class RateLimitBucket:
    name: str
    limit: int
    window_seconds: int
    key: str            # "user" or "ip"
    fail_closed: bool   # True: Redis down refuses the request (503); False: it passes


RATE_INVITE_CREATE = RateLimitBucket("invite_create", 30, 60, "user", fail_closed=True)
RATE_CSV_UPLOAD = RateLimitBucket("csv_upload", 5, 3600, "user", fail_closed=True)
RATE_INVITE_PREVIEW = RateLimitBucket("invite_preview", 30, 60, "ip", fail_closed=False)
RATE_INVITE_ACCEPT = RateLimitBucket("invite_accept", 10, 60, "user", fail_closed=False)

RATE_BUCKETS = {b.name: b for b in (RATE_INVITE_CREATE, RATE_CSV_UPLOAD, RATE_INVITE_PREVIEW, RATE_INVITE_ACCEPT)}

# --- Idempotency ---
IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_REPLAY_HEADER = "Idempotent-Replayed"
IDEMPOTENCY_LOCK_SECONDS = 60
IDEMPOTENCY_TTL_HOURS = 24

# --- JWT ---
JWT_LEEWAY_SECONDS = 10
JWT_REQUIRED_CLAIMS = ("sub", "email", "jti", "exp", "iat", "iss")
