from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from .models import DeviceSpectrum
from .spectrum import (
    SpectrumEncodingError,
    add_counts_exact,
    calibration_fingerprint,
    encode_counts_uint32le,
)


@dataclass(frozen=True, slots=True)
class SpectrumCursor:
    session_id: UUID
    connection_id: UUID
    observed_at: datetime
    duration_seconds: int
    coefficients: tuple[float, float, float]
    calibration_fingerprint: bytes
    counts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FrameAccumulator:
    session_id: UUID
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    counts: tuple[int, ...]
    source_intervals: int
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpectrumFrame:
    frame_id: UUID
    session_id: UUID
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    counts: tuple[int, ...]
    source_intervals: int
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpectrumGap:
    session_id: UUID
    detected_at: datetime
    reason: str
    previous_duration_seconds: int
    observed_duration_seconds: int


@dataclass(frozen=True, slots=True)
class SpectrumState:
    cursor: SpectrumCursor | None = None
    accumulator: FrameAccumulator | None = None


@dataclass(frozen=True, slots=True)
class SpectrumTransition:
    state: SpectrumState
    started_session: UUID | None = None
    closed_session: UUID | None = None
    frame: SpectrumFrame | None = None
    gap: SpectrumGap | None = None
    warnings: tuple[str, ...] = ()


def _validate_observation(observation: DeviceSpectrum, expected_channel_count: int | None) -> bytes:
    if observation.observed_at.tzinfo is None or observation.observed_at.utcoffset() is None:
        raise ValueError("spectrum observed_at must be timezone-aware")
    if observation.duration_seconds < 0:
        raise ValueError("spectrum duration cannot be negative")
    encoded = encode_counts_uint32le(
        observation.counts,
        expected_channel_count=expected_channel_count,
    )
    return encoded


def _new_cursor(observation: DeviceSpectrum, connection_id: UUID, session_id: UUID) -> SpectrumCursor:
    fingerprint = calibration_fingerprint(len(observation.counts), observation.coefficients)
    return SpectrumCursor(
        session_id=session_id,
        connection_id=connection_id,
        observed_at=observation.observed_at,
        duration_seconds=observation.duration_seconds,
        coefficients=observation.coefficients,
        calibration_fingerprint=fingerprint,
        counts=observation.counts,
    )


def advance_spectrum_state(
    state: SpectrumState,
    observation: DeviceSpectrum,
    *,
    connection_id: UUID,
    expected_channel_count: int | None = None,
    frame_target_seconds: int = 300,
) -> SpectrumTransition:
    """Advance a cumulative spectrum without splitting any observed delta."""

    if frame_target_seconds < 1:
        raise ValueError("frame_target_seconds must be positive")
    _validate_observation(
        observation,
        expected_channel_count if state.cursor is None else None,
    )

    if state.cursor is None:
        session_id = uuid4()
        return SpectrumTransition(
            state=SpectrumState(cursor=_new_cursor(observation, connection_id, session_id)),
            started_session=session_id,
        )

    cursor = state.cursor
    observed_fingerprint = calibration_fingerprint(len(observation.counts), observation.coefficients)
    reason: str | None = None
    if len(observation.counts) != len(cursor.counts):
        reason = "channel_count_change"
    elif observed_fingerprint != cursor.calibration_fingerprint:
        reason = "calibration_change"
    elif observation.duration_seconds < cursor.duration_seconds:
        reason = "duration_regression"
    elif any(current < previous for current, previous in zip(observation.counts, cursor.counts, strict=True)):
        reason = "count_regression"

    if reason is not None:
        session_id = uuid4()
        gap = SpectrumGap(
            session_id=cursor.session_id,
            detected_at=observation.observed_at,
            reason=reason,
            previous_duration_seconds=cursor.duration_seconds,
            observed_duration_seconds=observation.duration_seconds,
        )
        partial_frame = None
        boundary_warnings: tuple[str, ...] = ()
        if state.accumulator is not None:
            partial_frame = SpectrumFrame(
                frame_id=uuid4(),
                session_id=state.accumulator.session_id,
                started_at=state.accumulator.started_at,
                ended_at=state.accumulator.ended_at,
                duration_seconds=state.accumulator.duration_seconds,
                counts=state.accumulator.counts,
                source_intervals=state.accumulator.source_intervals,
                quality_flags=tuple(
                    sorted({*state.accumulator.quality_flags, "partial_frame_on_session_boundary"})
                ),
            )
            boundary_warnings = ("partial_frame_on_session_boundary",)
        return SpectrumTransition(
            state=SpectrumState(cursor=_new_cursor(observation, connection_id, session_id)),
            started_session=session_id,
            closed_session=cursor.session_id,
            frame=partial_frame,
            gap=gap,
            warnings=boundary_warnings,
        )

    duration_delta = observation.duration_seconds - cursor.duration_seconds
    count_delta = tuple(
        current - previous for current, previous in zip(observation.counts, cursor.counts, strict=True)
    )
    new_cursor = _new_cursor(observation, connection_id, cursor.session_id)
    warnings: list[str] = []
    if connection_id != cursor.connection_id:
        warnings.append("session_continued_across_reconnect")
    anomaly_gap = None
    if duration_delta == 0 and any(count_delta):
        warnings.append("counts_changed_without_duration_change")
        anomaly_gap = SpectrumGap(
            session_id=cursor.session_id,
            detected_at=observation.observed_at,
            reason="counts_changed_without_duration_change",
            previous_duration_seconds=cursor.duration_seconds,
            observed_duration_seconds=observation.duration_seconds,
        )
    if duration_delta == 0 and not any(count_delta):
        return SpectrumTransition(
            state=SpectrumState(new_cursor, state.accumulator),
            warnings=tuple(warnings),
        )

    if state.accumulator is None:
        accumulator = FrameAccumulator(
            session_id=cursor.session_id,
            started_at=cursor.observed_at,
            ended_at=observation.observed_at,
            duration_seconds=duration_delta,
            counts=count_delta,
            source_intervals=1,
            quality_flags=tuple(sorted(warnings)),
        )
    else:
        if state.accumulator.session_id != cursor.session_id:
            raise ValueError("frame accumulator belongs to a different session")
        try:
            accumulated_counts = add_counts_exact((state.accumulator.counts, count_delta))
        except SpectrumEncodingError as error:
            raise SpectrumEncodingError("frame accumulator overflow") from error
        accumulator = FrameAccumulator(
            session_id=cursor.session_id,
            started_at=state.accumulator.started_at,
            ended_at=observation.observed_at,
            duration_seconds=state.accumulator.duration_seconds + duration_delta,
            counts=accumulated_counts,
            source_intervals=state.accumulator.source_intervals + 1,
            quality_flags=tuple(sorted({*state.accumulator.quality_flags, *warnings})),
        )

    if accumulator.duration_seconds >= frame_target_seconds:
        frame = SpectrumFrame(
            frame_id=uuid4(),
            session_id=accumulator.session_id,
            started_at=accumulator.started_at,
            ended_at=accumulator.ended_at,
            duration_seconds=accumulator.duration_seconds,
            counts=accumulator.counts,
            source_intervals=accumulator.source_intervals,
            quality_flags=accumulator.quality_flags,
        )
        return SpectrumTransition(
            state=SpectrumState(cursor=new_cursor, accumulator=None),
            frame=frame,
            gap=anomaly_gap,
            warnings=tuple(warnings),
        )
    return SpectrumTransition(
        state=SpectrumState(cursor=new_cursor, accumulator=accumulator),
        gap=anomaly_gap,
        warnings=tuple(warnings),
    )
