import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Job(Base):
    """Scheduled work. `ux_jobs_open_dedupe` keeps a recurring job from being queued twice while one is open."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str | None] = mapped_column(Text)
