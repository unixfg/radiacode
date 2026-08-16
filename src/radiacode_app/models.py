from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RawBatch:
    batch_id: UUID
    device_slug: str
    connection_id: UUID
    received_at: datetime
    payload: bytes
    sha256: bytes
    expected_sequence_before: int | None = None


@dataclass(frozen=True, slots=True)
class DecodedRecord:
    record_index: int
    sequence: int | None
    event_id: int | None
    group_id: int | None
    device_tick: int | None
    received_at: datetime
    sample_at: datetime | None
    timestamp_quality: str
    kind: str
    flags: int | None
    raw_record: bytes
    raw_payload: bytes
    values: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DecodeResult:
    records: tuple[DecodedRecord, ...]
    warnings: tuple[str, ...]
    next_expected_sequence: int | None
    truncated: bool
    unknown_tail: bool


@dataclass(frozen=True, slots=True)
class DeviceSpectrum:
    observed_at: datetime
    duration_seconds: int
    coefficients: tuple[float, float, float]
    counts: tuple[int, ...]
