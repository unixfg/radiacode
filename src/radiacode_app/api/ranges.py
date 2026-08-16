from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta


class PublicRequestError(ValueError):
    pass


SPECTRUM_FRAME_TARGET_SECONDS = 300


def bounded_utc_range(
    start: datetime,
    end: datetime,
    *,
    max_days: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    if start.tzinfo is None or start.utcoffset() is None:
        raise PublicRequestError("start must include a timezone")
    if end.tzinfo is None or end.utcoffset() is None:
        raise PublicRequestError("end must include a timezone")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if end_utc <= start_utc:
        raise PublicRequestError("end must be later than start")
    if end_utc - start_utc > timedelta(days=max_days):
        raise PublicRequestError(f"range must not exceed {max_days} days")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if end_utc > current + timedelta(minutes=5):
        raise PublicRequestError("end is too far in the future")
    return start_utc, end_utc


def resolution_seconds(start: datetime, end: datetime, max_points: int, *, minimum: int = 1) -> int:
    if not 1 <= max_points <= 2_000:
        raise PublicRequestError("max_points must be between 1 and 2000")
    seconds = max(1.0, (end - start).total_seconds())
    raw = max(minimum, math.ceil(seconds / max_points))
    # Predictable human-friendly server resolutions make chart caching stable.
    for candidate in (1, 5, 10, 30, 60, 300, 900, 3600, 21_600, 86_400, 604_800):
        if candidate >= raw:
            return candidate
    return math.ceil(raw / 604_800) * 604_800


def spectrum_resolution(start: datetime, end: datetime, requested_time_bins: int) -> str:
    if requested_time_bins < 1:
        raise PublicRequestError("spectrum source budget must be positive")
    span_seconds = max(1.0, (end - start).total_seconds())
    if math.ceil(span_seconds / SPECTRUM_FRAME_TARGET_SECONDS) <= requested_time_bins:
        return "frame"
    if math.ceil(span_seconds / 3_600) <= requested_time_bins:
        return "hour"
    return "day"
