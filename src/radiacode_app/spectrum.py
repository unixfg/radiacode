from __future__ import annotations

import bisect
import hashlib
import math
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

ENCODING_VERSION_UINT32_LE: Final[int] = 1
UINT32_MAX: Final[int] = (1 << 32) - 1


class SpectrumEncodingError(ValueError):
    pass


def _validated_counts(counts: Iterable[int], expected_channel_count: int | None = None) -> tuple[int, ...]:
    materialized = tuple(counts)
    if not materialized:
        raise SpectrumEncodingError("a spectrum must contain at least one channel")
    if expected_channel_count is not None and len(materialized) != expected_channel_count:
        raise SpectrumEncodingError(
            f"channel count mismatch: observed {len(materialized)}, expected {expected_channel_count}"
        )
    for index, value in enumerate(materialized):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SpectrumEncodingError(f"channel {index} is not an integer")
        if not 0 <= value <= UINT32_MAX:
            raise SpectrumEncodingError(f"channel {index} is outside uint32 range")
    return materialized


def encode_counts_uint32le(
    counts: Iterable[int],
    *,
    expected_channel_count: int | None = None,
) -> bytes:
    """Encode exactly four little-endian bytes for every validated channel."""

    values = _validated_counts(counts, expected_channel_count)
    return struct.pack(f"<{len(values)}I", *values)


def decode_counts_uint32le(data: bytes, channel_count: int) -> tuple[int, ...]:
    if channel_count < 1:
        raise SpectrumEncodingError("channel_count must be positive")
    expected = channel_count * 4
    if len(data) != expected:
        raise SpectrumEncodingError(
            f"encoded length {len(data)} does not equal channel_count * 4 ({expected})"
        )
    return tuple(struct.unpack(f"<{channel_count}I", data))


def spectrum_sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def calibration_fingerprint(channel_count: int, coefficients: Sequence[float]) -> bytes:
    if channel_count < 2:
        raise ValueError("channel_count must include at least one data channel and overflow metadata")
    if len(coefficients) != 3 or not all(math.isfinite(value) for value in coefficients):
        raise ValueError("calibration must contain three finite coefficients")
    # The device supplies IEEE-754 float32 values. Fingerprinting the original
    # representation prevents insignificant Python float formatting changes from
    # creating new calibration epochs.
    canonical = struct.pack("<Ifff", channel_count, *coefficients)
    return hashlib.sha256(canonical).digest()


def calibrated_channel_edges(
    channel_count: int,
    coefficients: Sequence[float],
    *,
    exclude_overflow_channel: bool = True,
) -> tuple[float, ...]:
    """Return energy edges using the device polynomial at half-channel positions.

    RadiaCode coefficients map channel centers to energy. The final physical
    channel is retained as under/overflow metadata and is excluded from peak
    analysis by default.
    """

    if len(coefficients) != 3:
        raise ValueError("calibration requires a0, a1, and a2")
    usable_channels = channel_count - 1 if exclude_overflow_channel else channel_count
    if usable_channels < 1:
        raise ValueError("no analyzable channels")
    a0, a1, a2 = coefficients
    edges = tuple(a0 + a1 * (index - 0.5) + a2 * (index - 0.5) ** 2 for index in range(usable_channels + 1))
    if not all(math.isfinite(value) for value in edges):
        raise ValueError("calibration produced a non-finite edge")
    if any(right <= left for left, right in pairwise(edges)):
        raise ValueError("calibration is not strictly increasing over the channel range")
    return edges


@dataclass(frozen=True, slots=True)
class RebinnedSpectrum:
    counts: tuple[float, ...]
    outside_low: float
    outside_high: float
    overflow_metadata: int
    source_total: int

    @property
    def conserved_total(self) -> float:
        return sum(self.counts) + self.outside_low + self.outside_high + self.overflow_metadata


def count_conserving_rebin(
    counts: Sequence[int],
    source_edges: Sequence[float],
    target_edges: Sequence[float],
    *,
    last_channel_is_overflow: bool = True,
) -> RebinnedSpectrum:
    """Rebin with overlap weighting while preserving every source count.

    Uniform density inside a source energy bin is the only assumption. The final
    allocation for each source bin is calculated as the residual so floating
    point rounding cannot create or destroy counts.
    """

    values = _validated_counts(counts)
    analyzable = values[:-1] if last_channel_is_overflow else values
    overflow_metadata = values[-1] if last_channel_is_overflow else 0
    if len(source_edges) != len(analyzable) + 1:
        raise ValueError("source edge count must be analyzable channel count + 1")
    if len(target_edges) < 2:
        raise ValueError("at least two target edges are required")
    if any(right <= left for left, right in pairwise(source_edges)):
        raise ValueError("source edges must be strictly increasing")
    if any(right <= left for left, right in pairwise(target_edges)):
        raise ValueError("target edges must be strictly increasing")

    rebinned = [0.0] * (len(target_edges) - 1)
    outside_low = 0.0
    outside_high = 0.0
    target_min = target_edges[0]
    target_max = target_edges[-1]

    for source_index, count in enumerate(analyzable):
        if count == 0:
            continue
        left = source_edges[source_index]
        right = source_edges[source_index + 1]
        width = right - left
        allocations: list[tuple[str, int | None, float]] = []
        if left < target_min:
            allocations.append(("low", None, max(0.0, min(right, target_min) - left)))

        first_target = max(0, bisect.bisect_right(target_edges, left) - 1)
        for target_index in range(first_target, len(rebinned)):
            overlap = min(right, target_edges[target_index + 1]) - max(left, target_edges[target_index])
            if overlap > 0:
                allocations.append(("bin", target_index, overlap))
            if target_edges[target_index + 1] >= right:
                break

        if right > target_max:
            allocations.append(("high", None, max(0.0, right - max(left, target_max))))

        covered_width = sum(item[2] for item in allocations)
        if not math.isclose(covered_width, width, rel_tol=1e-12, abs_tol=1e-12 * max(1.0, width)):
            raise ValueError("target edges do not classify the complete source bin")

        assigned = 0.0
        for allocation_index, (destination, destination_index, overlap) in enumerate(allocations):
            amount = (
                float(count) - assigned
                if allocation_index == len(allocations) - 1
                else count * overlap / width
            )
            assigned += amount
            if destination == "low":
                outside_low += amount
            elif destination == "high":
                outside_high += amount
            else:
                assert destination_index is not None
                rebinned[destination_index] += amount

    result = RebinnedSpectrum(
        counts=tuple(rebinned),
        outside_low=outside_low,
        outside_high=outside_high,
        overflow_metadata=overflow_metadata,
        source_total=sum(values),
    )
    if not math.isclose(result.conserved_total, result.source_total, rel_tol=1e-12, abs_tol=1e-8):
        raise AssertionError("rebinning failed to conserve counts")
    return result


def add_counts_exact(spectra: Iterable[Sequence[int]]) -> tuple[int, ...]:
    iterator = iter(spectra)
    try:
        total = list(_validated_counts(next(iterator)))
    except StopIteration as error:
        raise SpectrumEncodingError("at least one spectrum is required") from error
    for spectrum in iterator:
        values = _validated_counts(spectrum, len(total))
        for index, value in enumerate(values):
            combined = total[index] + value
            if combined > UINT32_MAX:
                raise SpectrumEncodingError(f"uint32 overflow while aggregating channel {index}")
            total[index] = combined
    return tuple(total)
