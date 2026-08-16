from __future__ import annotations

from ..models import DecodedRecord
from .models import TelemetryEvent, TelemetryUpdate

type MqttMessage = TelemetryUpdate | TelemetryEvent


def messages_for_record(record: DecodedRecord) -> tuple[MqttMessage, ...]:
    """Translate one persisted decoder record into public MQTT telemetry.

    Host ``received_at`` is intentionally used instead of the inferred device
    sample timestamp. Unknown, truncated, and raw-only records are not MQTT
    concerns and produce no message.
    """

    values = record.values
    if record.kind == "real_time" and values.get("valid") is True:
        return (
            TelemetryUpdate(
                observed_at=record.received_at,
                realtime_valid=True,
                cps=float(values["count_rate"]),
                cps_uncertainty_pct=(
                    float(values["count_rate_error_pct"])
                    if values.get("count_rate_error_pct") is not None
                    else None
                ),
                dose_rate_usv_h=float(values["dose_rate"]),
                dose_rate_uncertainty_pct=(
                    float(values["dose_rate_error_pct"])
                    if values.get("dose_rate_error_pct") is not None
                    else None
                ),
            ),
        )
    if record.kind == "rare" and values.get("valid") is True:
        return (
            TelemetryUpdate(
                observed_at=record.received_at,
                realtime_valid=False,
                accumulated_dose_usv=float(values["accumulated_dose"]),
                accumulated_duration_seconds=int(values["duration_seconds"]),
                temperature_c=float(values["temperature_c"]),
                battery_percent=float(values["charge_pct"]),
            ),
        )
    if record.kind != "event":
        return ()

    event_code = int(values["event"])
    event_name = values.get("event_name")
    event_kind = str(event_name) if event_name else "unknown_device_event"
    parameter = int(values["parameter"])
    result: list[MqttMessage] = [
        TelemetryEvent(
            kind=event_kind,
            observed_at=record.received_at,
            details={"code": event_code, "parameter": parameter},
        )
    ]
    if event_kind in {"charge_start", "charge_stop"}:
        result.append(
            TelemetryUpdate(
                observed_at=record.received_at,
                realtime_valid=False,
                charging=event_kind == "charge_start",
            )
        )
    return tuple(result)
