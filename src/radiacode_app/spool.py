from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from .models import RawBatch
from .spectrum import spectrum_sha256


class SpoolError(RuntimeError):
    pass


class SpoolCapacityError(SpoolError):
    pass


@dataclass(frozen=True, slots=True)
class PendingBatch:
    batch: RawBatch
    attempts: int
    last_error_class: str | None


class SQLiteSpool:
    """Durable FIFO spool for already-consumed device batches.

    `append` commits under `synchronous=FULL` before it returns. Decoding must not
    begin until that return. Acknowledgement is a separate transaction performed
    only after the corresponding PostgreSQL transaction commits.
    """

    def __init__(self, path: Path, *, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, isolation_level=None, timeout=30.0)
        os.chmod(path, 0o600)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA wal_autocheckpoint=1000")
        self._connection.execute("PRAGMA trusted_schema=OFF")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS spool_batches (
                batch_id TEXT PRIMARY KEY,
                device_slug TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload BLOB NOT NULL,
                sha256 BLOB NOT NULL CHECK(length(sha256) = 32),
                expected_sequence_before INTEGER,
                enqueued_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error_class TEXT
            ) STRICT
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS spool_batches_fifo ON spool_batches(enqueued_at, batch_id)"
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteSpool:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def payload_bytes(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(sum(length(payload)), 0) FROM spool_batches"
        ).fetchone()
        return int(row[0])

    def pending_count(self) -> int:
        row = self._connection.execute("SELECT count(*) FROM spool_batches").fetchone()
        return int(row[0])

    def ensure_capacity(self, reserve_bytes: int) -> None:
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes cannot be negative")
        used = self.payload_bytes()
        if used + reserve_bytes > self.max_bytes:
            raise SpoolCapacityError(
                f"spool capacity guard failed: used={used}, reserve={reserve_bytes}, max={self.max_bytes}"
            )

    def append(self, batch: RawBatch) -> None:
        if batch.received_at.tzinfo is None or batch.received_at.utcoffset() is None:
            raise ValueError("batch received_at must be timezone-aware")
        if batch.sha256 != spectrum_sha256(batch.payload):
            raise SpoolError("batch checksum does not match payload")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            used = int(
                self._connection.execute(
                    "SELECT COALESCE(sum(length(payload)), 0) FROM spool_batches"
                ).fetchone()[0]
            )
            if used + len(batch.payload) > self.max_bytes:
                raise SpoolCapacityError(
                    f"spool capacity exceeded: used={used}, batch={len(batch.payload)}, max={self.max_bytes}"
                )
            self._connection.execute(
                """
                INSERT INTO spool_batches(
                    batch_id, device_slug, connection_id, received_at, payload,
                    sha256, expected_sequence_before
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(batch.batch_id),
                    batch.device_slug,
                    str(batch.connection_id),
                    batch.received_at.isoformat(),
                    batch.payload,
                    batch.sha256,
                    batch.expected_sequence_before,
                ),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def pending(self, *, limit: int | None = None) -> tuple[PendingBatch, ...]:
        sql = """
            SELECT batch_id, device_slug, connection_id, received_at, payload,
                   sha256, expected_sequence_before, attempts, last_error_class
            FROM spool_batches
            ORDER BY enqueued_at, batch_id
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            sql += " LIMIT ?"
            parameters = (limit,)
        rows = self._connection.execute(sql, parameters).fetchall()
        result: list[PendingBatch] = []
        for row in rows:
            batch = RawBatch(
                batch_id=UUID(row[0]),
                device_slug=row[1],
                connection_id=UUID(row[2]),
                received_at=datetime.fromisoformat(row[3]),
                payload=bytes(row[4]),
                sha256=bytes(row[5]),
                expected_sequence_before=row[6],
            )
            if batch.sha256 != spectrum_sha256(batch.payload):
                raise SpoolError(f"stored checksum mismatch for batch {batch.batch_id}")
            result.append(PendingBatch(batch=batch, attempts=row[7], last_error_class=row[8]))
        return tuple(result)

    def acknowledge(self, batch_id: UUID) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                "DELETE FROM spool_batches WHERE batch_id = ?",
                (str(batch_id),),
            )
            if cursor.rowcount != 1:
                raise SpoolError(f"cannot acknowledge missing batch {batch_id}")
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def mark_failure(self, batch_id: UUID, error: BaseException) -> None:
        # Store only the class. Exception strings can contain DSNs or USB serials.
        cursor = self._connection.execute(
            """
            UPDATE spool_batches
               SET attempts = attempts + 1, last_error_class = ?
             WHERE batch_id = ?
            """,
            (type(error).__name__, str(batch_id)),
        )
        if cursor.rowcount != 1:
            raise SpoolError(f"cannot mark missing batch {batch_id}")
