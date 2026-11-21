"""structlog JSON logging with the request's correlation_id injected into every record."""

import logging
import sys

import structlog

from app.core.config import settings


def _inject_correlation_id(logger, method_name, event_dict):
    try:
        from app.middleware.correlation import get_correlation_id
        cid = get_correlation_id()
        if cid:
            event_dict["correlation_id"] = cid
    except Exception:
        pass  # logging must never break a request
    return event_dict


def setup_logging() -> None:
    processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_correlation_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(processor=processors[-1]))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
    logging.getLogger("aiokafka").setLevel(logging.WARNING)


setup_logging()
logger = structlog.get_logger("app")
