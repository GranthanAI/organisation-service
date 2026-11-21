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
AUDIT_ORG_DETAILS_CHANGED = "org.details_changed"
AUDIT_ORG_OWNERSHIP_TRANSFERRED = "org.ownership_transferred"

AUDIT_ACTIONS = frozenset({
    AUDIT_MEMBER_JOINED, AUDIT_MEMBER_LEFT, AUDIT_MEMBER_REMOVED, AUDIT_MEMBER_ROLE_CHANGED, AUDIT_ORG_CREATED,
    AUDIT_ORG_STATUS_CHANGED, AUDIT_SEATS_SET, AUDIT_INVITE_CREATED, AUDIT_INVITE_REVOKED, AUDIT_DOMAIN_VERIFIED,
    AUDIT_POLICY_CHANGED, AUDIT_ORG_OWNER_MISSING, AUDIT_ORG_DETAILS_CHANGED, AUDIT_ORG_OWNERSHIP_TRANSFERRED,
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


# --- GST (India) ---
# State and union-territory codes used in a GSTIN and as organisations.state_code (two digits).
GST_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}
# 15 characters: 2-digit state, 10-character PAN, entity number, a fixed Z, a check character.
GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"

# --- job kinds and dedupe keys (Phase 5) ---
JOB_PILOT_REMIND = "pilot.remind"
JOB_PILOT_EXPIRE = "pilot.expire"
JOB_ORG_CLOSE_MEMBERS = "org.close_members"
PILOT_REMIND_DAYS = (7, 1)          # org.pilot.ending is sent this many days before the pilot ends
CLOSE_BATCH_SIZE = 200              # memberships ended per transaction when an organisation is closed
