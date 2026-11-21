import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    """Insert-only: the trigger forbid_change() rejects UPDATE and DELETE (and org_app has no such grant)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_kind: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
