from __future__ import annotations

import math
import unittest

from radiacode_app.spectrum import (
    SpectrumEncodingError,
    add_counts_exact,
    calibrated_channel_edges,
    calibration_fingerprint,
    count_conserving_rebin,
    decode_counts_uint32le,
    encode_counts_uint32le,
    spectrum_sha256,
)


class SpectrumCodecTests(unittest.TestCase):
    def test_uint32_little_endian_is_exact(self) -> None:
        encoded = encode_counts_uint32le((1, 0x01020304), expected_channel_count=2)
        self.assertEqual(encoded, b"\x01\x00\x00\x00\x04\x03\x02\x01")
        self.assertEqual(decode_counts_uint32le(encoded, 2), (1, 0x01020304))
        self.assertEqual(len(spectrum_sha256(encoded)), 32)

    def test_codec_rejects_wrong_length_and_out_of_range(self) -> None:
        with self.assertRaises(SpectrumEncodingError):
            encode_counts_uint32le((1,), expected_channel_count=2)
        with self.assertRaises(SpectrumEncodingError):
            encode_counts_uint32le((-1,))
        with self.assertRaises(SpectrumEncodingError):
            encode_counts_uint32le((2**32,))
        with self.assertRaises(SpectrumEncodingError):
            decode_counts_uint32le(b"\x00" * 7, 2)

    def test_fingerprint_uses_float32_device_representation(self) -> None:
        first = calibration_fingerprint(1024, (1.0, 2.0, 3.0))
        second = calibration_fingerprint(1024, (1.0 + 1e-10, 2.0, 3.0))
        self.assertEqual(first, second)
        self.assertNotEqual(first, calibration_fingerprint(1023, (1.0, 2.0, 3.0)))

    def test_edges_exclude_final_metadata_channel(self) -> None:
        self.assertEqual(calibrated_channel_edges(3, (0.0, 1.0, 0.0)), (-0.5, 0.5, 1.5))

    def test_rebin_conserves_counts_and_separates_overflow(self) -> None:
        result = count_conserving_rebin(
            (10, 20, 7),
            (0.0, 1.0, 2.0),
            (0.0, 0.5, 1.0, 2.0),
        )
        self.assertEqual(result.counts, (5.0, 5.0, 20.0))
        self.assertEqual(result.overflow_metadata, 7)
        self.assertTrue(math.isclose(result.conserved_total, 37.0))

    def test_partial_target_tracks_counts_outside_range(self) -> None:
        result = count_conserving_rebin(
            (10, 20, 7),
            (0.0, 1.0, 2.0),
            (0.5, 1.5),
        )
        self.assertEqual(result.counts, (15.0,))
        self.assertEqual(result.outside_low, 5.0)
        self.assertEqual(result.outside_high, 10.0)
        self.assertEqual(result.conserved_total, 37.0)

    def test_exact_add_detects_uint32_overflow(self) -> None:
        self.assertEqual(add_counts_exact(((1, 2), (3, 4))), (4, 6))
        with self.assertRaises(SpectrumEncodingError):
            add_counts_exact((((1 << 32) - 1,), (1,)))


if __name__ == "__main__":
    unittest.main()
