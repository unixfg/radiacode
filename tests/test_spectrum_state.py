from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from radiacode_app.models import DeviceSpectrum
from radiacode_app.spectrum_state import SpectrumState, advance_spectrum_state

START = datetime(2026, 8, 16, tzinfo=UTC)


def observation(
    seconds: int,
    counts: tuple[int, ...],
    calibration: tuple[float, float, float] = (0, 1, 0),
) -> DeviceSpectrum:
    return DeviceSpectrum(
        observed_at=START + timedelta(seconds=seconds),
        duration_seconds=seconds,
        coefficients=calibration,
        counts=counts,
    )


class SpectrumStateTests(unittest.TestCase):
    def test_exact_deltas_form_one_unsplit_frame(self) -> None:
        connection = uuid4()
        transition = advance_spectrum_state(
            SpectrumState(), observation(0, (0, 0, 0)), connection_id=connection, expected_channel_count=3
        )
        session = transition.started_session
        state = transition.state
        for minute in range(1, 6):
            transition = advance_spectrum_state(
                state,
                observation(minute * 60, (minute, minute * 2, 0)),
                connection_id=connection,
                expected_channel_count=3,
            )
            state = transition.state
        self.assertIsNotNone(transition.frame)
        assert transition.frame is not None
        self.assertEqual(transition.frame.session_id, session)
        self.assertEqual(transition.frame.duration_seconds, 300)
        self.assertEqual(transition.frame.counts, (5, 10, 0))
        self.assertEqual(transition.frame.source_intervals, 5)
        self.assertIsNone(state.accumulator)

    def test_large_delta_is_not_proportionally_split(self) -> None:
        connection = uuid4()
        state = advance_spectrum_state(
            SpectrumState(), observation(0, (0, 0, 0)), connection_id=connection, expected_channel_count=3
        ).state
        transition = advance_spectrum_state(
            state,
            observation(360, (6, 9, 1)),
            connection_id=connection,
            expected_channel_count=3,
            frame_target_seconds=300,
        )
        assert transition.frame is not None
        self.assertEqual(transition.frame.duration_seconds, 360)
        self.assertEqual(transition.frame.counts, (6, 9, 1))

    def test_monotonic_reconnect_continues_same_session(self) -> None:
        first_connection = uuid4()
        initial = advance_spectrum_state(
            SpectrumState(),
            observation(0, (0, 0, 0)),
            connection_id=first_connection,
            expected_channel_count=3,
        )
        transition = advance_spectrum_state(
            initial.state,
            observation(60, (1, 2, 0)),
            connection_id=uuid4(),
            expected_channel_count=3,
        )
        self.assertEqual(transition.state.cursor.session_id, initial.started_session)
        self.assertIn("session_continued_across_reconnect", transition.warnings)

    def test_reconnect_warning_is_retained_in_the_eventual_frame(self) -> None:
        first_connection = uuid4()
        initial = advance_spectrum_state(
            SpectrumState(),
            observation(0, (0, 0, 0)),
            connection_id=first_connection,
            expected_channel_count=3,
        )
        partial = advance_spectrum_state(
            initial.state,
            observation(60, (1, 2, 0)),
            connection_id=uuid4(),
            expected_channel_count=3,
        )
        completed = advance_spectrum_state(
            partial.state,
            observation(300, (5, 10, 0)),
            connection_id=first_connection,
            expected_channel_count=3,
        )
        assert completed.frame is not None
        self.assertIn("session_continued_across_reconnect", completed.frame.quality_flags)

    def test_counts_without_duration_are_preserved_and_flagged(self) -> None:
        connection = uuid4()
        initial = advance_spectrum_state(
            SpectrumState(),
            observation(0, (0, 0, 0)),
            connection_id=connection,
            expected_channel_count=3,
        )
        anomalous = advance_spectrum_state(
            initial.state,
            observation(0, (1, 2, 0)),
            connection_id=connection,
            expected_channel_count=3,
        )
        assert anomalous.gap is not None
        self.assertEqual(anomalous.gap.reason, "counts_changed_without_duration_change")
        assert anomalous.state.accumulator is not None
        self.assertEqual(anomalous.state.accumulator.counts, (1, 2, 0))
        self.assertEqual(anomalous.state.accumulator.duration_seconds, 0)
        self.assertIn(
            "counts_changed_without_duration_change",
            anomalous.state.accumulator.quality_flags,
        )

    def test_channel_change_is_a_recorded_boundary_after_initial_validation(self) -> None:
        connection = uuid4()
        initial = advance_spectrum_state(
            SpectrumState(),
            observation(0, (0, 0, 0)),
            connection_id=connection,
            expected_channel_count=3,
        )
        changed = advance_spectrum_state(
            initial.state,
            observation(60, (1, 2, 3, 0)),
            connection_id=connection,
            expected_channel_count=3,
        )
        assert changed.gap is not None
        self.assertEqual(changed.gap.reason, "channel_count_change")
        assert changed.state.cursor is not None
        self.assertEqual(len(changed.state.cursor.counts), 4)

    def test_regression_closes_session_and_flushes_partial_frame(self) -> None:
        connection = uuid4()
        initial = advance_spectrum_state(
            SpectrumState(), observation(0, (0, 0, 0)), connection_id=connection, expected_channel_count=3
        )
        partial = advance_spectrum_state(
            initial.state, observation(60, (2, 3, 0)), connection_id=connection, expected_channel_count=3
        )
        reset = advance_spectrum_state(
            partial.state, observation(1, (0, 0, 0)), connection_id=connection, expected_channel_count=3
        )
        self.assertEqual(reset.closed_session, initial.started_session)
        self.assertEqual(reset.gap.reason, "duration_regression")
        self.assertIn("partial_frame_on_session_boundary", reset.warnings)
        self.assertIsNotNone(reset.frame)
        assert reset.frame is not None
        self.assertEqual(reset.frame.session_id, initial.started_session)
        self.assertEqual(reset.frame.duration_seconds, 60)
        self.assertEqual(reset.frame.counts, (2, 3, 0))
        self.assertIsNone(reset.state.accumulator)

    def test_calibration_change_starts_new_epoch_boundary(self) -> None:
        connection = uuid4()
        initial = advance_spectrum_state(
            SpectrumState(), observation(0, (0, 0, 0)), connection_id=connection, expected_channel_count=3
        )
        changed = advance_spectrum_state(
            initial.state,
            observation(60, (1, 2, 0), calibration=(0, 2, 0)),
            connection_id=connection,
            expected_channel_count=3,
        )
        self.assertEqual(changed.gap.reason, "calibration_change")
        self.assertNotEqual(changed.started_session, initial.started_session)


if __name__ == "__main__":
    unittest.main()
