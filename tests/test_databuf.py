from __future__ import annotations

import struct
import unittest
from datetime import UTC, datetime, timedelta

from radiacode_app.databuf import decode_data_buf


def record(sequence: int, event: int, group: int, tick: int, body: bytes) -> bytes:
    return struct.pack("<BBBi", sequence, event, group, tick) + body


class DataBufferDecoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.received_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    def test_preserves_header_tick_flags_raw_and_batch_relative_time(self) -> None:
        first_body = struct.pack("<ffHHHB", 12.5, 0.25, 31, 42, 0x1234, 0x56)
        last_body = struct.pack("<IfHHH", 99, 1.5, 2345, 8700, 0x7788)
        payload = record(254, 0, 0, -200, first_body) + record(255, 0, 3, -100, last_body)
        decoded = decode_data_buf(payload, self.received_at)

        self.assertFalse(decoded.truncated)
        self.assertEqual(decoded.next_expected_sequence, 0)
        realtime, rare = decoded.records
        self.assertEqual(realtime.sequence, 254)
        self.assertEqual(realtime.device_tick, -200)
        self.assertEqual(realtime.flags, 0x1234)
        self.assertEqual(realtime.raw_record, payload[: 7 + len(first_body)])
        self.assertEqual(realtime.raw_payload, first_body)
        self.assertEqual(realtime.sample_at, self.received_at - timedelta(seconds=1))
        self.assertEqual(realtime.timestamp_quality, "batch_relative")
        self.assertAlmostEqual(realtime.values["count_rate_error_pct"], 3.1)
        self.assertAlmostEqual(realtime.values["dose_rate"], 2_500.0)
        self.assertEqual(rare.flags, 0x7788)
        self.assertEqual(rare.sample_at, self.received_at)
        self.assertAlmostEqual(rare.values["accumulated_dose"], 15_000.0)
        self.assertAlmostEqual(rare.values["temperature_c"], 3.45)
        self.assertAlmostEqual(rare.values["charge_pct"], 87.0)

    def test_sequence_gap_is_preserved_but_known_record_decoding_continues(self) -> None:
        payload = record(9, 0, 1, 0, struct.pack("<ff", 1.0, 2.0))
        decoded = decode_data_buf(payload, self.received_at, expected_sequence=7)
        self.assertIn("sequence_gap:expected=7:observed=9:distance=2", decoded.warnings)
        self.assertEqual(decoded.records[0].kind, "raw")

    def test_protocol_exposure_units_are_normalized_to_microsieverts(self) -> None:
        dose_rate_r_h = 1.23e-5
        cases = (
            (0, struct.pack("<ffHHHB", 7.0, dose_rate_r_h, 10, 20, 0, 0)),
            (1, struct.pack("<ff", 7.0, dose_rate_r_h)),
            (2, struct.pack("<IffHH", 9, 7.0, dose_rate_r_h, 20, 0)),
            (9, struct.pack("<fH", dose_rate_r_h, 0)),
        )
        for group, body in cases:
            with self.subTest(group=group):
                decoded = decode_data_buf(record(1, 0, group, 0, body), self.received_at)
                self.assertAlmostEqual(decoded.records[0].values["dose_rate"], 0.123, places=6)

        rare = decode_data_buf(
            record(1, 0, 3, 0, struct.pack("<IfHHH", 60, 2.5e-5, 2200, 5000, 0)),
            self.received_at,
        ).records[0]
        self.assertAlmostEqual(rare.values["accumulated_dose"], 0.25, places=6)

    def test_unknown_type_preserves_complete_remaining_tail_and_stops(self) -> None:
        tail = record(1, 99, 88, -1, b"\xaa\xbb\xcc")
        decoded = decode_data_buf(tail, self.received_at)
        self.assertTrue(decoded.unknown_tail)
        self.assertFalse(decoded.truncated)
        self.assertEqual(len(decoded.records), 1)
        self.assertEqual(decoded.records[0].kind, "unknown")
        self.assertEqual(decoded.records[0].raw_record, tail)
        self.assertEqual(decoded.records[0].raw_payload, b"\xaa\xbb\xcc")

    def test_truncated_known_record_preserves_bytes(self) -> None:
        payload = record(1, 0, 0, 7, b"\x00\x01")
        decoded = decode_data_buf(payload, self.received_at)
        self.assertTrue(decoded.truncated)
        self.assertEqual(decoded.records[0].kind, "truncated_record")
        self.assertEqual(decoded.records[0].raw_record, payload)

    def test_truncated_header_preserves_bytes(self) -> None:
        payload = b"\x01\x02\x03"
        decoded = decode_data_buf(payload, self.received_at)
        self.assertTrue(decoded.truncated)
        self.assertEqual(decoded.records[0].kind, "truncated_header")
        self.assertEqual(decoded.records[0].raw_record, payload)

    def test_variable_sample_record_length_is_bounded_and_preserved(self) -> None:
        sample_data = bytes(range(16))
        body = struct.pack("<HI", 2, 100) + sample_data
        decoded = decode_data_buf(record(1, 1, 1, 10, body), self.received_at)
        self.assertEqual(decoded.records[0].kind, "sample_8")
        self.assertEqual(decoded.records[0].values, {"sample_count": 2, "sample_time_ms": 100})

    def test_event_decoder_retains_unknown_numeric_event(self) -> None:
        body = struct.pack("<BBH", 250, 3, 4)
        decoded = decode_data_buf(record(1, 0, 7, 10, body), self.received_at)
        self.assertEqual(decoded.records[0].values["event"], 250)
        self.assertIsNone(decoded.records[0].values["event_name"])
        self.assertIn("unknown_event:250", decoded.warnings)

    def test_received_at_must_be_aware(self) -> None:
        with self.assertRaises(ValueError):
            decode_data_buf(b"", datetime(2026, 1, 1))

    def test_non_finite_and_negative_realtime_is_retained_but_invalid(self) -> None:
        body = struct.pack("<ffHHHB", float("nan"), -0.25, 31, 42, 0, 0)
        decoded = decode_data_buf(record(1, 0, 0, 10, body), self.received_at)

        values = decoded.records[0].values
        self.assertIsNone(values["count_rate"])
        self.assertEqual(values["dose_rate"], -2_500.0)
        self.assertIs(values["valid"], False)
        self.assertIn("non_finite_value:count_rate", decoded.warnings)
        self.assertIn("invalid_real_time", decoded.warnings)

    def test_non_finite_status_field_invalidates_only_the_projection(self) -> None:
        body = struct.pack("<IfHHH", 99, float("nan"), 2345, 8700, 0)
        decoded = decode_data_buf(record(1, 0, 3, 10, body), self.received_at)

        values = decoded.records[0].values
        self.assertIsNone(values["accumulated_dose"])
        self.assertIs(values["valid"], False)
        self.assertIn("invalid_status", decoded.warnings)

    def test_signed_tick_wrap_is_anchored_on_the_uint32_ring(self) -> None:
        body = struct.pack("<ff", 1.0, 2.0)
        payload = record(1, 0, 1, (1 << 31) - 1, body) + record(2, 0, 1, -(1 << 31), body)

        decoded = decode_data_buf(payload, self.received_at)

        self.assertEqual(decoded.records[0].device_tick, (1 << 31) - 1)
        self.assertEqual(decoded.records[1].device_tick, -(1 << 31))
        self.assertEqual(
            decoded.records[0].sample_at,
            self.received_at - timedelta(milliseconds=10),
        )
        self.assertEqual(decoded.records[1].sample_at, self.received_at)
        self.assertTrue(all(record.timestamp_quality == "batch_relative" for record in decoded.records))

    def test_future_tick_anomaly_is_retained_without_a_sample_timestamp(self) -> None:
        body = struct.pack("<ff", 1.0, 2.0)
        payload = record(1, 0, 1, 200, body) + record(2, 0, 1, 0, body)

        decoded = decode_data_buf(payload, self.received_at)

        anomalous, anchor = decoded.records
        self.assertIsNone(anomalous.sample_at)
        self.assertEqual(anomalous.timestamp_quality, "invalid_tick")
        self.assertIn("device_tick_outside_batch_anchor_window", anomalous.warnings)
        self.assertIn("device_tick_outside_batch_anchor_window", decoded.warnings)
        self.assertEqual(anchor.sample_at, self.received_at)
        self.assertEqual(anchor.timestamp_quality, "batch_relative")


if __name__ == "__main__":
    unittest.main()
