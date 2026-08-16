from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from radiacode_app.api.repository import PublicRepository
from radiacode_app.collector import Collector
from radiacode_app.database import (
    DatabaseIntegrityError,
    DatabaseUnavailable,
    PostgresSink,
)
from radiacode_app.databuf import decode_data_buf
from radiacode_app.maintenance import MAINTENANCE_LOCK_ID, Maintenance
from radiacode_app.migrator import bundled_migrations, migrate
from radiacode_app.models import DecodeResult, DeviceSpectrum, RawBatch
from radiacode_app.settings import Settings
from radiacode_app.spectrum import encode_counts_uint32le, spectrum_sha256
from radiacode_app.spool import SQLiteSpool

ADMIN_DSN = os.environ.get("RADIACODE_TEST_DATABASE_DSN")
pytestmark = pytest.mark.skipif(
    ADMIN_DSN is None,
    reason="RADIACODE_TEST_DATABASE_DSN is not set",
)
ROLE_NAMES = ("radiacode_writer", "radiacode_maintenance", "radiacode_reader")


@dataclass(frozen=True, slots=True)
class IntegrationDatabase:
    dsn: str
    first_migration_run: tuple[str, ...]
    second_migration_run: tuple[str, ...]


@pytest.fixture(scope="module")
def integration_database() -> Iterator[IntegrationDatabase]:
    assert ADMIN_DSN is not None
    database_name = f"radiacode_it_{uuid4().hex[:16]}"
    created_roles: list[str] = []
    with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
        for role_name in ROLE_NAMES:
            exists = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role_name,),
            ).fetchone()
            assert exists is not None
            if not exists[0]:
                connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name)))
                created_roles.append(role_name)
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    test_dsn = make_conninfo(ADMIN_DSN, dbname=database_name)
    database = IntegrationDatabase(
        dsn=test_dsn,
        first_migration_run=migrate(test_dsn),
        second_migration_run=migrate(test_dsn),
    )
    try:
        yield database
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
            for role_name in reversed(created_roles):
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))


def _wire_record(sequence: int, event_id: int, group_id: int, tick: int, body: bytes) -> bytes:
    return struct.pack("<BBBi", sequence, event_id, group_id, tick) + body


def _raw_batch(
    *,
    connection_id: UUID,
    received_at: datetime,
    payload: bytes,
    batch_id: UUID | None = None,
) -> RawBatch:
    return RawBatch(
        batch_id=batch_id or uuid4(),
        device_slug="rc-integration",
        connection_id=connection_id,
        received_at=received_at,
        payload=payload,
        sha256=spectrum_sha256(payload),
    )


@contextmanager
def _reader_repository(admin_dsn: str) -> Iterator[tuple[PublicRepository, str]]:
    """Use a real LOGIN inheriting only the public reader role.

    PublicRepository installs its own libpq `options`, so a test DSN using
    `options=-c role=radiacode_reader` would be silently overridden and would
    exercise the administrative user instead of the production privilege path.
    """

    role_name = f"radiacode_it_reader_{uuid4().hex[:12]}"
    password = uuid4().hex
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role_name),
                sql.Literal(password),
            )
        )
        connection.execute(sql.SQL("GRANT radiacode_reader TO {}").format(sql.Identifier(role_name)))

    reader_dsn = make_conninfo(admin_dsn, user=role_name, password=password)
    repository = PublicRepository(reader_dsn, max_size=2)
    try:
        repository.open()
        try:
            yield repository, reader_dsn
        finally:
            repository.close()
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))


