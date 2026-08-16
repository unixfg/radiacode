from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

type JsonScalar = str | int | float | bool

DEVICE_SLUG: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
REALTIME_FIELDS: Final = (
    "cps",
    "cps_uncertainty_pct",
    "dose_rate_usv_h",
    "dose_rate_uncertainty_pct",
)
SLOW_FIELDS: Final = (
    "accumulated_dose_usv",
    "accumulated_duration_seconds",
    "temperature_c",
    "battery_percent",
    "charging",
)


def validate_device_slug(value: str) -> str:
    if DEVICE_SLUG.fullmatch(value) is None:
        raise ValueError("device slug must be lowercase ASCII letters, numbers, and hyphens")
    return value


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_numeric(name: str, value: float | int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    if name != "temperature_c" and value < 0:
        raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class TelemetryUpdate:
    """A public-safe observation prepared by the collector.

    Invalid realtime samples may still carry slow-changing device attributes.
    They do not advance the availability clock or replace the last valid CPS and
    dose values.
    """

    observed_at: datetime
    realtime_valid: bool = True
    cps: float | None = None
    cps_uncertainty_pct: float | None = None
    dose_rate_usv_h: float | None = None
    dose_rate_uncertainty_pct: float | None = None
    accumulated_dose_usv: float | None = None
    accumulated_duration_seconds: int | None = None
    temperature_c: float | None = None
    battery_percent: float | None = None
    charging: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at, "observed_at"))
        for name in (*REALTIME_FIELDS, *SLOW_FIELDS[:-1]):
            _validate_numeric(name, getattr(self, name))
        if self.battery_percent is not None and self.battery_percent > 100:
            raise ValueError("battery_percent cannot exceed 100")


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    kind: str
    observed_at: datetime
    message: str | None = None
    details: dict[str, JsonScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.kind) is None:
            raise ValueError("event kind must be a public-safe snake_case identifier")
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at, "observed_at"))
        if self.message is not None and len(self.message) > 240:
            raise ValueError("event message must not exceed 240 characters")
        for key, value in self.details.items():
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None:
                raise ValueError("event detail keys must be public-safe snake_case identifiers")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("event detail numbers must be finite")


@dataclass(frozen=True, slots=True)
class CachedValue:
    value: JsonScalar
    observed_at: datetime


@dataclass(slots=True)
class DeviceStateCache:
    """Timestamped cache used to avoid publishing missing values as zero."""

    device_slug: str
    _values: dict[str, CachedValue] = field(default_factory=dict, init=False)
    _last_valid_realtime_at: datetime | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.device_slug = validate_device_slug(self.device_slug)

    @property
    def last_valid_realtime_at(self) -> datetime | None:
        return self._last_valid_realtime_at

    def apply(self, update: TelemetryUpdate) -> None:
        if update.realtime_valid:
            realtime_values = [getattr(update, name) for name in REALTIME_FIELDS]
            # A validity heartbeat must include the two principal live readings;
            # uncertainty fields may legitimately be unavailable temporarily.
            if update.cps is None or update.dose_rate_usv_h is None:
                raise ValueError("a valid realtime update requires cps and dose_rate_usv_h")
            self._last_valid_realtime_at = update.observed_at
            for name, value in zip(REALTIME_FIELDS, realtime_values, strict=True):
                if value is not None:
                    self._values[name] = CachedValue(value, update.observed_at)

        for name in SLOW_FIELDS:
            value = getattr(update, name)
            if value is not None:
                self._values[name] = CachedValue(value, update.observed_at)

    def is_connected(self, now: datetime, stale_after: timedelta) -> bool:
        checked_at = _aware_utc(now, "now")
        return (
            self._last_valid_realtime_at is not None
            and checked_at - self._last_valid_realtime_at <= stale_after
        )

    def snapshot(self, now: datetime, stale_after: timedelta) -> dict[str, JsonScalar]:
        checked_at = _aware_utc(now, "now")
        result: dict[str, JsonScalar] = {
            "device": self.device_slug,
            "connected": self.is_connected(checked_at, stale_after),
            "published_at": checked_at.isoformat().replace("+00:00", "Z"),
        }
        if self._last_valid_realtime_at is not None:
            result["realtime_observed_at"] = self._last_valid_realtime_at.isoformat().replace("+00:00", "Z")
        for name, cached in self._values.items():
            result[name] = cached.value
            result[f"{name}_observed_at"] = cached.observed_at.isoformat().replace("+00:00", "Z")
        return result
