"""FastAPI dependencies: who is calling, are they staff, are they in this org with this right.

Nothing here logs the Authorization header, a raw token or the jti.
"""

import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_redis_client
from app.core.enums import Role, StaffRole
from app.core.exceptions import ForbiddenRole, NotFound, Unauthenticated, UpstreamUnavailable
from app.core.logging import logger
from app.repositories.known_user_repository import KnownUserRepository
from app.repositories.membership_repository import MembershipRepository
from app.security.jwt import verify_jwt_token
from app.security.known_user_sync import sync_known_user
from app.security.models import CurrentUser, OrgContext
from app.services import permission_rules
from app.services.permission_rules import Action, Permission

_bearer = HTTPBearer(auto_error=False)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
BLACKLIST_PREFIX = "blacklist:"   # auth-service writes "blacklist:<jti>" into the shared Redis on logout


def bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


async def authenticate_user(method: str, token: str | None, redis: Redis, session: AsyncSession) -> CurrentUser:
    """The one identity check, shared by get_current_user and IdempotentRoute (before it replays a stored
    response): verify the JWT, refuse a revoked jti and a deleted user. Every refusal is a bare 401.

    Redis down means revocation cannot be checked: reads go through (logged), writes get 503."""
    if not token:
        raise Unauthenticated()
    user = CurrentUser.from_claims(verify_jwt_token(token))
    try:
        revoked = bool(await redis.exists(f"{BLACKLIST_PREFIX}{user.jti}"))
    except Exception:
        if method in SAFE_METHODS:
            logger.warning("token blacklist unavailable, allowing read", method=method)
            revoked = False
        else:
            raise UpstreamUnavailable("Cannot verify the session right now") from None
    if revoked:
        raise Unauthenticated()
    known = await KnownUserRepository(session).get(user.id)
    if known is not None and known.deleted_at is not None:
        await session.rollback()
        raise Unauthenticated()
    stored_email = None if known is None else known.email
    await session.rollback()      # give the connection back NOW: a route that then opens its own transaction must not hold two
    await sync_known_user(session, user.id, user.email, None if known is None else SimpleNamespace(email=stored_email))
    return user


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    redis: Redis = Depends(get_redis_client),
    session: AsyncSession = Depends(get_db_session),
) -> CurrentUser:
    token = credentials.credentials if credentials else None
    return await authenticate_user(request.method, token, redis, session)


def require_staff(min_role: StaffRole) -> Callable[..., Awaitable[CurrentUser]]:
    """SUPPORT allows support and admin; ADMIN allows admin only. A non-staff caller gets 403."""
    allowed = {StaffRole.SUPPORT: {StaffRole.SUPPORT, StaffRole.ADMIN}, StaffRole.ADMIN: {StaffRole.ADMIN}}[min_role]

    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.staff_role not in allowed:
            raise ForbiddenRole("Staff access required")
        return user

    return dependency


def require_org_role(action: Action) -> Callable[..., Awaitable[OrgContext]]:
    """Dependency factory for routes with `{org_id}` in the path. Loads the caller's ACTIVE membership in that org
    and checks the matrix. Not a member (never joined, left, removed, wrong org) is 404, never 403, so org ids
    cannot be probed. Staff do not use this: they have admin routes.

    An OwnRoleOnly cell (an admin removing someone) passes here: it depends on the target's role, which only the
    use case knows. Call `ctx.check_target(target_role)` for remove and role changes (Phases 5-6)."""

    async def dependency(
        org_id: uuid.UUID,
        user: CurrentUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_db_session),
    ) -> OrgContext:
        membership = await MembershipRepository(session).get_active(org_id, user.id)
        if membership is None:
            raise NotFound("Organisation not found")
        role, membership_id = Role(membership.role), membership.id
        await session.rollback()      # release the connection before the route opens its own transaction (no request holds two)
        if permission_rules.permission_for(action, role) is not Permission.OWN_ROLE_ONLY:
            permission_rules.check(action, role)
        return OrgContext(org_id=org_id, user=user, role=role, membership_id=membership_id, action=action)

    return dependency
