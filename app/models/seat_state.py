import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SeatState(Base):
    """One row per organisation; the row locked FOR UPDATE on every join. seats_bought NULL = no cap."""

    __tablename__ = "seat_state"

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"), primary_key=True)
    plan_code: Mapped[str] = mapped_column(Text)
    variant: Mapped[str] = mapped_column(Text)
    seats_bought: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    pending_seats: Mapped[int | None] = mapped_column(Integer)      # a decrease billing will apply at the period end
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
