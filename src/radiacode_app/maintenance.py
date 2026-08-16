from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .spectrum import UINT32_MAX, decode_counts_uint32le, encode_counts_uint32le, spectrum_sha256

MAINTENANCE_LOCK_ID = 7_243_944_686_731_002_002


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    acquired_lock: bool
    scalar_minutes_upserted: int = 0
    spectrum_rollups_upserted: int = 0
    partitions_dropped: int = 0


@dataclass(frozen=True, slots=True)
class _FrameRow:
    device_id: Any
    calibration_epoch_id: Any
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    channel_count: int
    counts: tuple[int, ...]
    quality_flags: tuple[str, ...]


def _bucket(timestamp: datetime, resolution: str) -> datetime:
    utc = timestamp.astimezone(UTC)
    if resolution == "hour":
        return utc.replace(minute=0, second=0, microsecond=0)
    if resolution == "day":
        return utc.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("unsupported resolution")


def _segment_frames(frames: list[_FrameRow]) -> list[list[_FrameRow]]:
    """Greedily keep every persisted rollup segment within uint32 encoding v1."""

    if not frames:
        raise ValueError("a rollup requires at least one frame")
    segments: list[list[_FrameRow]] = []
    current: list[_FrameRow] = []
    current_counts: list[int] = []
    for frame in frames:
        if not current:
            current = [frame]
            current_counts = list(frame.counts)
            continue
        if any(left + right > UINT32_MAX for left, right in zip(current_counts, frame.counts, strict=True)):
            segments.append(current)
            current = [frame]
            current_counts = list(frame.counts)
            continue
        current.append(frame)
        current_counts = [left + right for left, right in zip(current_counts, frame.counts, strict=True)]
    segments.append(current)
    return segments


