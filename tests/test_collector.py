from __future__ import annotations

import struct
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import SecretStr

from radiacode_app.collector import Collector
from radiacode_app.database import DatabaseUnavailable
from radiacode_app.databuf import decode_data_buf
from radiacode_app.device import USBFailureClass
from radiacode_app.models import DecodeResult, DeviceSpectrum, RawBatch
from radiacode_app.mqtt import TelemetryEvent, TelemetryUpdate
from radiacode_app.settings import Settings
from radiacode_app.spectrum_state import SpectrumState, SpectrumTransition
from radiacode_app.spool import SQLiteSpool

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def realtime_payload(sequence: int = 10) -> bytes:
    return struct.pack("<BBBi", sequence, 0, 0, 1234) + struct.pack(
        "<ffHHHB",
        12.5,
        0.25,
        31,
        42,
        0x1234,
        0x56,
    )


def event_payload(sequence: int, event: int) -> bytes:
    return struct.pack("<BBBi", sequence, 0, 7, 1235) + struct.pack("<BBH", event, 0, 0)


class FakeDevice:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def read_data_buf_raw(self) -> bytes:
        self.reads += 1
        return self.payload

    def firmware_version(self) -> tuple[int, int]:
        return 4, 13

    def read_spectrum(self, *, observed_at: datetime | None = None) -> DeviceSpectrum:
        return DeviceSpectrum(observed_at or NOW, 0, (0.0, 1.0, 0.0), (0, 0, 0))

    def read_accumulated_spectrum(self, *, observed_at: datetime | None = None) -> DeviceSpectrum:
        return self.read_spectrum(observed_at=observed_at)


class FakeSink:
    def __init__(self) -> None:
        self.healthy = True
        self.fail_commits = 0
        self.commit_attempts: list[tuple[RawBatch, DecodeResult]] = []
        self.persisted_expected_sequence: int | None = None
        self.connection_opens: list[tuple[UUID, datetime, tuple[int, int] | None]] = []

    def healthcheck(self) -> bool:
        return self.healthy

    def ensure_device(self) -> UUID:
        return uuid4()

    def load_expected_sequence(self) -> int | None:
        return self.persisted_expected_sequence

    def record_connection_open(
        self,
        connection_id: UUID,
        connected_at: datetime,
        *,
        firmware: tuple[int, int] | None = None,
    ) -> None:
        self.connection_opens.append((connection_id, connected_at, firmware))

    def record_connection_close(
        self,
        connection_id: UUID,
        disconnected_at: datetime,
        reason: str,
    ) -> None:
        return None

    def commit_data_batch(self, batch: RawBatch, decoded: DecodeResult) -> bool:
        self.commit_attempts.append((batch, decoded))
        if self.fail_commits:
            self.fail_commits -= 1
            raise DatabaseUnavailable("simulated outage")
        self.persisted_expected_sequence = decoded.next_expected_sequence
        return True

    def commit_spectrum(
        self,
        observation: DeviceSpectrum,
        *,
        connection_id: UUID,
    ) -> SpectrumTransition:
        return SpectrumTransition(SpectrumState())

    def store_accumulated_snapshot(
        self,
        observation: DeviceSpectrum,
        *,
        connection_id: UUID,
        snapshot_kind: str,
    ) -> None:
        return None


class BackendUSBError(RuntimeError):
    def __init__(self, code: int) -> None:
        super().__init__("sanitized")
        self.backend_error_code = code


class FakeTelemetryPublisher:
    def __init__(self) -> None:
        self.updates: list[TelemetryUpdate] = []
        self.events: list[TelemetryEvent] = []

    def record(self, update: TelemetryUpdate) -> None:
        self.updates.append(update)

    def publish_event(self, event: TelemetryEvent) -> bool:
        self.events.append(event)
        return True


def settings() -> Settings:
    return Settings(
        device_slug="rc-test",
        device_serial=SecretStr("private-device-serial"),
        spool_reserved_batch_bytes=256,
    )


