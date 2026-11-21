import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrgPolicy(Base):
    """`version` backs If-Match updates. member_spend_cap_paise NULL = no cap (money is int paise)."""

    __tablename__ = "org_policies"

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"), primary_key=True)
    lock_modes: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_mode_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    member_spend_cap_paise: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[uuid.UUID | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
