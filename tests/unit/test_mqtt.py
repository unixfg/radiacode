from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from radiacode_app.models import DecodedRecord
from radiacode_app.mqtt import (
    MqttConfig,
    MqttPublisher,
    TelemetryEvent,
    TelemetryUpdate,
    messages_for_record,
)


class FakeResult:
    def __init__(self, client: FakeClient) -> None:
        self.rc = 0
        self.client = client
        self.waited: list[float | None] = []
        self.published = True

    def wait_for_publish(self, timeout: float | None = None) -> None:
        self.waited.append(timeout)
        self.client.operations.append(("wait_for_publish", timeout))

    def is_published(self) -> bool:
        return self.published


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, int, bool]] = []
        self.connected: tuple[str, int, int] | None = None
        self.on_connect: Any = None
        self.on_connect_fail: Any = None
        self.on_disconnect: Any = None
        self.operations: list[tuple[str, object]] = []
        self.results: list[FakeResult] = []

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.connected = (host, port, keepalive)

    def loop_start(self) -> None:
        self.operations.append(("loop_start", None))
        return None

    def loop_stop(self) -> None:
        self.operations.append(("loop_stop", None))
        return None

    def disconnect(self) -> None:
        self.operations.append(("disconnect", None))
        return None

    def will_set(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.will = (topic, payload, qos, retain)

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> FakeResult:
        self.messages.append((topic, payload, qos, retain))
        self.operations.append(("publish", topic))
        result = FakeResult(self)
        self.results.append(result)
        return result

    def trigger_connect(self, reason_code: object = 0) -> None:
        assert self.on_connect is not None
        self.on_connect(self, None, object(), reason_code, None)

    def trigger_disconnect(self, reason_code: object = 0) -> None:
        assert self.on_disconnect is not None
        self.on_disconnect(self, None, object(), reason_code, None)

    def trigger_connect_fail(self) -> None:
        assert self.on_connect_fail is not None
        self.on_connect_fail(self, None)


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def config() -> MqttConfig:
    return MqttConfig(
        host="mqtt.internal",
        username="radiacode-publisher",
        password="secret-from-runtime",
        ca_file=Path("/run/secrets/mqtt/ca.crt"),
    )


def publisher(client: FakeClient | None = None) -> tuple[MqttPublisher, FakeClient]:
    fake = client or FakeClient()
    return (
        MqttPublisher(
            config(),
            "rc-110",
            "RadiaCode RC-110",
            fake,
            device_model="RC-110",
        ),
        fake,
    )


def valid_update(at: datetime = NOW) -> TelemetryUpdate:
    return TelemetryUpdate(
        observed_at=at,
        cps=12.5,
        cps_uncertainty_pct=0.4,
        dose_rate_usv_h=0.08,
        dose_rate_uncertainty_pct=0.01,
        accumulated_dose_usv=4.2,
        accumulated_duration_seconds=3600,
        temperature_c=22.5,
        battery_percent=87,
        charging=True,
    )


def test_state_and_availability_are_retained_qos_one() -> None:
    adapter, fake = publisher()
    adapter.record(valid_update())

    assert adapter.publish_cycle(NOW + timedelta(seconds=5))

    state = next(message for message in fake.messages if message[0].endswith("/state"))
    availability = next(message for message in fake.messages if message[0].endswith("/availability"))
    parsed = json.loads(state[1])
    assert state[2:] == (1, True)
    assert availability == ("radiacode/rc-110/availability", "online", 1, True)
    assert parsed["connected"] is True
    assert parsed["dose_rate_usv_h"] == 0.08
    assert parsed["temperature_c_observed_at"] == "2026-08-16T12:00:00Z"


def test_invalid_realtime_does_not_replace_live_values_and_cached_slow_values_remain() -> None:
    adapter, fake = publisher()
    adapter.record(valid_update())
    adapter.record(
        TelemetryUpdate(
            observed_at=NOW + timedelta(seconds=8),
            realtime_valid=False,
            cps=0,
            dose_rate_usv_h=0,
            battery_percent=86,
        )
    )

    adapter.publish_cycle(NOW + timedelta(seconds=11))

    state = json.loads(next(message[1] for message in fake.messages if message[0].endswith("/state")))
    assert state["connected"] is False
    assert state["cps"] == 12.5
    assert state["cps_observed_at"] == "2026-08-16T12:00:00Z"
    assert state["battery_percent"] == 86
    assert state["battery_percent_observed_at"] == "2026-08-16T12:00:08Z"


def test_event_is_non_retained_and_never_uses_control_or_spectrum_topics() -> None:
    adapter, fake = publisher()

    assert adapter.publish_event(TelemetryEvent("spectrum_gap", NOW, details={"duration_seconds": 60}))

    assert fake.messages == [
        (
            "radiacode/rc-110/event",
            '{"details":{"duration_seconds":60},"device":"rc-110","kind":"spectrum_gap",'
            '"observed_at":"2026-08-16T12:00:00Z"}',
            1,
            False,
        )
    ]
    assert all("spectrum" not in topic and "control" not in topic for topic, *_ in fake.messages)


def test_discovery_creates_all_entities_as_one_home_assistant_device() -> None:
    adapter, fake = publisher()

    assert adapter.publish_discovery()

    assert len(fake.messages) == 10
    configs = [json.loads(payload) for _, payload, qos, retained in fake.messages if qos == 1 and retained]
    assert {item["unique_id"].rsplit("_", 1)[-1] for item in configs} >= {"cps", "charging", "connected"}
    assert {tuple(item["device"]["identifiers"]) for item in configs} == {("radiacode_rc-110",)}
    assert {item["device"]["model"] for item in configs} == {"RC-110"}
    assert all(topic.startswith("homeassistant/") for topic, *_ in fake.messages)
    assert all("serial" not in payload.lower() for _, payload, *_ in fake.messages)
    uncertainty_configs = [item for item in configs if "uncertainty" in item["unique_id"]]
    assert {item["unit_of_measurement"] for item in uncertainty_configs} == {"%"}


def test_publish_failure_is_reported_but_not_raised() -> None:
    class BrokenClient(FakeClient):
        def publish(self, topic: str, payload: str, qos: int, retain: bool) -> Any:
            raise OSError("broker unavailable")

    errors: list[tuple[str, Exception | None]] = []
    adapter = MqttPublisher(
        config(),
        "rc-110",
        "RadiaCode RC-110",
        BrokenClient(),
        on_error=lambda operation, error: errors.append((operation, error)),
    )
    adapter.record(valid_update())

    assert adapter.publish_cycle(NOW) is False
    assert len(errors) == 2
    assert all(isinstance(error, OSError) for _, error in errors)


def test_valid_realtime_requires_principal_readings() -> None:
    adapter, _ = publisher()
    with pytest.raises(ValueError, match="requires cps"):
        adapter.record(TelemetryUpdate(observed_at=NOW, cps=3.0))


def test_start_sets_retained_offline_last_will() -> None:
    adapter, fake = publisher()

    assert adapter.start()

    assert fake.will == ("radiacode/rc-110/availability", "offline", 1, True)
    assert fake.connected == ("mqtt.internal", 8883, 60)
    assert fake.messages == []


def test_successful_connect_and_reconnect_republish_discovery_and_current_state() -> None:
    adapter, fake = publisher()
    adapter.record(valid_update())
    assert adapter.start()
    assert adapter.publish_cycle(NOW + timedelta(seconds=1)) is False
    assert fake.messages == []

    fake.trigger_connect()

    assert len([message for message in fake.messages if message[0].startswith("homeassistant/")]) == 10
    assert len([message for message in fake.messages if message[0].endswith("/state")]) == 1
    assert len([message for message in fake.messages if message[0].endswith("/availability")]) == 1

    fake.trigger_disconnect()
    messages_before_reconnect = len(fake.messages)
    adapter.record(valid_update(NOW + timedelta(seconds=20)))
    assert adapter.publish_cycle(NOW + timedelta(seconds=21)) is False
    assert len(fake.messages) == messages_before_reconnect
    fake.trigger_connect()

    assert len(fake.messages) == messages_before_reconnect + 12
    latest_state = json.loads(
        next(message[1] for message in reversed(fake.messages) if message[0].endswith("/state"))
    )
    assert latest_state["cps_observed_at"] == "2026-08-16T12:00:20Z"


def test_failed_connack_and_unexpected_disconnect_are_reported_without_raising() -> None:
    errors: list[tuple[str, Exception | None]] = []
    fake = FakeClient()
    adapter = MqttPublisher(
        config(),
        "rc-110",
        "RadiaCode RC-110",
        fake,
        on_error=lambda operation, error: errors.append((operation, error)),
    )
    assert adapter.start()

    class FailedReason:
        is_failure = True

    fake.trigger_connect(FailedReason())
    fake.trigger_connect_fail()
    fake.trigger_disconnect()

    assert errors == [("connack", None), ("connect_fail", None), ("disconnect", None)]
    assert fake.messages == []


def test_close_waits_boundedly_for_offline_ack_before_graceful_disconnect() -> None:
    errors: list[tuple[str, Exception | None]] = []
    fake = FakeClient()
    adapter = MqttPublisher(
        config(),
        "rc-110",
        "RadiaCode RC-110",
        fake,
        on_error=lambda operation, error: errors.append((operation, error)),
    )
    assert adapter.start()
    fake.trigger_connect()
    fake.operations.clear()
    fake.messages.clear()
    fake.results.clear()

    adapter.close()
    fake.trigger_disconnect()

    assert fake.messages == [("radiacode/rc-110/availability", "offline", 1, True)]
    assert fake.results[0].waited == [2.0]
    assert fake.operations == [
        ("publish", "radiacode/rc-110/availability"),
        ("wait_for_publish", 2.0),
        ("disconnect", None),
        ("loop_stop", None),
    ]
    assert errors == []


def test_close_reports_offline_ack_timeout_and_falls_back_to_last_will() -> None:
    class TimeoutResult(FakeResult):
        def wait_for_publish(self, timeout: float | None = None) -> None:
            super().wait_for_publish(timeout)
            self.published = False

    class TimeoutClient(FakeClient):
        def publish(self, topic: str, payload: str, qos: int, retain: bool) -> FakeResult:
            self.messages.append((topic, payload, qos, retain))
            self.operations.append(("publish", topic))
            result = TimeoutResult(self)
            self.results.append(result)
            return result

    errors: list[tuple[str, Exception | None]] = []
    fake = TimeoutClient()
    adapter = MqttPublisher(
        config(),
        "rc-110",
        "RadiaCode RC-110",
        fake,
        on_error=lambda operation, error: errors.append((operation, error)),
    )
    assert adapter.start()
    fake.trigger_connect()
    fake.operations.clear()
    fake.messages.clear()
    fake.results.clear()

    adapter.close()

    assert errors == [("publish_timeout:radiacode/rc-110/availability", None)]
    assert ("disconnect", None) not in fake.operations
    assert ("loop_stop", None) in fake.operations


def test_decoder_bridge_uses_host_time_and_preserves_slow_cache() -> None:
    realtime = DecodedRecord(
        record_index=0,
        sequence=1,
        event_id=0,
        group_id=0,
        device_tick=-12,
        received_at=NOW,
        sample_at=NOW - timedelta(seconds=2),
        timestamp_quality="batch_relative",
        kind="real_time",
        flags=0,
        raw_record=b"",
        raw_payload=b"",
        values={
            "valid": True,
            "count_rate": 12.5,
            "count_rate_error_pct": 3.2,
            "dose_rate": 0.08,
            "dose_rate_error_pct": 4.1,
        },
    )
    rare = DecodedRecord(
        record_index=1,
        sequence=2,
        event_id=0,
        group_id=3,
        device_tick=-11,
        received_at=NOW,
        sample_at=None,
        timestamp_quality="invalid_tick",
        kind="rare",
        flags=0,
        raw_record=b"",
        raw_payload=b"",
        values={
            "valid": True,
            "accumulated_dose": 4.2,
            "duration_seconds": 3600,
            "temperature_c": 22.5,
            "charge_pct": 87,
        },
    )

    realtime_message = messages_for_record(realtime)[0]
    slow_message = messages_for_record(rare)[0]

    assert isinstance(realtime_message, TelemetryUpdate)
    assert realtime_message.observed_at == NOW
    assert realtime_message.cps_uncertainty_pct == 3.2
    assert isinstance(slow_message, TelemetryUpdate)
    assert slow_message.realtime_valid is False
    assert slow_message.battery_percent == 87


def test_decoder_bridge_ignores_invalid_numeric_records() -> None:
    invalid = DecodedRecord(
        record_index=0,
        sequence=1,
        event_id=0,
        group_id=0,
        device_tick=1,
        received_at=NOW,
        sample_at=None,
        timestamp_quality="not_available",
        kind="real_time",
        flags=0,
        raw_record=b"",
        raw_payload=b"",
        values={"valid": False, "count_rate": None, "dose_rate": None},
    )

    assert messages_for_record(invalid) == ()