class Maintenance:
    def __init__(
        self,
        dsn: str,
        *,
        retention_days: int = 30,
        advisory_lock_id: int = MAINTENANCE_LOCK_ID,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        self._dsn = dsn
        self.retention_days = retention_days
        self.advisory_lock_id = advisory_lock_id

    def run(self, *, today: date | None = None) -> MaintenanceReport:
        import psycopg

        effective_today = today or datetime.now(UTC).date()
        with psycopg.connect(self._dsn, autocommit=False) as connection:
            lock_row = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (self.advisory_lock_id,)
            ).fetchone()
            if lock_row is None or not lock_row[0]:
                return MaintenanceReport(acquired_lock=False)
            try:
                with connection.transaction():
                    connection.execute(
                        "SELECT radiacode_private.ensure_daily_partitions(%s, %s)",
                        (
                            effective_today - timedelta(days=self.retention_days),
                            effective_today + timedelta(days=8),
                        ),
                    )
                    scalar_count = self._roll_up_scalars(connection)
                    spectrum_count = self._roll_up_spectra(connection)
                    dropped_row = connection.execute(
                        "SELECT radiacode_private.drop_daily_partitions_before(%s)",
                        (effective_today - timedelta(days=self.retention_days),),
                    ).fetchone()
                    if dropped_row is None:
                        raise RuntimeError("partition maintenance returned no result")
                    dropped = int(dropped_row[0])
                return MaintenanceReport(
                    acquired_lock=True,
                    scalar_minutes_upserted=scalar_count,
                    spectrum_rollups_upserted=spectrum_count,
                    partitions_dropped=dropped,
                )
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (self.advisory_lock_id,))
                connection.commit()

    def _roll_up_scalars(self, connection: Any) -> int:
        database_now_row = connection.execute("SELECT clock_timestamp()").fetchone()
        if database_now_row is None:
            raise RuntimeError("database clock returned no result")
        database_now = database_now_row[0]
        complete_before = database_now.replace(second=0, microsecond=0) - timedelta(minutes=2)
        cursor = connection.execute(
            """
            WITH affected AS (
                DELETE FROM radiacode_private.scalar_rollup_dirty
                 WHERE bucket_at < %s
                RETURNING device_id, bucket_at
            ), ranked AS (
                SELECT samples.device_id,
                       affected.bucket_at,
                       samples.received_at,
                       count_rate,
                       dose_rate,
                       row_number() OVER (
                           PARTITION BY samples.device_id, affected.bucket_at
                           ORDER BY samples.received_at DESC, samples.record_index DESC
                       ) AS newest
                  FROM affected
                  JOIN radiacode_private.scalar_samples AS samples
                    ON samples.device_id = affected.device_id
                   AND samples.received_at >= affected.bucket_at
                   AND samples.received_at < affected.bucket_at + interval '1 minute'
                 WHERE samples.sample_kind = 'real_time'
            ), aggregated AS (
                SELECT device_id, bucket_at, count(*)::integer AS sample_count,
                       min(count_rate) AS count_rate_min,
                       max(count_rate) AS count_rate_max,
                       avg(count_rate) AS count_rate_avg,
                       max(count_rate) FILTER (WHERE newest = 1) AS count_rate_latest,
                       min(dose_rate) AS dose_rate_min,
                       max(dose_rate) AS dose_rate_max,
                       avg(dose_rate) AS dose_rate_avg,
                       max(dose_rate) FILTER (WHERE newest = 1) AS dose_rate_latest
                  FROM ranked
                 GROUP BY device_id, bucket_at
            )
            INSERT INTO radiacode_private.scalar_minute_rollups(
                device_id, bucket_at, sample_count,
                count_rate_min, count_rate_max, count_rate_avg, count_rate_latest,
                dose_rate_min, dose_rate_max, dose_rate_avg, dose_rate_latest
            )
            SELECT device_id, bucket_at, sample_count,
                   count_rate_min, count_rate_max, count_rate_avg, count_rate_latest,
                   dose_rate_min, dose_rate_max, dose_rate_avg, dose_rate_latest
              FROM aggregated
            ON CONFLICT (device_id, bucket_at) DO UPDATE SET
                sample_count = EXCLUDED.sample_count,
                count_rate_min = EXCLUDED.count_rate_min,
                count_rate_max = EXCLUDED.count_rate_max,
                count_rate_avg = EXCLUDED.count_rate_avg,
                count_rate_latest = EXCLUDED.count_rate_latest,
                dose_rate_min = EXCLUDED.dose_rate_min,
                dose_rate_max = EXCLUDED.dose_rate_max,
                dose_rate_avg = EXCLUDED.dose_rate_avg,
                dose_rate_latest = EXCLUDED.dose_rate_latest,
                rolled_at = clock_timestamp()
            """,
            (complete_before,),
        )
        upserted = max(0, int(cursor.rowcount))
        connection.execute(
            """
            INSERT INTO radiacode_private.rollup_watermarks(resolution, processed_before)
            VALUES ('minute', %s)
            ON CONFLICT (resolution) DO UPDATE
                SET processed_before = EXCLUDED.processed_before
            """,
            (complete_before,),
        )
        return upserted

    def _roll_up_spectra(self, connection: Any) -> int:
        now = datetime.now(UTC)
        upserted = 0
        for resolution in ("hour", "day"):
            boundary = _bucket(now, resolution)
            watermark_row = connection.execute(
                """
                SELECT processed_before
                  FROM radiacode_private.rollup_watermarks
                 WHERE resolution = %s
                """,
                (resolution,),
            ).fetchone()
            lower = watermark_row[0] if watermark_row is not None else None

            # A first run streams the historical backfill. Later runs revisit
            # only buckets that were open at the previous watermark. Spectrum
            # frames are immutable and arrive in host-time order, so older
            # closed buckets never need a full-table rescan.
            if lower is None or lower < boundary:
                upserted += self._stream_spectrum_rollups(
                    connection,
                    resolution=resolution,
                    start=lower,
                    end=boundary,
                    cursor_suffix="closed",
                )
            safety_delta = timedelta(hours=1) if resolution == "hour" else timedelta(days=1)
            connection.execute(
                """
                INSERT INTO radiacode_private.rollup_watermarks(resolution, processed_before)
                VALUES (%s, %s)
                ON CONFLICT (resolution) DO UPDATE
                    SET processed_before = EXCLUDED.processed_before
                """,
                (resolution, boundary - safety_delta),
            )

            # Refresh just the bounded, currently-open wall-clock bucket so the
            # dashboard does not wait for the next hour/day boundary.
            upserted += self._stream_spectrum_rollups(
                connection,
                resolution=resolution,
                start=boundary,
                end=now,
                cursor_suffix="open",
            )
        return upserted

    def _stream_spectrum_rollups(
        self,
        connection: Any,
        *,
        resolution: str,
        start: datetime | None,
        end: datetime,
        cursor_suffix: str,
    ) -> int:
        conditions = ["duration_seconds > 0", "ended_at < %s"]
        parameters: list[object] = [end]
        if start is not None:
            conditions.append("ended_at >= %s")
            parameters.append(start)
        query = f"""
            SELECT device_id, calibration_epoch_id, started_at, ended_at,
                   duration_seconds, channel_count, counts, quality_flags
              FROM radiacode_private.spectrum_frames
             WHERE {" AND ".join(conditions)}
             ORDER BY device_id, calibration_epoch_id, ended_at, frame_id
        """
        upserted = 0
        current_key: tuple[Any, Any, datetime] | None = None
        current_frames: list[_FrameRow] = []
        with connection.cursor(name=f"radiacode_{resolution}_{cursor_suffix}") as cursor:
            cursor.itersize = 256
            cursor.execute(query, parameters)
            for row in cursor:
                frame = _FrameRow(
                    device_id=row[0],
                    calibration_epoch_id=row[1],
                    started_at=row[2],
                    ended_at=row[3],
                    duration_seconds=int(row[4]),
                    channel_count=int(row[5]),
                    counts=decode_counts_uint32le(bytes(row[6]), int(row[5])),
                    quality_flags=tuple(row[7] or ()),
                )
                key = (
                    frame.device_id,
                    frame.calibration_epoch_id,
                    _bucket(frame.ended_at, resolution),
                )
                if current_key is not None and key != current_key:
                    upserted += self._upsert_spectrum_rollups(
                        connection,
                        resolution=resolution,
                        bucket_at=current_key[2],
                        frames=current_frames,
                    )
                    current_frames = []
                current_key = key
                current_frames.append(frame)
        if current_key is not None:
            upserted += self._upsert_spectrum_rollups(
                connection,
                resolution=resolution,
                bucket_at=current_key[2],
                frames=current_frames,
            )
        return upserted

    def _upsert_spectrum_rollups(
        self,
        connection: Any,
        *,
        resolution: str,
        bucket_at: datetime,
        frames: list[_FrameRow],
    ) -> int:
        from psycopg.types.json import Jsonb

        first = frames[0]
        channel_count = first.channel_count
        if any(
            frame.device_id != first.device_id
            or frame.calibration_epoch_id != first.calibration_epoch_id
            or frame.channel_count != channel_count
            for frame in frames
        ):
            raise ValueError("rollup group contains incompatible spectrum frames")
        segments = _segment_frames(frames)
        for segment_index, segment in enumerate(segments):
            counts = tuple(sum(frame.counts[index] for frame in segment) for index in range(channel_count))
            quality_flags = tuple(sorted({flag for frame in segment for flag in frame.quality_flags}))
            encoded = encode_counts_uint32le(counts, expected_channel_count=channel_count)
            connection.execute(
                """
                INSERT INTO radiacode_private.spectrum_rollups(
                    device_id, calibration_epoch_id, resolution, bucket_at,
                    actual_started_at, actual_ended_at, duration_seconds,
                    channel_count, counts_encoding_version, counts, total_count,
                    sha256, source_frame_count, quality_flags, bucket_assignment,
                    segment_index
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 1,
                    %s, %s, %s, %s, %s, 'frame_end', %s
                )
                ON CONFLICT (
                    device_id, calibration_epoch_id, resolution, bucket_at, segment_index
                ) DO UPDATE SET
                    actual_started_at = EXCLUDED.actual_started_at,
                    actual_ended_at = EXCLUDED.actual_ended_at,
                    duration_seconds = EXCLUDED.duration_seconds,
                    channel_count = EXCLUDED.channel_count,
                    counts_encoding_version = EXCLUDED.counts_encoding_version,
                    counts = EXCLUDED.counts,
                    total_count = EXCLUDED.total_count,
                    sha256 = EXCLUDED.sha256,
                    source_frame_count = EXCLUDED.source_frame_count,
                    quality_flags = EXCLUDED.quality_flags,
                    bucket_assignment = EXCLUDED.bucket_assignment
                """,
                (
                    first.device_id,
                    first.calibration_epoch_id,
                    resolution,
                    bucket_at,
                    min(frame.started_at for frame in segment),
                    max(frame.ended_at for frame in segment),
                    sum(frame.duration_seconds for frame in segment),
                    channel_count,
                    encoded,
                    sum(counts),
                    spectrum_sha256(encoded),
                    len(segment),
                    Jsonb(quality_flags),
                    segment_index,
                ),
            )
        connection.execute(
            """
            DELETE FROM radiacode_private.spectrum_rollups
             WHERE device_id = %s
               AND calibration_epoch_id = %s
               AND resolution = %s
               AND bucket_at = %s
               AND segment_index >= %s
            """,
            (
                first.device_id,
                first.calibration_epoch_id,
                resolution,
                bucket_at,
                len(segments),
            ),
        )
        return len(segments)
