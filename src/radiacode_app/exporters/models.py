"""Transport-neutral spectrum export model.

This model intentionally has no database identifier or hardware serial field.  All
exporters consume the same public-safe representation so a future format cannot
accidentally disclose internal identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite

UINT64_MAX = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class ExportSpectrum:
    """A single detector/calibration-epoch spectrum or exposure frame."""

    device_slug: str
    device_model: str
    calibration_key: str
    start_at: datetime
    end_at: datetime
    duration_seconds: float
    counts: tuple[int, ...]
    calibration: tuple[float, float, float]
    title: str = "Radiation spectrum"
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    rebinned: bool = False

    def __post_init__(self) -> None:
        if not self.device_slug or not self.device_slug.replace("-", "").replace("_", "").isalnum():
            raise ValueError("device_slug must be a non-empty public slug")
        if not self.device_model:
            raise ValueError("device_model is required")
        if not self.calibration_key:
            raise ValueError("calibration_key is required")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("export timestamps must be timezone-aware")
        if self.end_at < self.start_at:
            raise ValueError("end_at precedes start_at")
        if not isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive and finite")
        if len(self.counts) < 2:
            raise ValueError("a spectrum must contain data channels plus the final metadata channel")
        # Stored frames are uint32, but an exact aggregate may legitimately
        # exceed uint32 after many frames are added. Interchange models retain
        # those counts without narrowing them.
        if any(not isinstance(value, int) or value < 0 or value > UINT64_MAX for value in self.counts):
            raise ValueError("export counts must be unsigned 64-bit integers")
        if len(self.calibration) != 3 or any(not isfinite(value) for value in self.calibration):
            raise ValueError("a finite quadratic calibration is required")

    @property
    def channel_count(self) -> int:
        return len(self.counts)

    @property
    def valid_count(self) -> int:
        """Counts in calibrated channels, excluding RadiaCode metadata/overflow."""

        return sum(self.counts[:-1])

    @property
    def total_count(self) -> int:
        return sum(self.counts)

    @property
    def overflow_count(self) -> int:
        return self.counts[-1]

    @property
    def content_sha256(self) -> str:
        digest = sha256()
        for value in self.counts:
            digest.update(value.to_bytes(8, byteorder="little", signed=False))
        return digest.hexdigest()

    @property
    def real_time_seconds(self) -> float:
        """Wall elapsed time, never shorter than the summed live exposure."""

        wall_elapsed = (self.utc_end() - self.utc_start()).total_seconds()
        return max(self.duration_seconds, wall_elapsed)

    def energy_kev(self, channel: int) -> float:
        if channel < 0 or channel >= self.channel_count - 1:
            raise ValueError("the final channel is metadata and has no calibrated energy")
        a0, a1, a2 = self.calibration
        return a0 + a1 * channel + a2 * channel * channel

    def utc_start(self) -> datetime:
        return self.start_at.astimezone(UTC)

    def utc_end(self) -> datetime:
        return self.end_at.astimezone(UTC)
