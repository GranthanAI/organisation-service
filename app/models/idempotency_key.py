import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    """Scoped to (actor_id, route); kept 24 h."""

    __tablename__ = "idempotency_keys"

    actor_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    route: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
