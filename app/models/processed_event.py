from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessedEvent(Base):
    """Consumer-side dedupe: PK (consumer, event_id)."""

    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
