from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceSummary(PublicModel):
    slug: str
    name: str
    model: str
    firmware_version: str | None
    available: bool
    last_seen_at: datetime | None


class DevicesResponse(PublicModel):
    devices: list[DeviceSummary]


class CurrentState(PublicModel):
    device: str
    received_at: datetime | None
    available: bool
    cps: float | None
    dose_rate: float | None
    cps_uncertainty_pct: float | None
    dose_rate_uncertainty_pct: float | None
    accumulated_dose: float | None
    accumulated_duration_seconds: int | None
    temperature_c: float | None
    battery_pct: float | None
    charging: bool | None
    field_timestamps: dict[str, datetime]


class CurrentStatesResponse(PublicModel):
    states: list[CurrentState]


class ScalarValues(PublicModel):
    min: float
    max: float
    avg: float
    latest: float


class ScalarPoint(PublicModel):
    at: datetime
    cps: ScalarValues | None
    dose_rate: ScalarValues | None


class ScalarHistoryResponse(PublicModel):
    device: str
    start: datetime
    end: datetime
    resolution_seconds: int
    points: list[ScalarPoint]


class PublicEvent(PublicModel):
    at: datetime
    code: str
    name: str
    parameter: str | int | float | bool | None


class EventsResponse(PublicModel):
    events: list[PublicEvent]


class Calibration(PublicModel):
    a0: float
    a1: float
    a2: float


class SpectrumSeries(PublicModel):
    epoch_started_at: datetime
    start: datetime
    end: datetime
    duration_seconds: int
    channel_count: int
    calibration: Calibration
    counts: list[int]
    overflow_count: int
    quality_flags: list[str]
    rebinned: bool = False


class SpectrumResponse(PublicModel):
    device: str
    spectra: list[SpectrumSeries]
    rebinned: bool = False


class RebinnedSeries(PublicModel):
    device: str
    counts: list[float]
    source_total: int
    coverage: float


class SpectrumComparisonResponse(PublicModel):
    energy_edges_kev: list[float]
    series: list[RebinnedSeries]
    rebinned: bool = True


class SpectrogramResponse(PublicModel):
    device: str
    time_edges: list[datetime]
    energy_edges_kev: list[float]
    counts: list[list[float]]
    source_resolution: str
    rebinned: bool = True