class CollectorTests(unittest.TestCase):
    def test_raw_batch_is_spooled_before_decode_and_acknowledged_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=1_000_000)
            sink = FakeSink()
            device = FakeDevice(realtime_payload())
            telemetry = FakeTelemetryPublisher()
            collector = Collector(
                settings(),
                sink,
                spool,
                utcnow=lambda: NOW,
                monotonic=lambda: 100.0,
                telemetry_publisher=telemetry,
            )
            observed_pending: list[int] = []

            def decode_after_spool(payload: bytes, received_at: datetime, **kwargs: Any) -> DecodeResult:
                observed_pending.append(spool.pending_count())
                return decode_data_buf(payload, received_at, **kwargs)

            with patch("radiacode_app.collector.decode_data_buf", side_effect=decode_after_spool):
                decoded = collector.poll_data_once(device, uuid4())
            self.assertIsNotNone(decoded)
            self.assertEqual(observed_pending, [1])
            self.assertEqual(spool.pending_count(), 0)
            self.assertEqual(device.reads, 1)
            self.assertEqual(len(sink.commit_attempts), 1)
            self.assertEqual(len(telemetry.updates), 1)
            self.assertEqual(telemetry.updates[0].cps, 12.5)
            self.assertEqual(telemetry.updates[0].observed_at, NOW)
            spool.close()

    def test_database_failure_replays_state_without_republishing_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=1_000_000)
            sink = FakeSink()
            sink.fail_commits = 1
            device = FakeDevice(realtime_payload(sequence=250) + event_payload(251, 7))
            telemetry = FakeTelemetryPublisher()
            collector = Collector(settings(), sink, spool, utcnow=lambda: NOW, telemetry_publisher=telemetry)
            with self.assertRaises(DatabaseUnavailable):
                collector.poll_data_once(device, uuid4())
            self.assertEqual(spool.pending_count(), 1)
            pending = spool.pending()[0]
            self.assertEqual(pending.attempts, 1)
            self.assertEqual(pending.last_error_class, "DatabaseUnavailable")
            self.assertTrue(collector.drain_spool())
            self.assertEqual(spool.pending_count(), 0)
            self.assertEqual(device.reads, 1)
            self.assertEqual(len(sink.commit_attempts), 2)
            self.assertEqual(sink.commit_attempts[0][0].batch_id, sink.commit_attempts[1][0].batch_id)
            self.assertEqual(len(telemetry.updates), 2)
            self.assertEqual(telemetry.updates[0].cps, 12.5)
            self.assertEqual(telemetry.updates[0].observed_at, NOW)
            self.assertTrue(telemetry.updates[1].charging)
            self.assertEqual(telemetry.updates[1].observed_at, NOW)
            self.assertEqual(telemetry.events, [])
            spool.close()

    def test_separate_data_buf_reads_do_not_share_sequence_expectations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=1_000_000)
            sink = FakeSink()
            device = FakeDevice(realtime_payload(sequence=10))
            collector = Collector(settings(), sink, spool, utcnow=lambda: NOW)

            first = collector.poll_data_once(device, uuid4())
            second = collector.poll_data_once(device, uuid4())

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None
            assert second is not None
            self.assertEqual(first.warnings, ())
            self.assertEqual(second.warnings, ())
            spool.close()

    def test_connection_persists_target_firmware_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=1_000_000)
            sink = FakeSink()
            device = FakeDevice(b"")
            stop = Event()

            original_record_open = sink.record_connection_open

            def record_open(
                connection_id: UUID,
                connected_at: datetime,
                *,
                firmware: tuple[int, int] | None = None,
            ) -> None:
                original_record_open(connection_id, connected_at, firmware=firmware)
                stop.set()

            sink.record_connection_open = record_open  # type: ignore[method-assign]
            collector = Collector(
                settings(),
                sink,
                spool,
                adapter_factory=lambda: device,
                utcnow=lambda: NOW,
            )

            collector.run(stop)

            self.assertEqual(len(sink.connection_opens), 1)
            self.assertEqual(sink.connection_opens[0][2], (4, 13))
            self.assertTrue(device.closed)
            spool.close()

    def test_unhealthy_database_prevents_destructive_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=1_000_000)
            sink = FakeSink()
            sink.healthy = False
            device = FakeDevice(realtime_payload())
            collector = Collector(settings(), sink, spool, utcnow=lambda: NOW)
            with self.assertRaises(DatabaseUnavailable):
                collector.poll_data_once(device, uuid4())
            self.assertEqual(device.reads, 0)
            self.assertEqual(spool.pending_count(), 0)
            spool.close()

    def test_only_active_no_device_failure_requests_reallocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=1_000_000)
            sink = FakeSink()
            markers: list[tuple[Path, dict[str, object]]] = []

            def marker(path: Path, **kwargs: object) -> None:
                markers.append((path, kwargs))

            collector = Collector(settings(), sink, spool, marker_writer=marker, pod_uid="pod-uid")
            connection_id = uuid4()
            self.assertFalse(collector._handle_usb_failure(BackendUSBError(-4), None))
            self.assertFalse(collector._handle_usb_failure(BackendUSBError(-3), connection_id))
            self.assertTrue(collector._handle_usb_failure(BackendUSBError(-4), connection_id))
            self.assertEqual(len(markers), 1)
            self.assertEqual(markers[0][1]["pod_uid"], "pod-uid")
            self.assertEqual(markers[0][1]["connection_id"], connection_id)
            self.assertEqual(USBFailureClass.NO_DEVICE.value, "no_device")
            spool.close()


if __name__ == "__main__":
    unittest.main()
