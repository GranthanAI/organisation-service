"""seats.refresh_all (nightly): with SEAT_SOURCE=billing, ask billing about every organisation that has a paid or billing-sourced
seat row, 50 at a time, so a lost subscription.changed never leaves seats wrong for long. With SEAT_SOURCE=staff it does nothing."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.clock import Clock
from app.core.config import settings
from app.core.logging import logger
from app.models.job import Job
from app.services import seat_source
from app.services.job_handlers import job_handler


@job_handler(constants.JOB_SEATS_REFRESH_ALL)
async def seats_refresh_all(session: AsyncSession, job: Job, clock: Clock) -> None:
    if settings.SEAT_SOURCE != "billing":
        return
    totals = await seat_source.refresh_all(_factory(session), clock)       # its own transactions, a batch at a time
    logger.info("seats refreshed from billing", **totals)


def _factory(session: AsyncSession):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    return async_sessionmaker(bind=session.bind, expire_on_commit=False, autoflush=False)
