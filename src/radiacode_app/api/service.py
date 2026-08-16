from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime

from radiacode_app.spectrum import calibrated_channel_edges, count_conserving_rebin

from .contracts import (
    Calibration,
    RebinnedSeries,
    SpectrogramResponse,
    SpectrumComparisonResponse,
    SpectrumResponse,
    SpectrumSeries,
)
from .repository import SpectrumRow


def aggregate_rows(rows: Iterable[SpectrumRow]) -> list[SpectrumRow]:
    groups: dict[tuple[str, str, datetime], list[SpectrumRow]] = defaultdict(list)
    for row in rows:
        groups[(row.device, row.calibration_epoch, row.calibration_started_at)].append(row)
    result: list[SpectrumRow] = []
    for key in sorted(groups):
        items = groups[key]
        first = items[0]
        counts = [0] * first.channel_count
        flags: set[str] = set()
        for item in items:
            if (
                item.channel_count != first.channel_count
                or len(item.counts) != len(counts)
                or item.calibration != first.calibration
            ):
                raise ValueError("calibration epoch contains incompatible spectra")
            for index, value in enumerate(item.counts):
                counts[index] += value
            flags.update(item.quality_flags)
        result.append(
            replace(
                first,
                start_at=min(item.start_at for item in items),
                end_at=max(item.end_at for item in items),
                duration_seconds=sum(item.duration_seconds for item in items),
                counts=tuple(counts),
                quality_flags=tuple(sorted(flags)),
            )
        )
    return result


def spectrum_response(device: str, rows: Iterable[SpectrumRow]) -> SpectrumResponse:
    spectra: list[SpectrumSeries] = []
    for row in aggregate_rows(rows):
        spectra.append(
            SpectrumSeries(
                epoch_started_at=row.calibration_started_at,
                start=row.start_at,
                end=row.end_at,
                duration_seconds=row.duration_seconds,
                channel_count=row.channel_count,
                calibration=Calibration(a0=row.calibration[0], a1=row.calibration[1], a2=row.calibration[2]),
                counts=list(row.counts),
                overflow_count=row.counts[-1],
                quality_flags=list(row.quality_flags),
            )
        )
    return SpectrumResponse(device=device, spectra=spectra)


def _common_energy_edges(rows: list[SpectrumRow], energy_bins: int) -> tuple[float, ...]:
    if not 1 <= energy_bins <= 512:
        raise ValueError("energy_bins must be between 1 and 512")
    source_edges = [calibrated_channel_edges(row.channel_count, row.calibration) for row in rows]
    low = min(edges[0] for edges in source_edges)
    high = max(edges[-1] for edges in source_edges)
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        raise ValueError("invalid shared energy range")
    width = (high - low) / energy_bins
    return tuple(low + width * index for index in range(energy_bins + 1))


def comparison_response(
    rows: Iterable[SpectrumRow],
    energy_bins: int,
) -> SpectrumComparisonResponse:
    aggregated = aggregate_rows(rows)
    if not aggregated:
        return SpectrumComparisonResponse(energy_edges_kev=[], series=[])
    edges = _common_energy_edges(aggregated, energy_bins)
    by_device: dict[str, list[SpectrumRow]] = defaultdict(list)
    for row in aggregated:
        by_device[row.device].append(row)
    series: list[RebinnedSeries] = []
    for device in sorted(by_device):
        total = [0.0] * energy_bins
        source_total = 0
        for row in by_device[device]:
            rebinned = count_conserving_rebin(
                row.counts,
                calibrated_channel_edges(row.channel_count, row.calibration),
                edges,
            )
            total = [left + right for left, right in zip(total, rebinned.counts, strict=True)]
            source_total += sum(row.counts[:-1])
        in_range = sum(total)
        series.append(
            RebinnedSeries(
                device=device,
                counts=total,
                source_total=source_total,
                coverage=(in_range / source_total if source_total else 1.0),
            )
        )
    return SpectrumComparisonResponse(
        energy_edges_kev=list(edges),
        series=series,
    )


def spectrogram_response(
    device: str,
    start: datetime,
    end: datetime,
    rows: Iterable[SpectrumRow],
    *,
    time_bins: int,
    energy_bins: int,
    source_resolution: str,
) -> SpectrogramResponse:
    if not 1 <= time_bins <= 1_000:
        raise ValueError("time_bins must be between 1 and 1000")
    materialized = list(rows)
    if not materialized:
        return SpectrogramResponse(
            device=device,
            time_edges=[],
            energy_edges_kev=[],
            counts=[],
            source_resolution=source_resolution,
        )
    energy_edges = _common_energy_edges(materialized, energy_bins)
    span = end - start
    bin_width = span / time_bins
    time_edges = [start + bin_width * index for index in range(time_bins + 1)]
    grid = [[0.0 for _ in range(energy_bins)] for _ in range(time_bins)]
    for row in materialized:
        # Whole exposure frames and rollups are assigned by their end time;
        # counts are never divided across wall-clock bins.
        fraction = (row.end_at - start).total_seconds() / span.total_seconds()
        index = min(time_bins - 1, max(0, int(fraction * time_bins)))
        rebinned = count_conserving_rebin(
            row.counts,
            calibrated_channel_edges(row.channel_count, row.calibration),
            energy_edges,
        )
        grid[index] = [left + right for left, right in zip(grid[index], rebinned.counts, strict=True)]
    # Empty leading/trailing bins remain explicit so the heatmap preserves gaps.
    return SpectrogramResponse(
        device=device,
        time_edges=time_edges,
        energy_edges_kev=list(energy_edges),
        counts=grid,
        source_resolution=source_resolution,
    )
