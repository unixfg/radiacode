from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .models import DecodeResult, DeviceSpectrum, RawBatch
from .spectrum import (
    ENCODING_VERSION_UINT32_LE,
    calibration_fingerprint,
    decode_counts_uint32le,
    encode_counts_uint32le,
    spectrum_sha256,
)
from .spectrum_state import (
    FrameAccumulator,
    SpectrumCursor,
    SpectrumState,
    SpectrumTransition,
    advance_spectrum_state,
)


class DatabaseUnavailable(RuntimeError):
    pass


class DatabaseIntegrityError(RuntimeError):
    pass


def stable_device_id(slug: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"https://doesthings.online/radiacode/{slug}")


class PostgresSink:
    def __init__(
        self,
        dsn: str,
        *,
        device_slug: str,
        device_serial: str,
        display_name: str | None = None,
        model: str = "RadiaCode",
        expected_channel_count: int | None = None,
        frame_target_seconds: int = 300,
        app_version: str = "unknown",
    ) -> None:
        from psycopg_pool import ConnectionPool

        self.device_slug = device_slug
        self._device_serial = device_serial
        self.device_id = stable_device_id(device_slug)
        self.display_name = display_name or device_slug
        self.model = model
        self.expected_channel_count = expected_channel_count
        self.frame_target_seconds = frame_target_seconds
        self.app_version = app_version
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": False, "application_name": f"radiacode-{device_slug}"},
            open=True,
        )

    def close(self) -> None:
        self._pool.close()

    def healthcheck(self) -> bool:
        try:
            with self._pool.connection(timeout=5) as connection:
                connection.execute("SELECT 1")
            return True
        except Exception:
            return False

    def telemetry_bootstrap_updates(self) -> tuple[Any, ...]:
        """Load only cached state values; historical events are never replayed."""

        from .mqtt import TelemetryUpdate

        updates: list[TelemetryUpdate] = []
        try:
            with self._pool.connection() as connection:
                realtime = connection.execute(
                    """
                    SELECT received_at, count_rate, count_rate_error_pct,
                           dose_rate, dose_rate_error_pct
                      FROM radiacode_private.scalar_samples
                     WHERE device_id = %s AND sample_kind = 'real_time'
                     ORDER BY received_at DESC
                     LIMIT 1
                    """,
                    (self.device_id,),
                ).fetchone()
                if realtime is not None:
                    updates.append(
                        TelemetryUpdate(
                            observed_at=realtime[0],
                            realtime_valid=True,
                            cps=float(realtime[1]),
                            cps_uncertainty_pct=(float(realtime[2]) if realtime[2] is not None else None),
                            dose_rate_usv_h=float(realtime[3]),
                            dose_rate_uncertainty_pct=(
                                float(realtime[4]) if realtime[4] is not None else None
                            ),
                        )
                    )
                status = connection.execute(
                    """
                    SELECT received_at, accumulated_dose, duration_seconds,
                           temperature_c, charge_pct
                      FROM radiacode_private.status_samples
                     WHERE device_id = %s
                     ORDER BY received_at DESC
                     LIMIT 1
                    """,
                    (self.device_id,),
                ).fetchone()
                if status is not None:
                    updates.append(
                        TelemetryUpdate(
                            observed_at=status[0],
                            realtime_valid=False,
                            accumulated_dose_usv=float(status[1]),
                            accumulated_duration_seconds=int(status[2]),
                            temperature_c=float(status[3]),
                            battery_percent=float(status[4]),
                        )
                    )
                runtime = connection.execute(
                    """
                    SELECT charging_observed_at, charging
                      FROM radiacode_private.device_runtime_state
                     WHERE device_id = %s AND charging_observed_at IS NOT NULL
                    """,
                    (self.device_id,),
                ).fetchone()
                if runtime is not None:
                    updates.append(
                        TelemetryUpdate(
                            observed_at=runtime[0],
                            realtime_valid=False,
                            charging=bool(runtime[1]),
                        )
                    )
        except Exception:
            raise DatabaseUnavailable("database operation failed") from None
        return tuple(updates)

    def load_expected_sequence(self) -> int | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT next_buffer_sequence
                      FROM radiacode_private.device_runtime_state
                     WHERE device_id = %s
                    """,
                    (self.device_id,),
                ).fetchone()
        except Exception:
            raise DatabaseUnavailable("database operation failed") from None
        return int(row[0]) if row is not None and row[0] is not None else None

    def ensure_device(self) -> UUID:
        try:
            with self._pool.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    INSERT INTO radiacode_private.devices(
                        device_id, slug, display_name, model, usb_serial
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (slug) DO NOTHING
                    RETURNING device_id, usb_serial
                    """,
                    (
                        self.device_id,
                        self.device_slug,
                        self.display_name,
                        self.model,
                        self._device_serial,
                    ),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT device_id, usb_serial
                          FROM radiacode_private.devices
                         WHERE slug = %s
                        """,
                        (self.device_slug,),
                    ).fetchone()
                assert row is not None
                if row[0] != self.device_id or not hmac.compare_digest(row[1], self._device_serial):
                    raise DatabaseIntegrityError("configured device identity conflicts with the database")
                connection.execute(
                    """
                    UPDATE radiacode_private.devices
                       SET display_name = %s,
                           model = %s
                     WHERE device_id = %s
                    """,
                    (self.display_name, self.model, self.device_id),
                )
            return self.device_id
        except DatabaseIntegrityError:
            raise
        except Exception:
            raise DatabaseUnavailable("database operation failed") from None

    def record_connection_open(
        self,
        connection_id: UUID,
        connected_at: datetime,
        *,
        firmware: tuple[int, int] | None = None,
    ) -> None:
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    """
                    INSERT INTO radiacode_private.connections(
                        connection_id, device_id, connected_at,
                        firmware_major, firmware_minor, app_version
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (connection_id) DO NOTHING
                    """,
                    (
                        connection_id,
                        self.device_id,
                        connected_at,
                        firmware[0] if firmware else None,
                        firmware[1] if firmware else None,
                        self.app_version,
                    ),
                )
        except Exception:
            raise DatabaseUnavailable("database operation failed") from None

    def record_connection_close(self, connection_id: UUID, disconnected_at: datetime, reason: str) -> None:
        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    """
                    UPDATE radiacode_private.connections
                       SET disconnected_at = %s, close_reason = %s
                     WHERE connection_id = %s AND disconnected_at IS NULL
                    """,
                    (disconnected_at, reason, connection_id),
                )
        except Exception:
            raise DatabaseUnavailable("database operation failed") from None

    def commit_data_batch(self, batch: RawBatch, decoded: DecodeResult) -> bool:
        """Atomically store the raw batch and every decoded projection.

        Returns `False` for an already-committed, checksum-identical replay.
        """

        from psycopg.types.json import Jsonb

        sequences = [record.sequence for record in decoded.records if record.sequence is not None]
        decode_status = (
            "truncated"
            if decoded.truncated
            else "unknown_tail"
            if decoded.unknown_tail
            else "warning"
            if decoded.warnings
            else "ok"
        )
        try:
            with self._pool.connection() as connection, connection.transaction():
                inserted = connection.execute(
                    """
                    INSERT INTO radiacode_private.raw_buffer_batches(
                        received_at, batch_id, device_id, connection_id, payload,
                        sha256, first_sequence, last_sequence, record_count,
                        decode_status, warnings
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (received_at, batch_id) DO NOTHING
                    RETURNING batch_id
                    """,
                    (
                        batch.received_at,
                        batch.batch_id,
                        self.device_id,
                        batch.connection_id,
                        batch.payload,
                        batch.sha256,
                        sequences[0] if sequences else None,
                        sequences[-1] if sequences else None,
                        len(decoded.records),
                        decode_status,
                        Jsonb(decoded.warnings),
                    ),
                ).fetchone()
                if inserted is None:
                    existing = connection.execute(
                        """
                        SELECT sha256 FROM radiacode_private.raw_buffer_batches
                         WHERE received_at = %s AND batch_id = %s
                        """,
                        (batch.received_at, batch.batch_id),
                    ).fetchone()
                    if existing is None or bytes(existing[0]) != batch.sha256:
                        raise DatabaseIntegrityError("batch replay checksum mismatch")
                    return False

                for record in decoded.records:
                    connection.execute(
                        """
                        INSERT INTO radiacode_private.buffer_records(
                            received_at, batch_id, record_index, device_id,
                            connection_id, sequence, event_id, group_id, device_tick,
                            sample_at, timestamp_quality, kind, flags, raw_record,
                            raw_payload, values_json, warnings
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            batch.received_at,
                            batch.batch_id,
                            record.record_index,
                            self.device_id,
                            batch.connection_id,
                            record.sequence,
                            record.event_id,
                            record.group_id,
                            record.device_tick,
                            record.sample_at,
                            record.timestamp_quality,
                            record.kind,
                            record.flags,
                            record.raw_record,
                            record.raw_payload,
                            Jsonb(record.values),
                            Jsonb(record.warnings),
                        ),
                    )
                    self._insert_projection(connection, batch, record)
                    for warning in record.warnings:
                        if not warning.startswith("sequence_gap:"):
                            continue
                        details: dict[str, int] = {"record_index": record.record_index}
                        for component in warning.split(":")[1:]:
                            name, separator, value = component.partition("=")
                            if separator and name in {"expected", "observed", "distance"}:
                                details[name] = int(value)
                        connection.execute(
                            """
                            INSERT INTO radiacode_private.data_gaps(
                                gap_id, device_id, detected_at, gap_kind, details
                            ) VALUES (%s, %s, %s, 'data_buf_sequence_gap', %s)
                            """,
                            (uuid4(), self.device_id, batch.received_at, Jsonb(details)),
                        )
                if decoded.next_expected_sequence is not None:
                    connection.execute(
                        """
                        INSERT INTO radiacode_private.device_runtime_state(
                            device_id, next_buffer_sequence, buffer_sequence_observed_at
                        ) VALUES (%s, %s, %s)
                        ON CONFLICT (device_id) DO UPDATE
                           SET next_buffer_sequence = EXCLUDED.next_buffer_sequence,
                               buffer_sequence_observed_at = EXCLUDED.buffer_sequence_observed_at
                         WHERE radiacode_private.device_runtime_state.buffer_sequence_observed_at IS NULL
                            OR radiacode_private.device_runtime_state.buffer_sequence_observed_at
                               <= EXCLUDED.buffer_sequence_observed_at
                        """,
                        (
                            self.device_id,
                            decoded.next_expected_sequence,
                            batch.received_at,
                        ),
                    )
            return True
        except DatabaseIntegrityError:
            raise
        except Exception as error:
            raise DatabaseUnavailable("database operation failed") from error

    def _insert_projection(self, connection: Any, batch: RawBatch, record: Any) -> None:
        values = record.values
        if record.kind in {"real_time", "raw", "dose_rate_db"}:
            if record.kind == "real_time" and values.get("valid") is not True:
                return
            connection.execute(
                """
                INSERT INTO radiacode_private.scalar_samples(
                    received_at, batch_id, record_index, device_id, sample_at,
                    timestamp_quality, sample_kind, count_value, count_rate,
                    dose_rate, count_rate_error_pct, dose_rate_error_pct,
                    flags, real_time_flags
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    batch.received_at,
                    batch.batch_id,
                    record.record_index,
                    self.device_id,
                    record.sample_at,
                    record.timestamp_quality,
                    record.kind,
                    values.get("count"),
                    values.get("count_rate"),
                    values.get("dose_rate"),
                    values.get("count_rate_error_pct"),
                    values.get("dose_rate_error_pct"),
                    record.flags,
                    values.get("real_time_flags"),
                ),
            )
            if record.kind == "real_time":
                connection.execute(
                    """
                    INSERT INTO radiacode_private.scalar_rollup_dirty(device_id, bucket_at)
                    VALUES (%s, date_trunc('minute', %s::timestamptz, 'UTC'))
                    ON CONFLICT (device_id, bucket_at) DO NOTHING
                    """,
                    (self.device_id, batch.received_at),
                )
        elif record.kind == "rare":
            if values.get("valid") is not True:
                return
            connection.execute(
                """
                INSERT INTO radiacode_private.status_samples(
                    status_id, device_id, received_at, sample_at,
                    timestamp_quality, duration_seconds, accumulated_dose,
                    temperature_c, charge_pct, flags
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    self.device_id,
                    batch.received_at,
                    record.sample_at,
                    record.timestamp_quality,
                    values["duration_seconds"],
                    values["accumulated_dose"],
                    values["temperature_c"],
                    values["charge_pct"],
                    record.flags,
                ),
            )
        elif record.kind == "event":
            connection.execute(
                """
                INSERT INTO radiacode_private.device_events(
                    event_row_id, device_id, received_at, sample_at,
                    timestamp_quality, event_code, event_name,
                    event_parameter, flags
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    self.device_id,
                    batch.received_at,
                    record.sample_at,
                    record.timestamp_quality,
                    values["event"],
                    values["event_name"],
                    values["parameter"],
                    record.flags,
                ),
            )
            if values["event_name"] in {"charge_start", "charge_stop"}:
                connection.execute(
                    """
                    INSERT INTO radiacode_private.device_runtime_state(
                        device_id, charging, charging_observed_at
                    ) VALUES (%s, %s, %s)
                    ON CONFLICT (device_id) DO UPDATE
                       SET charging = EXCLUDED.charging,
                           charging_observed_at = EXCLUDED.charging_observed_at
                     WHERE radiacode_private.device_runtime_state.charging_observed_at IS NULL
                        OR radiacode_private.device_runtime_state.charging_observed_at
                           <= EXCLUDED.charging_observed_at
                    """,
                    (
                        self.device_id,
                        values["event_name"] == "charge_start",
                        batch.received_at,
                    ),
                )

    def commit_spectrum(
        self,
        observation: DeviceSpectrum,
        *,
        connection_id: UUID,
    ) -> SpectrumTransition:
        try:
            with self._pool.connection() as connection, connection.transaction():
                state, current_calibration_id = self._load_spectrum_state(connection)
                transition = advance_spectrum_state(
                    state,
                    observation,
                    connection_id=connection_id,
                    expected_channel_count=self.expected_channel_count,
                    frame_target_seconds=self.frame_target_seconds,
                )
                observed_fingerprint = calibration_fingerprint(
                    len(observation.counts),
                    observation.coefficients,
                )
                calibration_id = self._ensure_calibration_epoch(
                    connection,
                    observation,
                    observed_fingerprint,
                    current_calibration_id=current_calibration_id,
                )

                if transition.closed_session is not None:
                    connection.execute(
                        """
                        UPDATE radiacode_private.spectrum_sessions
                           SET ended_at = %s, end_reason = %s
                         WHERE session_id = %s AND ended_at IS NULL
                        """,
                        (
                            observation.observed_at,
                            transition.gap.reason if transition.gap else "session_boundary",
                            transition.closed_session,
                        ),
                    )
                if transition.started_session is not None:
                    connection.execute(
                        """
                        INSERT INTO radiacode_private.spectrum_sessions(
                            session_id, device_id, calibration_epoch_id, started_at
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (transition.started_session, self.device_id, calibration_id, observation.observed_at),
                    )

                frame_calibration_id = (
                    current_calibration_id if transition.closed_session is not None else calibration_id
                )
                self._store_spectrum_state(
                    connection,
                    transition,
                    calibration_id,
                    frame_calibration_id=frame_calibration_id,
                )
                if transition.gap is not None:
                    from psycopg.types.json import Jsonb

                    connection.execute(
                        """
                        INSERT INTO radiacode_private.data_gaps(
                            gap_id, device_id, session_id, detected_at,
                            gap_kind, details
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            uuid4(),
                            self.device_id,
                            transition.gap.session_id,
                            transition.gap.detected_at,
                            transition.gap.reason,
                            Jsonb(
                                {
                                    "previous_duration_seconds": transition.gap.previous_duration_seconds,
                                    "observed_duration_seconds": transition.gap.observed_duration_seconds,
                                }
                            ),
                        ),
                    )
                return transition
        except (ValueError, DatabaseIntegrityError):
            raise
        except Exception:
            raise DatabaseUnavailable("database operation failed") from None

    def _load_spectrum_state(self, connection: Any) -> tuple[SpectrumState, UUID | None]:
        row = connection.execute(
            """
            SELECT cursor.session_id, cursor.connection_id, cursor.observed_at,
                   cursor.duration_seconds, cursor.channel_count, cursor.counts,
                   cursor.calibration_epoch_id, epoch.coefficient_a0,
                   epoch.coefficient_a1, epoch.coefficient_a2, epoch.fingerprint
              FROM radiacode_private.spectrum_cursors cursor
              JOIN radiacode_private.calibration_epochs epoch
                ON epoch.calibration_epoch_id = cursor.calibration_epoch_id
             WHERE cursor.device_id = %s
             FOR UPDATE OF cursor
            """,
            (self.device_id,),
        ).fetchone()
        if row is None:
            return SpectrumState(), None
        cursor = SpectrumCursor(
            session_id=row[0],
            connection_id=row[1],
            observed_at=row[2],
            duration_seconds=int(row[3]),
            counts=decode_counts_uint32le(bytes(row[5]), int(row[4])),
            coefficients=(float(row[7]), float(row[8]), float(row[9])),
            calibration_fingerprint=bytes(row[10]),
        )
        accumulator_row = connection.execute(
            """
            SELECT session_id, started_at, ended_at, duration_seconds,
                   channel_count, counts, source_intervals, quality_flags
              FROM radiacode_private.spectrum_frame_accumulators
             WHERE device_id = %s
             FOR UPDATE
            """,
            (self.device_id,),
        ).fetchone()
        accumulator = None
        if accumulator_row is not None:
            accumulator = FrameAccumulator(
                session_id=accumulator_row[0],
                started_at=accumulator_row[1],
                ended_at=accumulator_row[2],
                duration_seconds=int(accumulator_row[3]),
                counts=decode_counts_uint32le(bytes(accumulator_row[5]), int(accumulator_row[4])),
                source_intervals=int(accumulator_row[6]),
                quality_flags=tuple(accumulator_row[7] or ()),
            )
        return SpectrumState(cursor=cursor, accumulator=accumulator), row[6]

    def _ensure_calibration_epoch(
        self,
        connection: Any,
        observation: DeviceSpectrum,
        fingerprint: bytes,
        *,
        current_calibration_id: UUID | None,
    ) -> UUID:
        row = connection.execute(
            """
            SELECT calibration_epoch_id, fingerprint
              FROM radiacode_private.calibration_epochs
             WHERE device_id = %s AND ended_at IS NULL
             FOR UPDATE
            """,
            (self.device_id,),
        ).fetchone()
        if row is not None and bytes(row[1]) == fingerprint:
            if current_calibration_id is not None and row[0] != current_calibration_id:
                raise DatabaseIntegrityError("cursor calibration does not match the open epoch")
            return cast(UUID, row[0])
        if row is not None:
            connection.execute(
                """
                UPDATE radiacode_private.calibration_epochs
                   SET ended_at = %s
                 WHERE calibration_epoch_id = %s
                """,
                (observation.observed_at, row[0]),
            )
        calibration_id = uuid4()
        connection.execute(
            """
            INSERT INTO radiacode_private.calibration_epochs(
                calibration_epoch_id, device_id, started_at, channel_count,
                coefficient_a0, coefficient_a1, coefficient_a2, fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                calibration_id,
                self.device_id,
                observation.observed_at,
                len(observation.counts),
                *observation.coefficients,
                fingerprint,
            ),
        )
        return calibration_id

    def _snapshot_calibration_epoch(
        self,
        connection: Any,
        observation: DeviceSpectrum,
        fingerprint: bytes,
    ) -> UUID:
        """Resolve snapshot calibration without changing the live spectrum epoch."""

        row = connection.execute(
            """
            SELECT calibration_epoch_id
              FROM radiacode_private.calibration_epochs
             WHERE device_id = %s AND fingerprint = %s
             ORDER BY (ended_at IS NULL) DESC, started_at DESC
             LIMIT 1
            """,
            (self.device_id, fingerprint),
        ).fetchone()
        if row is not None:
            return cast(UUID, row[0])

        # spectrum_accum() is audit-only. A zero-duration closed epoch provides
        # an immutable calibration reference without opening, closing, or
        # replacing the authoritative cumulative-spectrum epoch.
        calibration_id = uuid4()
        connection.execute(
            """
            INSERT INTO radiacode_private.calibration_epochs(
                calibration_epoch_id, device_id, started_at, ended_at,
                channel_count, coefficient_a0, coefficient_a1,
                coefficient_a2, fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                calibration_id,
                self.device_id,
                observation.observed_at,
                observation.observed_at,
                len(observation.counts),
                *observation.coefficients,
                fingerprint,
            ),
        )
        return calibration_id

    def _store_spectrum_state(
        self,
        connection: Any,
        transition: SpectrumTransition,
        calibration_id: UUID,
        *,
        frame_calibration_id: UUID | None,
    ) -> None:
        from psycopg.types.json import Jsonb

        cursor = transition.state.cursor
        assert cursor is not None
        encoded_cursor = encode_counts_uint32le(
            cursor.counts,
        )
        connection.execute(
            """
            INSERT INTO radiacode_private.spectrum_cursors(
                device_id, session_id, connection_id, calibration_epoch_id,
                observed_at, duration_seconds, channel_count,
                counts_encoding_version, counts, total_count, sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (device_id) DO UPDATE SET
                session_id = EXCLUDED.session_id,
                connection_id = EXCLUDED.connection_id,
                calibration_epoch_id = EXCLUDED.calibration_epoch_id,
                observed_at = EXCLUDED.observed_at,
                duration_seconds = EXCLUDED.duration_seconds,
                channel_count = EXCLUDED.channel_count,
                counts_encoding_version = EXCLUDED.counts_encoding_version,
                counts = EXCLUDED.counts,
                total_count = EXCLUDED.total_count,
                sha256 = EXCLUDED.sha256
            """,
            (
                self.device_id,
                cursor.session_id,
                cursor.connection_id,
                calibration_id,
                cursor.observed_at,
                cursor.duration_seconds,
                len(cursor.counts),
                ENCODING_VERSION_UINT32_LE,
                encoded_cursor,
                sum(cursor.counts),
                spectrum_sha256(encoded_cursor),
            ),
        )

        accumulator = transition.state.accumulator
        if accumulator is None:
            connection.execute(
                "DELETE FROM radiacode_private.spectrum_frame_accumulators WHERE device_id = %s",
                (self.device_id,),
            )
        else:
            encoded_accumulator = encode_counts_uint32le(accumulator.counts)
            connection.execute(
                """
                INSERT INTO radiacode_private.spectrum_frame_accumulators(
                    device_id, session_id, started_at, ended_at, duration_seconds,
                    channel_count, counts_encoding_version, counts, total_count,
                    source_intervals, quality_flags
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (device_id) DO UPDATE SET
                    session_id = EXCLUDED.session_id,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    duration_seconds = EXCLUDED.duration_seconds,
                    channel_count = EXCLUDED.channel_count,
                    counts_encoding_version = EXCLUDED.counts_encoding_version,
                    counts = EXCLUDED.counts,
                    total_count = EXCLUDED.total_count,
                    source_intervals = EXCLUDED.source_intervals,
                    quality_flags = EXCLUDED.quality_flags
                """,
                (
                    self.device_id,
                    accumulator.session_id,
                    accumulator.started_at,
                    accumulator.ended_at,
                    accumulator.duration_seconds,
                    len(accumulator.counts),
                    ENCODING_VERSION_UINT32_LE,
                    encoded_accumulator,
                    sum(accumulator.counts),
                    accumulator.source_intervals,
                    Jsonb(accumulator.quality_flags),
                ),
            )

        frame = transition.frame
        if frame is not None:
            if frame_calibration_id is None:
                raise DatabaseIntegrityError("frame has no calibration epoch")
            encoded_frame = encode_counts_uint32le(
                frame.counts,
            )

            connection.execute(
                """
                INSERT INTO radiacode_private.spectrum_frames(
                    frame_id, device_id, session_id, calibration_epoch_id,
                    started_at, ended_at, duration_seconds, channel_count,
                    counts_encoding_version, counts, total_count, sha256,
                    source_intervals, quality_flags
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    frame.frame_id,
                    self.device_id,
                    frame.session_id,
                    frame_calibration_id,
                    frame.started_at,
                    frame.ended_at,
                    frame.duration_seconds,
                    len(frame.counts),
                    ENCODING_VERSION_UINT32_LE,
                    encoded_frame,
                    sum(frame.counts),
                    spectrum_sha256(encoded_frame),
                    frame.source_intervals,
                    Jsonb(frame.quality_flags),
                ),
            )

    def store_accumulated_snapshot(
        self,
        observation: DeviceSpectrum,
        *,
        connection_id: UUID,
        snapshot_kind: str,
    ) -> None:
        if snapshot_kind not in {"connection", "six_hour_audit"}:
            raise ValueError("unsupported accumulated snapshot kind")
        encoded = encode_counts_uint32le(
            observation.counts,
        )
        fingerprint = calibration_fingerprint(len(observation.counts), observation.coefficients)
        try:
            with self._pool.connection() as connection, connection.transaction():
                calibration_id = self._snapshot_calibration_epoch(
                    connection,
                    observation,
                    fingerprint,
                )
                from psycopg.types.json import Jsonb

                connection.execute(
                    """
                    INSERT INTO radiacode_private.spectrum_snapshots(
                        snapshot_id, device_id, connection_id, calibration_epoch_id, observed_at,
                        snapshot_kind, duration_seconds, channel_count,
                        coefficient_a0, coefficient_a1, coefficient_a2,
                        counts_encoding_version, counts, total_count, sha256, quality_flags
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        uuid4(),
                        self.device_id,
                        connection_id,
                        calibration_id,
                        observation.observed_at,
                        snapshot_kind,
                        observation.duration_seconds,
                        len(observation.counts),
                        *observation.coefficients,
                        ENCODING_VERSION_UINT32_LE,
                        encoded,
                        sum(observation.counts),
                        spectrum_sha256(encoded),
                        Jsonb(("audit_only", "spectrum_accum_undocumented")),
                    ),
                )
        except Exception:
            raise DatabaseUnavailable("database operation failed") from None
