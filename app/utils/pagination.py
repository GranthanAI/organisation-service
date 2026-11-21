"""Opaque keyset cursors: base64 of "<timestamp>|<id>". A bad cursor is a 422, never a 500."""

import base64
import uuid
from datetime import datetime

from app.core.exceptions import ValidationFailed

MAX_LIMIT = 100
DEFAULT_LIMIT = 50


def encode_cursor(stamp: datetime, row_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{stamp.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, ident = raw.partition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except Exception as exc:
        raise ValidationFailed("cursor is not valid") from exc
