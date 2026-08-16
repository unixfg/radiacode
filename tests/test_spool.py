from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from radiacode_app.models import RawBatch
from radiacode_app.spectrum import spectrum_sha256
from radiacode_app.spool import SpoolCapacityError, SQLiteSpool


def batch(payload: bytes = b"payload") -> RawBatch:
    return RawBatch(
        batch_id=uuid4(),
        device_slug="rc-test",
        connection_id=uuid4(),
        received_at=datetime(2026, 8, 16, tzinfo=UTC),
        payload=payload,
        sha256=spectrum_sha256(payload),
        expected_sequence_before=4,
    )


class SpoolTests(unittest.TestCase):
    def test_batch_survives_reopen_and_ack_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spool.sqlite3"
            original = batch()
            with SQLiteSpool(path, max_bytes=1024) as spool:
                spool.append(original)
                self.assertEqual(spool.pending_count(), 1)
            with SQLiteSpool(path, max_bytes=1024) as reopened:
                pending = reopened.pending()
                self.assertEqual(pending[0].batch, original)
                reopened.acknowledge(original.batch_id)
                self.assertEqual(reopened.pending_count(), 0)

    def test_capacity_is_checked_before_and_during_append(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=5) as spool,
        ):
            spool.ensure_capacity(5)
            with self.assertRaises(SpoolCapacityError):
                spool.ensure_capacity(6)
            with self.assertRaises(SpoolCapacityError):
                spool.append(batch(b"123456"))

    def test_failure_stores_only_exception_class(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            SQLiteSpool(Path(directory) / "spool.sqlite3", max_bytes=1024) as spool,
        ):
            original = batch()
            spool.append(original)
            spool.mark_failure(original.batch_id, RuntimeError("secret-value"))
            pending = spool.pending()[0]
            self.assertEqual(pending.attempts, 1)
            self.assertEqual(pending.last_error_class, "RuntimeError")


if __name__ == "__main__":
    unittest.main()
