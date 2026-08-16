from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Final

from .models import DecodedRecord, DecodeResult

HEADER: Final[struct.Struct] = struct.Struct("<BBBi")
MAX_BATCH_ANCHOR_AGE: Final[timedelta] = timedelta(hours=24)
MAX_BATCH_FUTURE_SKEW: Final[timedelta] = timedelta(seconds=1)
# DATA_BUF reports exposure and exposure rate in R and R/h. The pinned
# radiacode 0.4.0 examples use this same factor for public µSv/µSv/h values.
# Raw protocol bytes remain available in every DecodedRecord for audit.
ROENTGEN_TO_MICROSIEVERT: Final[float] = 10_000.0

EVENT_NAMES: Final[dict[int, str]] = {
    0: "power_off",
    1: "power_on",
    2: "low_battery_shutdown",
    3: "change_device_params",
    4: "dose_reset",
    5: "user_event",
    6: "battery_empty_alarm",
    7: "charge_start",
    8: "charge_stop",
    9: "dose_rate_alarm_1",
    10: "dose_rate_alarm_2",
    11: "dose_rate_offscale",
    12: "dose_alarm_1",
    13: "dose_alarm_2",
    14: "dose_offscale",
    15: "temperature_too_low",
    16: "temperature_too_high",
    17: "text_message",
    18: "memory_snapshot",
    19: "spectrum_reset",
    20: "count_rate_alarm_1",
    21: "count_rate_alarm_2",
    22: "count_rate_offscale",
}


@dataclass(frozen=True, slots=True)
class _RecordType:
    kind: str
    body_format: str | None
    sample_width: int | None = None

    @property
    def fixed_size(self) -> int | None:
        return struct.calcsize(self.body_format) if self.body_format is not None else None


RECORD_TYPES: Final[dict[tuple[int, int], _RecordType]] = {
    (0, 0): _RecordType("real_time", "<ffHHHB"),
    (0, 1): _RecordType("raw", "<ff"),
    (0, 2): _RecordType("dose_rate_db", "<IffHH"),
    (0, 3): _RecordType("rare", "<IfHHH"),
    (0, 4): _RecordType("user", "<IffHH"),
    (0, 5): _RecordType("schedule", "<IffHH"),
    (0, 6): _RecordType("acceleration", "<HHH"),
    (0, 7): _RecordType("event", "<BBH"),
    (0, 8): _RecordType("raw_count_rate", "<fH"),
    (0, 9): _RecordType("raw_dose_rate", "<fH"),
    (1, 1): _RecordType("sample_8", None, 8),
    (1, 2): _RecordType("sample_16", None, 16),
    (1, 3): _RecordType("sample_14", None, 14),
}


def _sequence_warning(expected: int | None, observed: int) -> str | None:
    if expected is None or expected == observed:
        return None
    missing = (observed - expected) % 256
    return f"sequence_gap:expected={expected}:observed={observed}:distance={missing}"


