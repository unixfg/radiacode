from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from typing import cast

import structlog
from structlog.typing import EventDict, FilteringBoundLogger


def _redact_secrets(_: object, __: str, event_dict: EventDict) -> EventDict:
    for key in tuple(event_dict):
        lowered = key.lower()
        if any(token in lowered for token in ("serial", "password", "secret", "dsn", "token")):
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str = "INFO", additional_secrets: Iterable[str] = ()) -> None:
    secrets = tuple(secret for secret in additional_secrets if secret)

    def redact_values(_: object, __: str, event_dict: EventDict) -> EventDict:
        for key, value in tuple(event_dict.items()):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[redacted]")
                event_dict[key] = value
        return event_dict

    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s", force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_secrets,
            redact_values,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def logger() -> FilteringBoundLogger:
    return cast(FilteringBoundLogger, structlog.get_logger("radiacode"))