def test_migrations_reapply_and_enforce_role_boundaries(
    integration_database: IntegrationDatabase,
) -> None:
    expected_versions = tuple(migration.version for migration in bundled_migrations())
    assert integration_database.first_migration_run == expected_versions
    assert integration_database.second_migration_run == ()

    with psycopg.connect(integration_database.dsn) as connection:
        checks = dict(
            connection.execute(
                """
                SELECT 'maintenance_private_schema',
                       has_schema_privilege(
                           'radiacode_maintenance', 'radiacode_private', 'USAGE'
                       )
                UNION ALL
                SELECT 'maintenance_scalar_select',
                       has_table_privilege(
                           'radiacode_maintenance',
                           'radiacode_private.scalar_samples',
                           'SELECT'
                       )
                UNION ALL
                SELECT 'maintenance_scalar_insert',
                       has_table_privilege(
                           'radiacode_maintenance',
                           'radiacode_private.scalar_samples',
                           'INSERT'
                       )
                UNION ALL
                SELECT 'maintenance_rollup_write',
                       has_table_privilege(
                           'radiacode_maintenance',
                           'radiacode_private.scalar_minute_rollups',
                           'SELECT,INSERT,UPDATE'
                       )
                UNION ALL
                SELECT 'maintenance_rollup_delete',
                       has_table_privilege(
                           'radiacode_maintenance',
                           'radiacode_private.scalar_minute_rollups',
                           'DELETE'
                       )
                UNION ALL
                SELECT 'maintenance_devices_select',
                       has_table_privilege(
                           'radiacode_maintenance', 'radiacode_private.devices', 'SELECT'
                       )
                UNION ALL
                SELECT 'maintenance_partition_execute',
                       has_function_privilege(
                           'radiacode_maintenance',
                           'radiacode_private.ensure_daily_partitions(date,date)',
                           'EXECUTE'
                       )
                UNION ALL
                SELECT 'writer_private_full_dml',
                       has_table_privilege(
                           'radiacode_writer',
                           'radiacode_private.devices',
                           'SELECT,INSERT,UPDATE,DELETE'
                       )
                UNION ALL
                SELECT 'writer_device_identity_select',
                       has_column_privilege(
                           'radiacode_writer',
                           'radiacode_private.devices',
                           'device_id',
                           'SELECT'
                       )
                UNION ALL
                SELECT 'writer_device_created_update',
                       has_column_privilege(
                           'radiacode_writer',
                           'radiacode_private.devices',
                           'created_at',
                           'UPDATE'
                       )
                UNION ALL
                SELECT 'writer_connection_predicate_select',
                       has_column_privilege(
                           'radiacode_writer',
                           'radiacode_private.connections',
                           'disconnected_at',
                           'SELECT'
                       )
                UNION ALL
                SELECT 'writer_bootstrap_status_select',
                       has_column_privilege(
                           'radiacode_writer',
                           'radiacode_private.status_samples',
                           'charge_pct',
                           'SELECT'
                       )
                UNION ALL
                SELECT 'writer_api_usage',
                       has_schema_privilege('radiacode_writer', 'radiacode_api', 'USAGE')
                UNION ALL
                SELECT 'reader_api_select',
                       has_table_privilege(
                           'radiacode_reader', 'radiacode_api.devices', 'SELECT'
                       )
                UNION ALL
                SELECT 'reader_private_usage',
                       has_schema_privilege(
                           'radiacode_reader', 'radiacode_private', 'USAGE'
                       )
                UNION ALL
                SELECT 'reader_private_select',
                       has_table_privilege(
                           'radiacode_reader', 'radiacode_private.devices', 'SELECT'
                       )
                """
            ).fetchall()
        )
        assert checks == {
            "maintenance_devices_select": False,
            "maintenance_partition_execute": True,
            "maintenance_private_schema": True,
            "maintenance_rollup_delete": False,
            "maintenance_rollup_write": True,
            "maintenance_scalar_insert": False,
            "maintenance_scalar_select": True,
            "reader_api_select": True,
            "reader_private_select": False,
            "reader_private_usage": False,
            "writer_api_usage": False,
            "writer_bootstrap_status_select": True,
            "writer_connection_predicate_select": True,
            "writer_device_created_update": False,
            "writer_device_identity_select": True,
            "writer_private_full_dml": False,
        }

        function_security = connection.execute(
            """
            SELECT proname, prosecdef, proconfig
              FROM pg_proc
              JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
             WHERE pg_namespace.nspname = 'radiacode_private'
               AND proname IN ('ensure_daily_partitions', 'drop_daily_partitions_before')
             ORDER BY proname
            """
        ).fetchall()
        assert function_security == [
            (
                "drop_daily_partitions_before",
                True,
                ["search_path=pg_catalog, pg_temp"],
            ),
            ("ensure_daily_partitions", True, ["search_path=pg_catalog, pg_temp"]),
        ]

        connection.execute("SET ROLE radiacode_maintenance")
        connection.execute(
            "SELECT radiacode_private.ensure_daily_partitions(DATE '2000-01-01', DATE '2000-01-01')"
        )
        dropped = connection.execute(
            "SELECT radiacode_private.drop_daily_partitions_before(DATE '2000-01-02')"
        ).fetchone()
        assert dropped == (3,)
        connection.execute("RESET ROLE")
        connection.execute("SET ROLE radiacode_reader")
        connection.execute("SELECT count(*) FROM radiacode_api.devices").fetchone()
        connection.execute("RESET ROLE")

        api_columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'radiacode_api'"
            ).fetchall()
        }
        assert "usb_serial" not in api_columns
        assert "device_id" not in api_columns
        assert "connection_id" not in api_columns


