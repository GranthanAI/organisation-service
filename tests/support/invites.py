"""Seed an invite whose link token is derived the way the service derives it, and return the token a recipient would click."""

import uuid
from datetime import timedelta

from app.db.session import unit_of_work
from app.security.tokens import invite_token, invite_token_hash
from tests.support.factories import NOW, make_invite


async def seed_invite(session_factory, org_id, email="invitee@amity.edu", role="member", created_by=None, kind="email",
                      version=1, expires_in=timedelta(days=14), **kw):
    invite_id = kw.pop("id", None) or uuid.uuid4()
    async with unit_of_work(session_factory) as s:
        invite = make_invite(
            org_id, id=invite_id, kind=kind, email=email, role=role, token_hash=invite_token_hash(invite_id, version),
            token_version=version, created_by=created_by, expires_at=NOW + expires_in, **kw)
        s.add(invite)
    return invite, invite_token(invite_id, version)