def _decode_values(kind: str, body: bytes) -> tuple[dict[str, Any], int | None, tuple[str, ...]]:
    warnings: list[str] = []
    flags: int | None = None
    values: dict[str, Any]
    if kind == "real_time":
        count_rate, dose_rate, count_error, dose_error, flags, real_time_flags = struct.unpack(
            "<ffHHHB", body
        )
        values = {
            "count_rate": count_rate,
            "dose_rate": dose_rate * ROENTGEN_TO_MICROSIEVERT,
            "count_rate_error_pct": count_error / 10.0,
            "dose_rate_error_pct": dose_error / 10.0,
            "real_time_flags": real_time_flags,
        }
    elif kind == "raw":
        count_rate, dose_rate = struct.unpack("<ff", body)
        values = {
            "count_rate": count_rate,
            "dose_rate": dose_rate * ROENTGEN_TO_MICROSIEVERT,
        }
    elif kind in {"dose_rate_db", "user", "schedule"}:
        count, count_rate, dose_rate, dose_error, flags = struct.unpack("<IffHH", body)
        values = {
            "count": count,
            "count_rate": count_rate,
            "dose_rate": dose_rate * ROENTGEN_TO_MICROSIEVERT,
            "dose_rate_error_pct": dose_error / 10.0,
        }
    elif kind == "rare":
        duration, dose, temperature, charge_level, flags = struct.unpack("<IfHHH", body)
        values = {
            "duration_seconds": duration,
            "accumulated_dose": dose * ROENTGEN_TO_MICROSIEVERT,
            "temperature_c": (temperature - 2000) / 100.0,
            "charge_pct": charge_level / 100.0,
        }
    elif kind == "acceleration":
        x, y, z = struct.unpack("<HHH", body)
        values = {"x_raw": x, "y_raw": y, "z_raw": z}
    elif kind == "event":
        event, parameter, flags = struct.unpack("<BBH", body)
        values = {"event": event, "event_name": EVENT_NAMES.get(event), "parameter": parameter}
        if event not in EVENT_NAMES:
            warnings.append(f"unknown_event:{event}")
    elif kind == "raw_count_rate":
        count_rate, flags = struct.unpack("<fH", body)
        values = {"count_rate": count_rate}
    elif kind == "raw_dose_rate":
        dose_rate, flags = struct.unpack("<fH", body)
        values = {"dose_rate": dose_rate * ROENTGEN_TO_MICROSIEVERT}
    elif kind.startswith("sample_"):
        samples, sample_time_ms = struct.unpack_from("<HI", body)
        values = {"sample_count": samples, "sample_time_ms": sample_time_ms}
    else:  # pragma: no cover - guarded by the record table
        raise AssertionError(f"unhandled record kind {kind}")
    for name, value in tuple(values.items()):
        if isinstance(value, float) and not isfinite(value):
            values[name] = None
            warnings.append(f"non_finite_value:{name}")
    if kind == "real_time":
        values["valid"] = all(
            isinstance(values.get(name), (int, float)) and values[name] >= 0
            for name in ("count_rate", "dose_rate")
        )
        if not values["valid"]:
            warnings.append("invalid_real_time")
    elif kind == "rare":
        dose = values.get("accumulated_dose")
        temperature = values.get("temperature_c")
        charge = values.get("charge_pct")
        values["valid"] = (
            isinstance(dose, (int, float))
            and dose >= 0
            and isinstance(temperature, (int, float))
            and isinstance(charge, (int, float))
            and 0 <= charge <= 100
        )
        if not values["valid"]:
            warnings.append("invalid_status")
    return values, flags, tuple(warnings)


def _anchor_sample_times(records: list[DecodedRecord], received_at: datetime) -> list[DecodedRecord]:
    anchor_tick = next(
        (record.device_tick for record in reversed(records) if record.device_tick is not None),
        None,
    )
    if anchor_tick is None:
        return records
    anchored: list[DecodedRecord] = []
    for record in records:
        if record.device_tick is None:
            anchored.append(record)
            continue
        # Work in the uint32 ring while retaining the signed tick exactly as sent.
        delta_ticks = ((record.device_tick - anchor_tick + (1 << 31)) % (1 << 32)) - (1 << 31)
        delta = timedelta(milliseconds=delta_ticks * 10)
        if delta < -MAX_BATCH_ANCHOR_AGE or delta > MAX_BATCH_FUTURE_SKEW:
            anchored.append(
                replace(
                    record,
                    sample_at=None,
                    timestamp_quality="invalid_tick",
                    warnings=(*record.warnings, "device_tick_outside_batch_anchor_window"),
                )
            )
        else:
            anchored.append(
                replace(record, sample_at=received_at + delta, timestamp_quality="batch_relative")
            )
    return anchored


