"""organisations. The Alembic migrations are the authority for DDL; models mirror them (checked by
tests/postgres/test_migrations.py). Enum columns are text with CHECK constraints (values in app/core/enums.py)."""

import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, Text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Organisation(Base):
    __tablename__ = "organisations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text)
    gstin: Mapped[str | None] = mapped_column(Text)
    state_code: Mapped[str | None] = mapped_column(CHAR(2))
    billing_email: Mapped[str | None] = mapped_column(CITEXT)
    status: Mapped[str] = mapped_column(Text)
    pilot_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
