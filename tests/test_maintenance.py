from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from radiacode_app.maintenance import Maintenance, _bucket, _FrameRow, _segment_frames
from radiacode_app.spectrum import UINT32_MAX


class MaintenanceTests(unittest.TestCase):
    def test_bucket_uses_utc_hour_and_day(self) -> None:
        timestamp = datetime(2026, 8, 16, 12, 34, 56, 789, tzinfo=UTC)
        self.assertEqual(_bucket(timestamp, "hour"), datetime(2026, 8, 16, 12, tzinfo=UTC))
        self.assertEqual(_bucket(timestamp, "day"), datetime(2026, 8, 16, tzinfo=UTC))
        with self.assertRaises(ValueError):
            _bucket(timestamp, "minute")

    def test_retention_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            Maintenance("unused", retention_days=0)

    def test_rollup_frames_split_before_uint32_overflow(self) -> None:
        observed_at = datetime(2026, 8, 16, 12, tzinfo=UTC)

        def frame(offset: int, counts: tuple[int, ...]) -> _FrameRow:
            return _FrameRow(
                device_id="device",
                calibration_epoch_id="calibration",
                started_at=observed_at + timedelta(seconds=offset * 300),
                ended_at=observed_at + timedelta(seconds=(offset + 1) * 300),
                duration_seconds=300,
                channel_count=2,
                counts=counts,
                quality_flags=(),
            )

        frames = [
            frame(0, (UINT32_MAX - 1, 1)),
            frame(1, (1, 2)),
            frame(2, (1, 3)),
        ]
        segments = _segment_frames(frames)
        self.assertEqual(segments, [frames[:2], frames[2:]])
        self.assertEqual([item for segment in segments for item in segment], frames)

    def test_empty_rollup_cannot_be_segmented(self) -> None:
        with self.assertRaises(ValueError):
            _segment_frames([])


if __name__ == "__main__":
    unittest.main()
