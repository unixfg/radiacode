from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from threading import Event
from typing import Protocol
from uuid import UUID, uuid4

from . import __version__
from .database import DatabaseUnavailable, PostgresSink
from .databuf import decode_data_buf
from .device import RadiaCodeAdapter, USBFailureClass, classify_usb_error
from .logging import logger
from .metrics import (
    ACCUMULATED_SNAPSHOT_FAILURES,
    DATA_BATCHES,
    DATABASE_FAILURES,
    DECODE_WARNINGS,
    DETECTOR_AVAILABLE,
    LAST_VALID_REALTIME,
    MQTT_FAILURES,
    SPECTRUM_GAPS,
    SPOOL_BATCHES,
    SPOOL_BYTES,
    USB_ERRORS,
    set_collector_state,
)
from .models import DecodedRecord, DecodeResult, DeviceSpectrum, RawBatch
from .mqtt import TelemetryEvent, TelemetryUpdate, messages_for_record
from .reallocator import write_reallocation_marker
from .settings import Settings
from .spectrum import spectrum_sha256
from .spectrum_state import SpectrumTransition
from .spool import SpoolCapacityError, SQLiteSpool


class Sink(Protocol):
    def healthcheck(self) -> bool: ...

    def ensure_device(self) -> UUID: ...

    def load_expected_sequence(self) -> int | None: ...

    def record_connection_open(
        self, connection_id: UUID, connected_at: datetime, *, firmware: tuple[int, int] | None = None
    ) -> None: ...

    def record_connection_close(
        self,
        connection_id: UUID,
        disconnected_at: datetime,
        reason: str,
    ) -> None: ...

    def commit_data_batch(self, batch: RawBatch, decoded: DecodeResult) -> bool: ...

    def commit_spectrum(self, observation: DeviceSpectrum, *, connection_id: UUID) -> SpectrumTransition: ...

    def store_accumulated_snapshot(
        self,
        observation: DeviceSpectrum,
        *,
        connection_id: UUID,
        snapshot_kind: str,
    ) -> None: ...


class Device(Protocol):
    def connect(self) -> None: ...

    def close(self) -> None: ...

    def read_data_buf_raw(self) -> bytes: ...

    def read_spectrum(self, *, observed_at: datetime | None = None) -> DeviceSpectrum: ...

    def read_accumulated_spectrum(self, *, observed_at: datetime | None = None) -> DeviceSpectrum: ...


class TelemetryPublisher(Protocol):
    def record(self, update: TelemetryUpdate) -> None: ...

    def publish_event(self, event: TelemetryEvent) -> bool: ...


