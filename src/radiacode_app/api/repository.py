from __future__ import annotations

import sys
from array import array
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from radiacode_app.spectrum import SpectrumEncodingError

from .ranges import PublicRequestError

MAX_PUBLIC_SPECTRUM_SOURCE_ROWS = 4_096


@dataclass(frozen=True, slots=True)
class SpectrumRow:
    device: str
    model: str
    calibration_epoch: str
    calibration_started_at: datetime
    start_at: datetime
    end_at: datetime
    duration_seconds: int
    channel_count: int
    calibration: tuple[float, float, float]
    counts: Sequence[int]
    quality_flags: tuple[str, ...]


def _resolution_floor(timestamp: datetime, resolution: str) -> datetime:
    value = timestamp.astimezone(UTC)
    if resolution == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    if resolution == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("unsupported spectrum resolution")


def _resolution_delta(resolution: str) -> timedelta:
    if resolution == "hour":
        return timedelta(hours=1)
    if resolution == "day":
        return timedelta(days=1)
    raise ValueError("unsupported spectrum resolution")


def _resolution_ceil(timestamp: datetime, resolution: str) -> datetime:
    floor = _resolution_floor(timestamp, resolution)
    return floor if timestamp.astimezone(UTC) == floor else floor + _resolution_delta(resolution)


