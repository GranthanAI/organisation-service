import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrgDomain(Base):
    """`ux_org_domains_verified`: a domain is verified for one organisation only."""

    __tablename__ = "org_domains"

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"), primary_key=True)
    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    verify_token: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column()
    verify_method: Mapped[str | None] = mapped_column(Text)
    auto_join: Mapped[bool] = mapped_column(Boolean, default=False)
    include_subdomains: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_checks: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JoinRequest(Base):
    __tablename__ = "join_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column()
    domain: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