def test_postgres_sink_atomic_replay_spectrum_and_maintenance(
    integration_database: IntegrationDatabase,
) -> None:
    observed_at = datetime.now(UTC) - timedelta(minutes=10)
    connection_id = uuid4()
    payload = b"".join(
        (
            _wire_record(
                1,
                0,
                0,
                100,
                struct.pack("<ffHHHB", 12.5, 0.25, 31, 42, 0x1234, 0x56),
            ),
            _wire_record(2, 0, 3, 101, struct.pack("<IfHHH", 99, 1.5, 2345, 8700, 0x7788)),
            _wire_record(3, 0, 7, 102, struct.pack("<BBH", 7, 0, 9)),
        )
    )
    decoded = decode_data_buf(payload, observed_at)
    batch = _raw_batch(
        connection_id=connection_id,
        received_at=observed_at,
        payload=payload,
    )
    writer_dsn = make_conninfo(
        integration_database.dsn,
        options="-c role=radiacode_writer",
    )
    sink = PostgresSink(
        writer_dsn,
        device_slug="rc-integration",
        device_serial="private-integration-serial",
        expected_channel_count=3,
        frame_target_seconds=300,
        app_version="integration-test",
    )
    try:
        assert sink.healthcheck()
        device_id = sink.ensure_device()
        sink.record_connection_open(connection_id, observed_at, firmware=(4, 13))
        assert sink.commit_data_batch(batch, decoded)
        assert not sink.commit_data_batch(batch, decoded)
        assert sink.load_expected_sequence() == 4
        bootstrap = sink.telemetry_bootstrap_updates()
        assert len(bootstrap) == 3
        assert bootstrap[0].cps == 12.5
        assert bootstrap[1].battery_percent == 87
        assert bootstrap[2].charging is True

        gap_payload = _wire_record(6, 0, 1, 103, struct.pack("<ff", 2.0, 0.1))
        gap_batch = _raw_batch(
            connection_id=connection_id,
            received_at=observed_at + timedelta(seconds=2),
            payload=gap_payload,
        )
        assert sink.commit_data_batch(
            gap_batch,
            decode_data_buf(gap_payload, gap_batch.received_at),
        )
        assert sink.load_expected_sequence() == 7

        changed_payload = payload + b"changed"
        mismatched_replay = _raw_batch(
            batch_id=batch.batch_id,
            connection_id=connection_id,
            received_at=observed_at,
            payload=changed_payload,
        )
        with pytest.raises(DatabaseIntegrityError):
            sink.commit_data_batch(
                mismatched_replay,
                decode_data_buf(changed_payload, observed_at),
            )

        bad_record = replace(decoded.records[0], flags=1 << 40)
        bad_decoded = DecodeResult(
            records=(bad_record,),
            warnings=(),
            next_expected_sequence=2,
            truncated=False,
            unknown_tail=False,
        )
        failed_batch = _raw_batch(
            connection_id=connection_id,
            received_at=observed_at + timedelta(seconds=1),
            payload=b"transaction-must-roll-back",
        )
        with pytest.raises(DatabaseUnavailable):
            sink.commit_data_batch(failed_batch, bad_decoded)

        transitions = []
        for offset, duration, counts in (
            (0, 10, (10, 20, 1)),
            (60, 70, (20, 30, 2)),
            (300, 310, (50, 80, 3)),
            (360, 1, (1, 1, 0)),
        ):
            transitions.append(
                sink.commit_spectrum(
                    DeviceSpectrum(
                        observed_at=observed_at + timedelta(seconds=offset),
                        duration_seconds=duration,
                        coefficients=(0.0, 1.0, 0.0),
                        counts=counts,
                    ),
                    connection_id=connection_id,
                )
            )
        assert sum(transition.frame is not None for transition in transitions) == 1
        assert sum(transition.gap is not None for transition in transitions) == 1
        sink.store_accumulated_snapshot(
            DeviceSpectrum(observed_at, 500, (5.0, 2.0, 0.0), (100, 200, 5)),
            connection_id=connection_id,
            snapshot_kind="connection",
        )
        # The undocumented audit source may report a different calibration.
        # It must not replace the live epoch or wedge the next normal spectrum.
        continued = sink.commit_spectrum(
            DeviceSpectrum(
                observed_at + timedelta(seconds=420),
                61,
                (0.0, 1.0, 0.0),
                (2, 2, 0),
            ),
            connection_id=connection_id,
        )
        assert continued.gap is None
        sink.record_connection_close(
            connection_id,
            observed_at + timedelta(hours=1),
            "integration_test",
        )
    finally:
        sink.close()

    expected_frame = encode_counts_uint32le((40, 60, 2), expected_channel_count=3)
    with psycopg.connect(integration_database.dsn) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM radiacode_private.raw_buffer_batches),
                (SELECT count(*) FROM radiacode_private.buffer_records),
                (SELECT count(*) FROM radiacode_private.scalar_samples),
                (SELECT count(*) FROM radiacode_private.status_samples),
                (SELECT count(*) FROM radiacode_private.device_events),
                (SELECT count(*) FROM radiacode_private.spectrum_frames),
                (SELECT count(*) FROM radiacode_private.data_gaps),
                (SELECT count(*) FROM radiacode_private.spectrum_snapshots)
            """
        ).fetchone()
        assert counts == (2, 4, 2, 1, 1, 1, 1, 1)
        rolled_back = connection.execute(
            "SELECT count(*) FROM radiacode_private.raw_buffer_batches WHERE batch_id = %s",
            (failed_batch.batch_id,),
        ).fetchone()
        assert rolled_back == (0,)
        runtime = connection.execute(
            """
            SELECT charging, charging_observed_at
              FROM radiacode_private.device_runtime_state
             WHERE device_id = %s
            """,
            (device_id,),
        ).fetchone()
        assert runtime == (True, observed_at)
        frame = connection.execute(
            """
            SELECT counts, sha256, total_count, duration_seconds,
                   octet_length(counts) = channel_count * 4
              FROM radiacode_private.spectrum_frames
            """
        ).fetchone()
        assert frame == (
            expected_frame,
            spectrum_sha256(expected_frame),
            102,
            300,
            True,
        )
        snapshot_metadata = connection.execute(
            """
            SELECT snapshots.calibration_epoch_id IS NOT NULL,
                   snapshots.quality_flags,
                   epochs.started_at = epochs.ended_at,
                   cursor.calibration_epoch_id <> snapshots.calibration_epoch_id,
                   live.calibration_epoch_id = cursor.calibration_epoch_id
              FROM radiacode_private.spectrum_snapshots AS snapshots
              JOIN radiacode_private.calibration_epochs AS epochs
                USING (calibration_epoch_id)
              JOIN radiacode_private.spectrum_cursors AS cursor
                ON cursor.device_id = snapshots.device_id
              JOIN LATERAL (
                  SELECT calibration_epoch_id
                    FROM radiacode_private.calibration_epochs
                   WHERE device_id = snapshots.device_id AND ended_at IS NULL
              ) AS live ON true
            """
        ).fetchone()
        assert snapshot_metadata == (
            True,
            ["audit_only", "spectrum_accum_undocumented"],
            True,
            True,
            True,
        )

    maintenance_dsn = make_conninfo(
        integration_database.dsn,
        options="-c role=radiacode_maintenance",
    )
    maintenance = Maintenance(maintenance_dsn)
    first_report = maintenance.run(today=datetime.now(UTC).date())
    late_received_at = observed_at.replace(second=30, microsecond=0)
    with psycopg.connect(integration_database.dsn) as connection:
        connection.execute(
            """
            INSERT INTO radiacode_private.scalar_samples(
                received_at, batch_id, record_index, device_id,
                timestamp_quality, sample_kind, count_rate, dose_rate
            ) VALUES (%s, %s, 0, %s, 'not_available', 'real_time', 20.0, 0.3)
            """,
            (late_received_at, uuid4(), device_id),
        )
        connection.execute(
            """
            INSERT INTO radiacode_private.scalar_rollup_dirty(device_id, bucket_at)
            VALUES (%s, date_trunc('minute', %s::timestamptz, 'UTC'))
            ON CONFLICT (device_id, bucket_at) DO NOTHING
            """,
            (device_id, late_received_at),
        )
        connection.commit()
        visible_sources = connection.execute(
            """
            SELECT sample_count
              FROM radiacode_api.scalar_minute_history
             WHERE slug = 'rc-integration'
             ORDER BY sample_count DESC
            """
        ).fetchall()
        assert visible_sources == [(1,), (1,)]
    second_report = maintenance.run(today=datetime.now(UTC).date())
    assert first_report.acquired_lock
    assert first_report.scalar_minutes_upserted == 1
    assert first_report.spectrum_rollups_upserted == 2
    assert second_report == first_report
    with psycopg.connect(integration_database.dsn) as connection:
        rollup_counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM radiacode_private.scalar_minute_rollups),
                (SELECT count(*) FROM radiacode_private.spectrum_rollups),
                (SELECT sample_count FROM radiacode_private.scalar_minute_rollups)
            """
        ).fetchone()
        assert rollup_counts == (1, 2, 2)

        connection.execute("SELECT pg_advisory_lock(%s)", (MAINTENANCE_LOCK_ID,))
        try:
            locked_report = maintenance.run(today=datetime.now(UTC).date())
            assert not locked_report.acquired_lock
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MAINTENANCE_LOCK_ID,))


