def sub(p, o, n):
    t = open(p, encoding="utf-8").read()
    assert o in t, (p, o[:70])
    open(p, "w", encoding="utf-8").write(t.replace(o, n, 1))


D = "app/security/dependencies.py"
sub(D, '''    known = await KnownUserRepository(session).get(user.id)
    if known is not None and known.deleted_at is not None:
        raise Unauthenticated()
    await sync_known_user(session, user.id, user.email, known)
    return user''', '''    known = await KnownUserRepository(session).get(user.id)
    if known is not None and known.deleted_at is not None:
        await session.rollback()
        raise Unauthenticated()
    stored_email = None if known is None else known.email
    await session.rollback()      # give the connection back NOW: a route that then opens its own transaction must not hold two
    await sync_known_user(session, user.id, user.email, None if known is None else SimpleNamespace(email=stored_email))
    return user''')
sub(D, "from typing import Optional\n" if "from typing import Optional\n" in open(D, encoding="utf-8").read() else "import uuid\n",
    ("from types import SimpleNamespace\nfrom typing import Optional\n" if "from typing import Optional\n" in open(D, encoding="utf-8").read() else "import uuid\nfrom types import SimpleNamespace\n"))
sub(D, '''        role = Role(membership.role)
        if permission_rules.permission_for(action, role) is not Permission.OWN_ROLE_ONLY:''', '''        role, membership_id = Role(membership.role), membership.id
        await session.rollback()      # release the connection before the route opens its own transaction (no request holds two)
        if permission_rules.permission_for(action, role) is not Permission.OWN_ROLE_ONLY:''')
sub(D, "return OrgContext(org_id=org_id, user=user, role=role, membership_id=membership.id, action=action)", "return OrgContext(org_id=org_id, user=user, role=role, membership_id=membership_id, action=action)")

# the expired-invite close no longer opens a second connection inside the transaction
S = "app/services/invite_service.py"
t = open(S, encoding="utf-8").read()
a = t.index("async def _close_expired_alone(")
b = t.index("async def accept(")
t = t[:a] + '''async def close_expired(session: AsyncSession, invite_id: uuid.UUID, now: datetime) -> None:
    """Close an expired invite (revoked_at, reason expired). The route calls it in its OWN transaction after accept() raised
    InviteExpired (which rolled back the first one): a request never holds two connections."""
    invite = await InviteRepository(session).get_for_update(invite_id)
    if invite is not None and invite.accepted_at is None and invite.revoked_at is None:
        invite.revoked_at, invite.revoke_reason = now, InviteRevokeReason.EXPIRED.value


''' + t[b:]
t = t.replace('''        await _close_expired_alone(session, found.id, now)
        raise InviteExpired("This invitation has expired")''', '''        error = InviteExpired("This invitation has expired")
        error.close_invite_id = found.id                       # the route closes it after this transaction rolls back
        raise error''')
open(S, "w", encoding="utf-8").write(t)

R = "app/api/v1/invites.py"
sub(R, '''    async with unit_of_work(factory) as session:
        return await invite_service.accept(session, user, body.token, clock)''', '''    try:
        async with unit_of_work(factory) as session:
            return await invite_service.accept(session, user, body.token, clock)
    except InviteExpired as exc:
        if getattr(exc, "close_invite_id", None) is not None:       # close it, in a transaction of its own, then answer 410
            async with unit_of_work(factory) as session:
                await invite_service.close_expired(session, exc.close_invite_id, clock.now())
        raise''')
sub(R, "from app.core.exceptions import NotFound\n", "from app.core.exceptions import InviteExpired, NotFound\n")

# pool timeout and a clear answer when the pool is exhausted
sub("app/core/config.py", "    DB_POOL_RECYCLE_SECONDS: int = 1800", "    DB_POOL_TIMEOUT_SECONDS: float = 10.0  # how long a request waits for a free connection before answering 503\n    DB_POOL_RECYCLE_SECONDS: int = 1800")
sub(".env.example", "DB_POOL_RECYCLE_SECONDS=1800\n", "DB_POOL_RECYCLE_SECONDS=1800\n# A request waits at most this long for a free connection, then answers 503 (Retry-After 1).\nDB_POOL_TIMEOUT_SECONDS=10\n")
sub("app/db/postgres.py", "            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,\n", "            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,\n            pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,\n")
E = "app/middleware/exception_handler.py"
sub(E, "    @app.exception_handler(Exception)\n    async def _unexpected", '''    @app.exception_handler(SQLAlchemyTimeoutError)
    async def _pool_exhausted(request: Request, exc: SQLAlchemyTimeoutError):
        logger.error("database connection pool exhausted", path=request.url.path, correlation_id=_correlation_id(request))
        return problem_response(request, 503, "UPSTREAM_UNAVAILABLE", "The service is busy, try again shortly.", headers={"Retry-After": "1"})

    @app.exception_handler(Exception)
    async def _unexpected''')
sub(E, "from fastapi.responses import JSONResponse\n", "from fastapi.responses import JSONResponse\nfrom sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError\n")
print("ok")
