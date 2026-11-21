"""Importing this package registers every model on Base.metadata (Alembic's env.py relies on it)."""

from app.models.audit_log import AuditLog
from app.models.domain import JoinRequest, OrgDomain
from app.models.idempotency_key import IdempotencyKey
from app.models.invite import Invite, InviteBatch
from app.models.job import Job
from app.models.known_user import KnownUser
from app.models.membership import Membership
from app.models.organisation import Organisation
from app.models.outbox import OutboxEvent
from app.models.policy import OrgPolicy
from app.models.processed_event import ProcessedEvent
from app.models.seat_state import SeatState

__all__ = [
    "AuditLog", "IdempotencyKey", "Invite", "InviteBatch", "Job", "JoinRequest", "KnownUser", "Membership",
    "OrgDomain", "OrgPolicy", "Organisation", "OutboxEvent", "ProcessedEvent", "SeatState",
]