class PublicRepository:
    """Queries only sanitized radiacode_api views granted to the web role."""

    def __init__(self, dsn: str, *, max_size: int = 10) -> None:
        self._pool = ConnectionPool[Connection[dict[str, Any]]](
            conninfo=dsn,
            min_size=0,
            max_size=max_size,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "options": ("-c statement_timeout=15000 -c idle_in_transaction_session_timeout=15000"),
            },
        )

    def open(self) -> None:
        self._pool.open(wait=True)

    def close(self) -> None:
        self._pool.close()

    def ping(self) -> bool:
        with self._pool.connection() as connection:
            connection.execute("SELECT 1 FROM radiacode_api.device_status LIMIT 0")
            return True

    def devices(self) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            return list(
                connection.execute(
                    """
                    SELECT slug, display_name AS name, model, firmware_version, last_seen_at
                      FROM radiacode_api.device_status
                     ORDER BY display_name, slug
                    """
                ).fetchall()
            )

    def current(self, slug: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            return connection.execute(
                "SELECT * FROM radiacode_api.device_status WHERE slug = %s",
                (slug,),
            ).fetchone()

    def current_states(self) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            return list(
                connection.execute(
                    """
                    SELECT *
                      FROM radiacode_api.device_status
                     ORDER BY display_name, slug
                    """
                ).fetchall()
            )

    def scalar_history(
        self,
        slug: str,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
        *,
        use_rollups: bool,
    ) -> list[dict[str, Any]]:
        view = "scalar_minute_history" if use_rollups else "scalar_history"
        with self._pool.connection() as connection:
            return list(
                connection.execute(
                    f"""
                    WITH bucketed AS (
                        SELECT to_timestamp(
                                   floor(extract(epoch FROM observed_at) / %s) * %s
                               ) AS bucket_at,
                               cps_min, cps_max, cps_avg, cps_latest,
                               dose_rate_min, dose_rate_max, dose_rate_avg, dose_rate_latest,
                               sample_count, observed_at
                          FROM radiacode_api.{view}
                         WHERE slug = %s AND observed_at >= %s AND observed_at < %s
                    )
                    SELECT bucket_at AS at,
                           min(cps_min) AS cps_min,
                           max(cps_max) AS cps_max,
                           sum(cps_avg * sample_count)
                               / NULLIF(sum(sample_count)
                                   FILTER (WHERE cps_avg IS NOT NULL), 0)
                               AS cps_avg,
                           (array_agg(cps_latest ORDER BY observed_at DESC)
                               FILTER (WHERE cps_latest IS NOT NULL))[1] AS cps_latest,
                           min(dose_rate_min) AS dose_rate_min,
                           max(dose_rate_max) AS dose_rate_max,
                           sum(dose_rate_avg * sample_count)
                               / NULLIF(sum(sample_count) FILTER (WHERE dose_rate_avg IS NOT NULL), 0)
                               AS dose_rate_avg,
                           (array_agg(dose_rate_latest ORDER BY observed_at DESC)
                               FILTER (WHERE dose_rate_latest IS NOT NULL))[1] AS dose_rate_latest
                      FROM bucketed
                     GROUP BY bucket_at
                     ORDER BY bucket_at
                     LIMIT 2000
                    """,
                    (bucket_seconds, bucket_seconds, slug, start, end),
                ).fetchall()
            )

    def events(self, slug: str, start: datetime, end: datetime, limit: int) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            return list(
                connection.execute(
                    """
        SELECT observed_at AS at, code, name, parameter
                      FROM radiacode_api.events
                     WHERE slug = %s AND observed_at >= %s AND observed_at < %s
                     ORDER BY observed_at DESC
                     LIMIT %s
                    """,
                    (slug, start, end, limit),
                ).fetchall()
            )

    @contextmanager
    def spectrum_frame_export(
        self,
        slugs: tuple[str, ...],
        start: datetime,
        end: datetime,
    ) -> Iterator[tuple[int, Iterator[SpectrumRow]]]:
        with self._pool.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = connection.execute(
                """
                SELECT count(*)
                  FROM radiacode_api.spectrum_frames
                 WHERE slug = ANY(%s) AND end_at >= %s AND end_at < %s
                """,
                (list(slugs), start, end),
            ).fetchone()
            count = int(row["count"] if row is not None else 0)

            def rows() -> Iterator[SpectrumRow]:
                with connection.cursor(name="radiacode_export_frames") as cursor:
                    cursor.itersize = 16
                    cursor.execute(
                        """
                        SELECT slug, model, calibration_epoch, calibration_started_at,
                               start_at, end_at, duration_seconds, channel_count,
                               coefficient_a0, coefficient_a1, coefficient_a2,
                               counts, quality_flags
                          FROM radiacode_api.spectrum_frames
                         WHERE slug = ANY(%s) AND end_at >= %s AND end_at < %s
                         ORDER BY end_at, slug, calibration_epoch
                        """,
                        (list(slugs), start, end),
                    )
                    for spectrum_row in cursor:
                        yield self._spectrum_row(spectrum_row)

            yield count, rows()

    def spectra(
        self,
        slugs: tuple[str, ...],
        start: datetime,
        end: datetime,
        *,
        resolution: str = "frame",
        limit: int = MAX_PUBLIC_SPECTRUM_SOURCE_ROWS,
        latest: bool = False,
    ) -> list[SpectrumRow]:
        if resolution not in {"frame", "hour", "day"}:
            raise ValueError("unsupported spectrum resolution")
        if limit < 1:
            raise ValueError("spectrum source limit must be positive")
        if latest and resolution != "frame":
            raise ValueError("latest spectrum queries must use frames")
        if resolution == "frame":
            return self._query_spectra(
                slugs,
                view="spectrum_frames",
                time_clause="end_at >= %s AND end_at < %s",
                time_parameters=(start, end),
                resolution=None,
                limit=limit,
                latest=latest,
            )

        delta = _resolution_delta(resolution)
        complete_start = _resolution_ceil(start, resolution)
        complete_end = _resolution_floor(end, resolution)
        # Keep the most recently closed bucket on raw immutable frames. This
        # closes the small maintenance-watermark race without sacrificing
        # bounded long-range queries.
        rollup_end = max(complete_start, complete_end - delta)
        rows: list[SpectrumRow] = []
        if complete_start < rollup_end:
            rows.extend(
                self._query_rollups_with_fallback(
                    slugs,
                    resolution=resolution,
                    start=complete_start,
                    end=rollup_end,
                    limit=limit,
                )
            )

        raw_ranges: list[tuple[datetime, datetime]] = []

        def add_raw_range(range_start: datetime, range_end: datetime) -> None:
            if range_start >= range_end:
                return
            if raw_ranges and range_start <= raw_ranges[-1][1]:
                previous_start, previous_end = raw_ranges[-1]
                raw_ranges[-1] = (previous_start, max(previous_end, range_end))
            else:
                raw_ranges.append((range_start, range_end))

        add_raw_range(start, min(complete_start, end))
        add_raw_range(max(rollup_end, start), end)
        for range_start, range_end in raw_ranges:
            remaining = max(0, limit - len(rows))
            rows.extend(
                self._query_spectra(
                    slugs,
                    view="spectrum_frames",
                    time_clause="end_at >= %s AND end_at < %s",
                    time_parameters=(range_start, range_end),
                    resolution=None,
                    limit=remaining,
                    latest=False,
                )
            )
        rows.sort(key=lambda row: (row.end_at, row.device, row.calibration_epoch))
        return rows

    def _query_rollups_with_fallback(
        self,
        slugs: tuple[str, ...],
        *,
        resolution: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[SpectrumRow]:
        """Read complete rollups and raw frames for any missing bucket atomically."""

        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                WITH rollup_groups AS (
                    SELECT slug, calibration_epoch, bucket_at,
                           sum(source_frame_count) AS source_frame_count
                      FROM radiacode_api.spectrum_rollups
                     WHERE slug = ANY(%s) AND resolution = %s
                       AND bucket_at >= %s AND bucket_at < %s
                     GROUP BY slug, calibration_epoch, bucket_at
                ), frame_rows AS (
                    SELECT slug, calibration_epoch,
                           date_trunc(%s, end_at, 'UTC') AS bucket_at
                      FROM radiacode_api.spectrum_frames
                     WHERE slug = ANY(%s) AND end_at >= %s AND end_at < %s
                ), frame_groups AS (
                    SELECT slug, calibration_epoch, bucket_at,
                           count(*) AS source_frame_count
                      FROM frame_rows
                     GROUP BY slug, calibration_epoch, bucket_at
                ), complete_groups AS (
                    SELECT rollups.slug, rollups.calibration_epoch, rollups.bucket_at
                      FROM rollup_groups AS rollups
                      JOIN frame_groups AS frames
                        USING (slug, calibration_epoch, bucket_at)
                     WHERE rollups.source_frame_count = frames.source_frame_count
                ), selected AS (
                    SELECT rollups.slug, rollups.model, rollups.calibration_epoch,
                           rollups.calibration_started_at, rollups.start_at, rollups.end_at,
                           rollups.duration_seconds, rollups.channel_count,
                           rollups.coefficient_a0, rollups.coefficient_a1,
                           rollups.coefficient_a2, rollups.counts, rollups.quality_flags
                      FROM radiacode_api.spectrum_rollups AS rollups
                      JOIN complete_groups AS complete
                        USING (slug, calibration_epoch, bucket_at)
                     WHERE rollups.slug = ANY(%s) AND rollups.resolution = %s
                       AND rollups.bucket_at >= %s AND rollups.bucket_at < %s
                    UNION ALL
                    SELECT frames.slug, frames.model, frames.calibration_epoch,
                           frames.calibration_started_at, frames.start_at, frames.end_at,
                           frames.duration_seconds, frames.channel_count,
                           frames.coefficient_a0, frames.coefficient_a1,
                           frames.coefficient_a2, frames.counts, frames.quality_flags
                      FROM radiacode_api.spectrum_frames AS frames
                     WHERE frames.slug = ANY(%s)
                       AND frames.end_at >= %s
                       AND frames.end_at < %s
                       AND NOT EXISTS (
                           SELECT 1
                             FROM complete_groups AS complete
                            WHERE complete.slug = frames.slug
                              AND complete.calibration_epoch = frames.calibration_epoch
                              AND complete.bucket_at = date_trunc(%s, frames.end_at, 'UTC')
                       )
                )
                SELECT *
                  FROM selected
                 ORDER BY end_at, slug, calibration_epoch
                 LIMIT %s
                """,
                (
                    list(slugs),
                    resolution,
                    start,
                    end,
                    resolution,
                    list(slugs),
                    start,
                    end,
                    list(slugs),
                    resolution,
                    start,
                    end,
                    list(slugs),
                    start,
                    end,
                    resolution,
                    limit + 1,
                ),
            ).fetchall()
        return self._materialize_spectrum_rows(rows, max_rows=limit)

    def _query_spectra(
        self,
        slugs: tuple[str, ...],
        *,
        view: str,
        time_clause: str,
        time_parameters: tuple[object, ...],
        resolution: str | None,
        limit: int,
        latest: bool,
    ) -> list[SpectrumRow]:
        resolution_clause = "" if resolution is None else "AND resolution = %s"
        parameters: list[object] = [list(slugs), *time_parameters]
        if resolution is not None:
            parameters.append(resolution)
        parameters.append(limit if latest else limit + 1)
        ordering = "end_at DESC, slug, calibration_epoch" if latest else "end_at, slug, calibration_epoch"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT slug, model, calibration_epoch, calibration_started_at, start_at, end_at,
                       duration_seconds, channel_count, coefficient_a0,
                       coefficient_a1, coefficient_a2, counts, quality_flags
                  FROM radiacode_api.{view}
                 WHERE slug = ANY(%s) AND {time_clause}
                       {resolution_clause}
                 ORDER BY {ordering}
                 LIMIT %s
                """,
                tuple(parameters),
            ).fetchall()
        return self._materialize_spectrum_rows(rows, max_rows=None if latest else limit)

    @classmethod
    def _materialize_spectrum_rows(
        cls,
        rows: Sequence[dict[str, Any]],
        *,
        max_rows: int | None,
    ) -> list[SpectrumRow]:
        # Reject before expanding every bytea into thousands of Python integer
        # objects. The query itself fetches at most max_rows + 1 compact rows.
        if max_rows is not None and len(rows) > max_rows:
            raise PublicRequestError("spectrum selection is too large")
        return [cls._spectrum_row(row) for row in rows]

    @staticmethod
    def _spectrum_row(row: dict[str, Any]) -> SpectrumRow:
        return SpectrumRow(
            device=row["slug"],
            model=row["model"],
            calibration_epoch=row["calibration_epoch"],
            calibration_started_at=row["calibration_started_at"],
            start_at=row["start_at"],
            end_at=row["end_at"],
            duration_seconds=row["duration_seconds"],
            channel_count=row["channel_count"],
            calibration=(
                row["coefficient_a0"],
                row["coefficient_a1"],
                row["coefficient_a2"],
            ),
            counts=_decode_counts_compact(bytes(row["counts"]), row["channel_count"]),
            quality_flags=tuple(row["quality_flags"] or ()),
        )


def _decode_counts_compact(data: bytes, channel_count: int) -> Sequence[int]:
    expected = channel_count * 4
    if channel_count < 1 or len(data) != expected:
        raise SpectrumEncodingError(
            f"encoded length {len(data)} does not equal channel_count * 4 ({expected})"
        )
    counts = array("I")
    counts.frombytes(data)
    if counts.itemsize != 4:
        raise SpectrumEncodingError("platform unsigned integer width is not four bytes")
    if sys.byteorder != "little":
        counts.byteswap()
    return counts