def test_public_repository_uses_reader_role_and_weights_scalar_rollups(
    integration_database: IntegrationDatabase,
) -> None:
    device_id = uuid4()
    slug = "rc-public-reader"
    base = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    with psycopg.connect(integration_database.dsn) as connection:
        connection.execute(
            """
            INSERT INTO radiacode_private.devices(
                device_id, slug, display_name, model, usb_serial
            ) VALUES (%s, %s, 'Public reader integration', 'RC-Test', %s)
            """,
            (device_id, slug, f"private-{uuid4()}"),
        )
        connection.execute(
            """
            INSERT INTO radiacode_private.connections(
                connection_id, device_id, connected_at,
                firmware_major, firmware_minor, app_version
            ) VALUES (%s, %s, %s, 4, 13, 'integration-test')
            """,
            (uuid4(), device_id, base - timedelta(minutes=1)),
        )
        connection.execute(
            """
            INSERT INTO radiacode_private.data_gaps(
                gap_id, device_id, detected_at, gap_kind
            ) VALUES
                (%s, %s, %s, 'data_buf_sequence_gap'),
                (%s, %s, %s, 'count_regression')
            """,
            (
                uuid4(),
                device_id,
                base + timedelta(seconds=1),
                uuid4(),
                device_id,
                base + timedelta(seconds=2),
            ),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO radiacode_private.scalar_minute_rollups(
                    device_id, bucket_at, sample_count,
                    count_rate_min, count_rate_max, count_rate_avg, count_rate_latest,
                    dose_rate_min, dose_rate_max, dose_rate_avg, dose_rate_latest
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    (device_id, base, 1, 10.0, 10.0, 10.0, 10.0, 1.0, 1.0, 1.0, 1.0),
                    (
                        device_id,
                        base + timedelta(minutes=1),
                        3,
                        15.0,
                        25.0,
                        20.0,
                        25.0,
                        2.0,
                        4.0,
                        3.0,
                        4.0,
                    ),
                ),
            )
        connection.commit()

    with _reader_repository(integration_database.dsn) as (repository, reader_dsn):
        assert repository.ping()
        public_device = next(row for row in repository.devices() if row["slug"] == slug)
        assert public_device["firmware_version"] == "4.13"
        public_events = repository.events(
            slug,
            base - timedelta(minutes=2),
            base + timedelta(minutes=2),
            20,
        )
        assert not any(row["code"] == "data_buf_sequence_gap" for row in public_events)
        assert any(
            row["code"] == "count_regression" and row["name"] == "Spectrum acquisition gap"
            for row in public_events
        )
        with psycopg.connect(reader_dsn) as reader_connection:
            assert reader_connection.execute("SELECT current_user").fetchone() is not None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader_connection.execute("SELECT usb_serial FROM radiacode_private.devices")

        rows = repository.scalar_history(
            slug,
            base,
            base + timedelta(minutes=2),
            120,
            use_rollups=True,
        )

    assert len(rows) == 1
    row = rows[0]
    assert row["cps_min"] == 10.0
    assert row["cps_max"] == 25.0
    assert row["cps_avg"] == pytest.approx(17.5)
    assert row["cps_latest"] == 25.0
    assert row["dose_rate_min"] == 1.0
    assert row["dose_rate_max"] == 4.0
    assert row["dose_rate_avg"] == pytest.approx(2.5)
    assert row["dose_rate_latest"] == 4.0


