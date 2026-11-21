import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Membership(Base):
    """One person's time in one organisation. `ux_memberships_one_active_org` allows one active row per user (D1)."""

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column()
    role: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    invited_by: Mapped[uuid.UUID | None] = mapped_column()
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(Text)