def decode_data_buf(
    payload: bytes,
    received_at: datetime,
    *,
    expected_sequence: int | None = None,
) -> DecodeResult:
    """Decode a v0.4.0 DATA_BUF payload without discarding undecodable bytes.

    The wire format has no length field for an unknown record type. Encountering
    one therefore preserves the complete remaining tail and halts; attempting to
    resynchronize would invent record boundaries.
    """

    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")

    position = 0
    records: list[DecodedRecord] = []
    global_warnings: list[str] = []
    truncated = False
    unknown_tail = False
    next_expected = expected_sequence

    while position < len(payload):
        start = position
        remaining = len(payload) - position
        if remaining < HEADER.size:
            warning = f"truncated_header:available={remaining}:expected={HEADER.size}"
            records.append(
                DecodedRecord(
                    record_index=len(records),
                    sequence=None,
                    event_id=None,
                    group_id=None,
                    device_tick=None,
                    received_at=received_at,
                    sample_at=None,
                    timestamp_quality="not_available",
                    kind="truncated_header",
                    flags=None,
                    raw_record=payload[position:],
                    raw_payload=payload[position:],
                    warnings=(warning,),
                )
            )
            global_warnings.append(warning)
            truncated = True
            break

        sequence, event_id, group_id, device_tick = HEADER.unpack_from(payload, position)
        position += HEADER.size
        record_warnings: list[str] = []
        if sequence_warning := _sequence_warning(next_expected, sequence):
            record_warnings.append(sequence_warning)
            global_warnings.append(sequence_warning)
        next_expected = (sequence + 1) % 256

        record_type = RECORD_TYPES.get((event_id, group_id))
        if record_type is None:
            warning = f"unknown_record_type:event={event_id}:group={group_id}"
            record_warnings.append(warning)
            global_warnings.append(warning)
            records.append(
                DecodedRecord(
                    record_index=len(records),
                    sequence=sequence,
                    event_id=event_id,
                    group_id=group_id,
                    device_tick=device_tick,
                    received_at=received_at,
                    sample_at=None,
                    timestamp_quality="not_available",
                    kind="unknown",
                    flags=None,
                    raw_record=payload[start:],
                    raw_payload=payload[position:],
                    warnings=tuple(record_warnings),
                )
            )
            unknown_tail = True
            break

        body_size = record_type.fixed_size
        if record_type.sample_width is not None:
            if len(payload) - position < 6:
                body_size = 6
            else:
                samples = struct.unpack_from("<H", payload, position)[0]
                body_size = 6 + samples * record_type.sample_width

        assert body_size is not None
        available = len(payload) - position
        if available < body_size:
            warning = (
                f"truncated_record:event={event_id}:group={group_id}:"
                f"available={available}:expected={body_size}"
            )
            record_warnings.append(warning)
            global_warnings.append(warning)
            records.append(
                DecodedRecord(
                    record_index=len(records),
                    sequence=sequence,
                    event_id=event_id,
                    group_id=group_id,
                    device_tick=device_tick,
                    received_at=received_at,
                    sample_at=None,
                    timestamp_quality="not_available",
                    kind="truncated_record",
                    flags=None,
                    raw_record=payload[start:],
                    raw_payload=payload[position:],
                    values={"expected_kind": record_type.kind, "expected_body_size": body_size},
                    warnings=tuple(record_warnings),
                )
            )
            truncated = True
            break

        body_end = position + body_size
        body = payload[position:body_end]
        values, flags, value_warnings = _decode_values(record_type.kind, body)
        record_warnings.extend(value_warnings)
        global_warnings.extend(value_warnings)
        records.append(
            DecodedRecord(
                record_index=len(records),
                sequence=sequence,
                event_id=event_id,
                group_id=group_id,
                device_tick=device_tick,
                received_at=received_at,
                sample_at=None,
                timestamp_quality="not_available",
                kind=record_type.kind,
                flags=flags,
                raw_record=payload[start:body_end],
                raw_payload=body,
                values=values,
                warnings=tuple(record_warnings),
            )
        )
        position = body_end

    records = _anchor_sample_times(records, received_at)
    # Anchoring can add per-record warnings, so reflect those in batch warnings.
    for record in records:
        for warning in record.warnings:
            if warning not in global_warnings:
                global_warnings.append(warning)
    return DecodeResult(
        records=tuple(records),
        warnings=tuple(global_warnings),
        next_expected_sequence=next_expected,
        truncated=truncated,
        unknown_tail=unknown_tail,
    )