def test_public_repository_hybrid_spectra_are_complete_without_duplicates(
    integration_database: IntegrationDatabase,
) -> None:
    device_id = uuid4()
    calibration_id = uuid4()
    session_id = uuid4()
    slug = "rc-public-hybrid"
    base = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)

    def insert_frame(
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        ended_at: datetime,
        counts: tuple[int, ...],
    ) -> None:
        encoded = encode_counts_uint32le(counts, expected_channel_count=3)
        connection.execute(
            """
            INSERT INTO radiacode_private.spectrum_frames(
                frame_id, device_id, session_id, calibration_epoch_id,
                started_at, ended_at, duration_seconds, channel_count,
                counts_encoding_version, counts, total_count, sha256,
                source_intervals
            ) VALUES (%s, %s, %s, %s, %s, %s, 300, 3, 1, %s, %s, %s, 1)
            """,
            (
                uuid4(),
                device_id,
                session_id,
                calibration_id,
                ended_at - timedelta(minutes=5),
                ended_at,
                encoded,
                sum(counts),
                spectrum_sha256(encoded),
            ),
        )

    def insert_rollup(
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        bucket_at: datetime,
        actual_started_at: datetime,
        actual_ended_at: datetime,
        counts: tuple[int, ...],
        source_frame_count: int,
    ) -> None:
        encoded = encode_counts_uint32le(counts, expected_channel_count=3)
        connection.execute(
            """
            INSERT INTO radiacode_private.spectrum_rollups(
                device_id, calibration_epoch_id, resolution, bucket_at,
                actual_started_at, actual_ended_at, duration_seconds,
                channel_count, counts_encoding_version, counts, total_count,
                sha256, source_frame_count
            ) VALUES (%s, %s, 'hour', %s, %s, %s, %s, 3, 1, %s, %s, %s, %s)
            """,
            (
                device_id,
                calibration_id,
                bucket_at,
                actual_started_at,
                actual_ended_at,
                source_frame_count * 300,
                encoded,
                sum(counts),
                spectrum_sha256(encoded),
                source_frame_count,
            ),
        )

    with psycopg.connect(integration_database.dsn) as connection:
        connection.execute(
            """
            INSERT INTO radiacode_private.devices(
                device_id, slug, display_name, model, usb_serial
            ) VALUES (%s, %s, 'Hybrid integration', 'RC-Test', %s)
            """,
            (device_id, slug, f"private-{uuid4()}"),
        )
        connection.execute(
            """
            INSERT INTO radiacode_private.calibration_epochs(
                calibration_epoch_id, device_id, started_at, channel_count,
                coefficient_a0, coefficient_a1, coefficient_a2, fingerprint
            ) VALUES (%s, %s, %s, 3, 0, 1, 0, %s)
            """,
            (calibration_id, device_id, base - timedelta(days=1), b"h" * 32),
        )
        connection.execute(
            """
            INSERT INTO radiacode_private.spectrum_sessions(
                session_id, device_id, calibration_epoch_id, started_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (session_id, device_id, calibration_id, base - timedelta(minutes=5)),
        )

        # A complete rollup replaces both source frames exactly once.
        insert_frame(connection, ended_at=base + timedelta(minutes=10), counts=(1, 2, 0))
        insert_frame(connection, ended_at=base + timedelta(minutes=20), counts=(3, 4, 0))
        insert_rollup(
            connection,
            bucket_at=base,
            actual_started_at=base + timedelta(minutes=5),
            actual_ended_at=base + timedelta(minutes=20),
            counts=(4, 6, 0),
            source_frame_count=2,
        )

        # A missing rollup falls back to its immutable source frame.
        insert_frame(
            connection,
            ended_at=base + timedelta(hours=1, minutes=10),
            counts=(5, 6, 0),
        )

        # This rollup became stale when a second frame committed later. The
        # public query must ignore the stale aggregate and return both raw rows.
        insert_frame(
            connection,
            ended_at=base + timedelta(hours=2, minutes=10),
            counts=(7, 8, 0),
        )
        insert_rollup(
            connection,
            bucket_at=base + timedelta(hours=2),
            actual_started_at=base + timedelta(hours=2, minutes=5),
            actual_ended_at=base + timedelta(hours=2, minutes=10),
            counts=(7, 8, 0),
            source_frame_count=1,
        )
        insert_frame(
            connection,
            ended_at=base + timedelta(hours=2, minutes=20),
            counts=(9, 10, 0),
        )

        # The most recently closed bucket stays on raw frames even if a stale
        # rollup raced with the API request boundary.
        insert_frame(
            connection,
            ended_at=base + timedelta(hours=4, minutes=10),
            counts=(11, 12, 0),
        )
        insert_rollup(
            connection,
            bucket_at=base + timedelta(hours=4),
            actual_started_at=base + timedelta(hours=4, minutes=5),
            actual_ended_at=base + timedelta(hours=4, minutes=10),
            counts=(99, 99, 0),
            source_frame_count=1,
        )
        connection.commit()

    with _reader_repository(integration_database.dsn) as (repository, _reader_dsn):
        rows = repository.spectra(
            (slug,),
            base,
            base + timedelta(hours=5),
            resolution="hour",
        )

    assert [tuple(row.counts) for row in rows] == [
        (4, 6, 0),
        (5, 6, 0),
        (7, 8, 0),
        (9, 10, 0),
        (11, 12, 0),
    ]


def test_spool_replays_batch_older_than_two_days(
    integration_database: IntegrationDatabase,
    tmp_path: Path,
) -> None:
    slug = "rc-old-spool"
    serial = f"private-{uuid4()}"
    writer_dsn = make_conninfo(
        integration_database.dsn,
        options="-c role=radiacode_writer",
    )
    sink = PostgresSink(
        writer_dsn,
        device_slug=slug,
        device_serial=serial,
        app_version="spool-integration-test",
    )
    connection_id = uuid4()
    received_at = datetime.now(UTC) - timedelta(days=7)
    payload = _wire_record(
        17,
        0,
        0,
        100,
        struct.pack("<ffHHHB", 12.5, 0.25, 31, 42, 0x1234, 0x56),
    )
    batch = RawBatch(
        batch_id=uuid4(),
        device_slug=slug,
        connection_id=connection_id,
        received_at=received_at,
        payload=payload,
        sha256=spectrum_sha256(payload),
    )
    spool = SQLiteSpool(tmp_path / "spool.sqlite3", max_bytes=1_000_000)
    try:
        sink.ensure_device()
        sink.record_connection_open(connection_id, received_at)
        spool.append(batch)
        collector = Collector(
            Settings(device_slug=slug, device_serial=SecretStr(serial)),
            sink,
            spool,
        )

        assert collector.drain_spool()
        assert spool.pending_count() == 0
        assert sink.load_expected_sequence() == 18
        with psycopg.connect(integration_database.dsn) as connection:
            persisted = connection.execute(
                """
                SELECT raw.received_at, scalar.count_rate
                  FROM radiacode_private.raw_buffer_batches AS raw
                  JOIN radiacode_private.scalar_samples AS scalar
                    ON scalar.received_at = raw.received_at
                   AND scalar.batch_id = raw.batch_id
                 WHERE raw.batch_id = %s
                """,
                (batch.batch_id,),
            ).fetchone()
        assert persisted == (received_at, 12.5)
    finally:
        spool.close()
        sink.close()