class Collector:
    def __init__(
        self,
        settings: Settings,
        sink: Sink,
        spool: SQLiteSpool,
        *,
        adapter_factory: Callable[[], Device] | None = None,
        utcnow: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        marker_writer: Callable[..., None] = write_reallocation_marker,
        pod_uid: str | None = None,
        telemetry_publisher: TelemetryPublisher | None = None,
    ) -> None:
        self.settings = settings
        self.device_slug = settings.require_device_slug()
        self.sink = sink
        self.spool = spool
        serial = settings.require_device_serial()
        self.adapter_factory = adapter_factory or (lambda: RadiaCodeAdapter(serial))
        self.utcnow = utcnow or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        self.marker_writer = marker_writer
        self.pod_uid = pod_uid or os.environ.get("POD_UID", "")
        self.telemetry_publisher = telemetry_publisher
        self.expected_sequence: int | None = None
        self.last_valid_realtime_monotonic: float | None = None
        self._log = logger().bind(device=self.device_slug)

    def _update_spool_metrics(self) -> None:
        SPOOL_BYTES.labels(self.device_slug).set(self.spool.payload_bytes())
        SPOOL_BATCHES.labels(self.device_slug).set(self.spool.pending_count())

    def _refresh_availability(self) -> None:
        if (
            self.last_valid_realtime_monotonic is None
            or self.monotonic() - self.last_valid_realtime_monotonic > self.settings.mqtt_stale_after_seconds
        ):
            DETECTOR_AVAILABLE.labels(self.device_slug).set(0)

    def _forward_mqtt_record(self, record: DecodedRecord) -> None:
        if self.telemetry_publisher is None:
            return
        try:
            for message in messages_for_record(record):
                if isinstance(message, TelemetryUpdate):
                    self.telemetry_publisher.record(message)
                else:
                    self.telemetry_publisher.publish_event(message)
        except Exception as error:
            MQTT_FAILURES.labels(self.device_slug).inc()
            self._log.warning("mqtt_record_rejected", error_class=type(error).__name__)

    def _forward_replayed_mqtt_record(self, record: DecodedRecord) -> None:
        """Refresh cached telemetry from durable history without replaying events."""

        if self.telemetry_publisher is None:
            return
        try:
            for message in messages_for_record(record):
                if isinstance(message, TelemetryUpdate):
                    self.telemetry_publisher.record(message)
        except Exception as error:
            MQTT_FAILURES.labels(self.device_slug).inc()
            self._log.warning(
                "mqtt_replayed_record_rejected",
                error_class=type(error).__name__,
            )

    def drain_spool(self) -> bool:
        """Replay in FIFO order; stop immediately if CNPG becomes unavailable."""

        if not self.sink.healthcheck():
            return False
        for pending in self.spool.pending():
            try:
                decoded = decode_data_buf(
                    pending.batch.payload,
                    pending.batch.received_at,
                    expected_sequence=pending.batch.expected_sequence_before,
                )
                self.sink.commit_data_batch(pending.batch, decoded)
                self.spool.acknowledge(pending.batch.batch_id)
                self.expected_sequence = decoded.next_expected_sequence
                for record in decoded.records:
                    self._forward_replayed_mqtt_record(record)
            except DatabaseUnavailable as error:
                self.spool.mark_failure(pending.batch.batch_id, error)
                DATABASE_FAILURES.labels(self.device_slug).inc()
                self._update_spool_metrics()
                return False
        self._update_spool_metrics()
        return True

    def poll_data_once(self, device: Device, connection_id: UUID) -> DecodeResult | None:
        """Perform one guarded destructive read and durable two-stage commit."""

        if not self.sink.healthcheck():
            raise DatabaseUnavailable("database health gate is closed")
        if self.spool.pending_count() and not self.drain_spool():
            raise DatabaseUnavailable("spool replay did not complete")
        # Reserve before the destructive device read. This cannot make the USB
        # read and SQLite commit atomic, but it removes preventable disk-full loss.
        self.spool.ensure_capacity(self.settings.spool_reserved_batch_bytes)
        batch_id = uuid4()
        payload = device.read_data_buf_raw()
        received_at = self.utcnow()
        if not payload:
            DATA_BATCHES.labels(self.device_slug, "empty").inc()
            return None
        batch = RawBatch(
            batch_id=batch_id,
            device_slug=self.device_slug,
            connection_id=connection_id,
            received_at=received_at,
            payload=payload,
            sha256=spectrum_sha256(payload),
            expected_sequence_before=self.expected_sequence,
        )
        # This FULL-synchronous WAL commit is deliberately before decoding.
        self.spool.append(batch)
        self._update_spool_metrics()
        decoded = decode_data_buf(payload, received_at, expected_sequence=self.expected_sequence)
        try:
            self.sink.commit_data_batch(batch, decoded)
        except DatabaseUnavailable as error:
            self.spool.mark_failure(batch.batch_id, error)
            DATA_BATCHES.labels(self.device_slug, "spooled").inc()
            DATABASE_FAILURES.labels(self.device_slug).inc()
            raise
        self.spool.acknowledge(batch.batch_id)
        self.expected_sequence = decoded.next_expected_sequence
        self._update_spool_metrics()
        DATA_BATCHES.labels(self.device_slug, "committed").inc()
        for warning in decoded.warnings:
            DECODE_WARNINGS.labels(self.device_slug, warning.split(":", 1)[0]).inc()
        for record in decoded.records:
            if record.kind == "real_time" and record.values.get("valid") is True:
                LAST_VALID_REALTIME.labels(self.device_slug).set(received_at.timestamp())
                DETECTOR_AVAILABLE.labels(self.device_slug).set(1)
                self.last_valid_realtime_monotonic = self.monotonic()
            self._forward_mqtt_record(record)
        return decoded

    def poll_spectrum_once(self, device: Device, connection_id: UUID) -> SpectrumTransition:
        if not self.sink.healthcheck():
            raise DatabaseUnavailable("database health gate is closed")
        observation = device.read_spectrum(observed_at=self.utcnow())
        transition = self.sink.commit_spectrum(observation, connection_id=connection_id)
        if transition.gap is not None:
            SPECTRUM_GAPS.labels(self.device_slug, transition.gap.reason).inc()
            if self.telemetry_publisher is not None:
                try:
                    self.telemetry_publisher.publish_event(
                        TelemetryEvent(
                            kind="spectrum_gap",
                            observed_at=transition.gap.detected_at,
                            details={
                                "reason": transition.gap.reason,
                                "previous_duration_seconds": (transition.gap.previous_duration_seconds),
                                "observed_duration_seconds": (transition.gap.observed_duration_seconds),
                            },
                        )
                    )
                except Exception as error:
                    MQTT_FAILURES.labels(self.device_slug).inc()
                    self._log.warning(
                        "mqtt_spectrum_gap_rejected",
                        error_class=type(error).__name__,
                    )
        return transition

    def audit_accumulated_once(self, device: Device, connection_id: UUID, *, kind: str) -> None:
        if not self.sink.healthcheck():
            raise DatabaseUnavailable("database health gate is closed")
        try:
            observation = device.read_accumulated_spectrum(observed_at=self.utcnow())
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if classify_usb_error(error) != USBFailureClass.OTHER:
                raise
            ACCUMULATED_SNAPSHOT_FAILURES.labels(self.device_slug).inc()
            self._log.warning(
                "accumulated_snapshot_unsupported",
                error_class=type(error).__name__,
            )
            return
        try:
            self.sink.store_accumulated_snapshot(
                observation,
                connection_id=connection_id,
                snapshot_kind=kind,
            )
        except DatabaseUnavailable:
            raise
        except (ValueError, OverflowError) as error:
            ACCUMULATED_SNAPSHOT_FAILURES.labels(self.device_slug).inc()
            self._log.warning(
                "accumulated_snapshot_invalid",
                error_class=type(error).__name__,
            )

    def _handle_usb_failure(self, error: BaseException, connection_id: UUID | None) -> bool:
        """Return true when the collector must stop and await pod replacement."""

        failure = classify_usb_error(error)
        DETECTOR_AVAILABLE.labels(self.device_slug).set(0)
        USB_ERRORS.labels(self.device_slug, failure.value).inc()
        self._log.warning("usb_operation_failed", usb_failure=failure.value)
        if failure == USBFailureClass.NO_DEVICE and connection_id is not None:
            if not self.pod_uid:
                self._log.error("reallocation_marker_not_written", reason="missing_pod_uid")
                return False
            self.marker_writer(
                self.settings.reallocation_marker,
                pod_uid=self.pod_uid,
                connection_id=connection_id,
            )
            self._log.warning("reallocation_requested", reason="libusb_no_device")
            return True
        # Access denied, busy, timeout, initial absence, and generic failures can
        # alert/retry but can never request pod deletion.
        return False

    def run(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        retry_count = 0
        DETECTOR_AVAILABLE.labels(self.device_slug).set(0)
        set_collector_state(self.device_slug, "starting")
        self._update_spool_metrics()

        while not stop.is_set():
            device: Device | None = None
            connection_id: UUID | None = None
            close_reason = "reconnect"
            self._refresh_availability()
            self._update_spool_metrics()
            if not self.sink.healthcheck():
                set_collector_state(self.device_slug, "waiting_database")
                stop.wait(self.settings.database_retry_seconds)
                continue
            try:
                self.sink.ensure_device()
                if not self.drain_spool():
                    set_collector_state(self.device_slug, "waiting_database")
                    stop.wait(self.settings.database_retry_seconds)
                    continue
                if self.expected_sequence is None:
                    self.expected_sequence = self.sink.load_expected_sequence()

                set_collector_state(self.device_slug, "connecting")
                device = self.adapter_factory()
                device.connect()
                connection_id = uuid4()
                self.sink.record_connection_open(connection_id, self.utcnow())
                retry_count = 0
                self.audit_accumulated_once(device, connection_id, kind="connection")

                next_data = self.monotonic()
                next_spectrum = next_data
                next_audit = next_data + self.settings.accumulator_audit_seconds
                set_collector_state(self.device_slug, "active")
                while not stop.is_set():
                    self._refresh_availability()
                    self._update_spool_metrics()
                    if not self.sink.healthcheck():
                        set_collector_state(self.device_slug, "waiting_database")
                        stop.wait(self.settings.database_retry_seconds)
                        continue
                    if self.spool.pending_count() and not self.drain_spool():
                        set_collector_state(self.device_slug, "waiting_database")
                        stop.wait(self.settings.database_retry_seconds)
                        continue
                    set_collector_state(self.device_slug, "active")
                    try:
                        now = self.monotonic()
                        if now >= next_data:
                            self.poll_data_once(device, connection_id)
                            next_data = max(next_data + self.settings.data_poll_seconds, self.monotonic())
                        now = self.monotonic()
                        if now >= next_spectrum:
                            self.poll_spectrum_once(device, connection_id)
                            next_spectrum = max(
                                next_spectrum + self.settings.spectrum_poll_seconds,
                                self.monotonic(),
                            )
                        now = self.monotonic()
                        if now >= next_audit:
                            self.audit_accumulated_once(device, connection_id, kind="six_hour_audit")
                            next_audit = max(
                                next_audit + self.settings.accumulator_audit_seconds,
                                self.monotonic(),
                            )
                    except DatabaseUnavailable:
                        DATABASE_FAILURES.labels(self.device_slug).inc()
                        set_collector_state(self.device_slug, "waiting_database")
                        # Keep the established USB handle, but issue no device
                        # commands until CNPG and spool replay are healthy again.
                        stop.wait(self.settings.database_retry_seconds)
                        continue
                    deadline = min(next_data, next_spectrum, next_audit)
                    stop.wait(max(0.01, min(1.0, deadline - self.monotonic())))
            except DatabaseUnavailable:
                DATABASE_FAILURES.labels(self.device_slug).inc()
                set_collector_state(self.device_slug, "waiting_database")
                stop.wait(self.settings.database_retry_seconds)
            except SpoolCapacityError:
                self._log.error("spool_capacity_guard_blocked_read")
                set_collector_state(self.device_slug, "backoff")
                stop.wait(self.settings.database_retry_seconds)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                if self._handle_usb_failure(error, connection_id):
                    close_reason = "libusb_no_device"
                    return
                close_reason = classify_usb_error(error).value
                retry_count += 1
                set_collector_state(self.device_slug, "backoff")
                stop.wait(min(2 ** min(retry_count, 8), self.settings.usb_retry_cap_seconds))
            finally:
                if device is not None:
                    with suppress(BaseException):
                        device.close()
                if connection_id is not None:
                    with suppress(DatabaseUnavailable):
                        self.sink.record_connection_close(
                            connection_id,
                            self.utcnow(),
                            "shutdown" if stop.is_set() else close_reason,
                        )
        set_collector_state(self.device_slug, "stopped")


def build_collector(
    settings: Settings,
    *,
    telemetry_publisher: TelemetryPublisher | None = None,
) -> tuple[Collector, PostgresSink, SQLiteSpool]:
    serial = settings.require_device_serial()
    sink = PostgresSink(
        settings.require_database_dsn(),
        device_slug=settings.require_device_slug(),
        device_serial=serial,
        display_name=settings.device_display_name,
        model=settings.device_model,
        expected_channel_count=settings.expected_channel_count,
        frame_target_seconds=settings.frame_target_seconds,
        app_version=__version__,
    )
    spool = SQLiteSpool(settings.spool_path, max_bytes=settings.spool_max_bytes)
    return Collector(settings, sink, spool, telemetry_publisher=telemetry_publisher), sink, spool
