from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from radiacode_app.api.app import WebSettings, create_app
from radiacode_app.api.ranges import PublicRequestError, spectrum_resolution
from radiacode_app.api.repository import (
    MAX_PUBLIC_SPECTRUM_SOURCE_ROWS,
    PublicRepository,
    SpectrumRow,
)
from radiacode_app.api.service import comparison_response, spectrum_response

NOW = datetime.now(UTC)


def utc_query(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def spectrum_row(device: str = "radiacode-110", epoch: str = "epoch-a") -> SpectrumRow:
    return SpectrumRow(
        device=device,
        model="RadiaCode-110",
        calibration_epoch=epoch,
        calibration_started_at=NOW - timedelta(days=1),
        start_at=NOW - timedelta(minutes=5),
        end_at=NOW,
        duration_seconds=300,
        channel_count=4,
        calibration=(0.0, 1.0, 0.0),
        counts=(1, 2, 3, 4),
        quality_flags=(),
    )


class FakeRepository:
    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def ping(self) -> bool:
        return True

    def devices(self) -> list[dict[str, object]]:
        return [
            {
                "slug": "radiacode-110",
                "name": "RadiaCode 110",
                "model": "RC-110",
                "firmware_version": "4.13",
                "last_seen_at": NOW,
            }
        ]

    def current(self, slug: str) -> dict[str, object] | None:
        if slug != "radiacode-110":
            return None
        return {
            "last_seen_at": NOW,
            "realtime_observed_at": NOW,
            "status_observed_at": NOW - timedelta(seconds=2),
            "cps": 12.5,
            "dose_rate": 0.042,
            "cps_uncertainty_pct": 3.0,
            "dose_rate_uncertainty_pct": 4.0,
            "accumulated_dose": 1.2,
            "accumulated_duration_seconds": 500,
            "temperature_c": 24.5,
            "battery_pct": 90.0,
            "charging": True,
        }

    def current_states(self) -> list[dict[str, object]]:
        online = self.current("radiacode-110")
        assert online is not None
        return [
            {"slug": "radiacode-110", **online},
            {
                "slug": "radiacode-offline",
                "last_seen_at": None,
                "realtime_observed_at": None,
                "status_observed_at": None,
                "charging_observed_at": None,
                "cps": None,
                "dose_rate": None,
                "cps_uncertainty_pct": None,
                "dose_rate_uncertainty_pct": None,
                "accumulated_dose": None,
                "accumulated_duration_seconds": None,
                "temperature_c": None,
                "battery_pct": None,
                "charging": None,
            },
        ]

    def scalar_history(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "at": NOW,
                "cps_min": 10.0,
                "cps_max": 15.0,
                "cps_avg": 12.0,
                "cps_latest": 12.5,
                "dose_rate_min": 0.03,
                "dose_rate_max": 0.05,
                "dose_rate_avg": 0.04,
                "dose_rate_latest": 0.042,
            }
        ]

    def events(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [{"at": NOW, "code": "connected", "name": "Detector connected", "parameter": None}]

    def spectra(self, *_args: object, **_kwargs: object) -> list[SpectrumRow]:
        return [spectrum_row()]


def client() -> TestClient:
    settings = WebSettings(
        database_dsn=SecretStr("postgresql://not-used"),
        static_dir=Path("/does/not/exist"),
    )
    return TestClient(create_app(settings, FakeRepository()))  # type: ignore[arg-type]


def test_public_contract_and_no_private_identity() -> None:
    start = utc_query(NOW - timedelta(hours=1))
    end = utc_query(NOW)
    with client() as api:
        responses = (
            api.get("/api/v1/devices"),
            api.get("/api/v1/device-states"),
            api.get("/api/v1/devices/radiacode-110/current"),
            api.get(f"/api/v1/devices/radiacode-110/scalar-history?start={start}&end={end}"),
            api.get(f"/api/v1/devices/radiacode-110/events?start={start}&end={end}"),
            api.get(f"/api/v1/devices/radiacode-110/spectrum?start={start}&end={end}"),
            api.get(
                f"/api/v1/devices/radiacode-110/spectrogram?start={start}&end={end}&time_bins=4&energy_bins=3"
            ),
        )
    assert all(response.status_code == 200 for response in responses)
    combined = json.dumps([response.json() for response in responses])
    assert "usb_serial" not in combined
    assert "device_id" not in combined
    assert "RC-110-007802" not in combined
    states = responses[1].json()["states"]
    current = responses[2].json()
    devices = responses[0].json()
    assert devices["devices"][0]["firmware_version"] == "4.13"
    assert [state["device"] for state in states] == ["radiacode-110", "radiacode-offline"]
    assert states[0]["available"] is True
    assert states[0]["field_timestamps"]["cps"] != states[0]["field_timestamps"]["battery_pct"]
    assert states[1]["available"] is False
    assert states[1]["received_at"] is None
    assert states[1]["cps"] is None
    assert states[1]["field_timestamps"] == {}
    assert current["received_at"] is not None
    assert current["field_timestamps"]["cps"] != current["field_timestamps"]["battery_pct"]
    spectrum = responses[5].json()
    assert spectrum["rebinned"] is False
    assert spectrum["spectra"][0]["counts"] == [1, 2, 3, 4]
    assert spectrum["spectra"][0]["overflow_count"] == 4
    spectrogram = responses[6].json()
    assert len(spectrogram["counts"]) == 4
    assert len(spectrogram["counts"][0]) == 3


def test_range_and_response_caps_are_enforced() -> None:
    start = utc_query(NOW - timedelta(days=4_000))
    end = utc_query(NOW)
    with client() as api:
        too_long = api.get(f"/api/v1/devices/radiacode-110/scalar-history?start={start}&end={end}")
        too_many = api.get(
            f"/api/v1/devices/radiacode-110/spectrogram?start={utc_query(NOW - timedelta(hours=1))}"
            f"&end={end}&time_bins=1001"
        )
    assert too_long.status_code == 422
    assert too_many.status_code == 422
    assert "postgres" not in too_long.text.lower()


def test_spectrum_resolution_bounds_estimated_source_rows() -> None:
    assert spectrum_resolution(NOW - timedelta(days=2), NOW, 1_000) == "frame"
    assert spectrum_resolution(NOW - timedelta(days=31), NOW, 1_000) == "hour"
    assert spectrum_resolution(NOW - timedelta(days=31), NOW, 100) == "day"


def test_spectrum_source_limit_is_rejected_before_bytea_decode() -> None:
    with pytest.raises(PublicRequestError, match="selection is too large"):
        PublicRepository._materialize_spectrum_rows(
            [{}] * (MAX_PUBLIC_SPECTRUM_SOURCE_ROWS + 1),
            max_rows=MAX_PUBLIC_SPECTRUM_SOURCE_ROWS,
        )


def test_epochs_remain_separate_and_comparison_conserves_counts() -> None:
    first = spectrum_row(epoch="epoch-a")
    second = spectrum_row(epoch="epoch-b")
    response = spectrum_response("radiacode-110", (first, second))
    assert len(response.spectra) == 2

    repeated_calibration = spectrum_row(epoch="epoch-a")
    repeated_calibration = replace(
        repeated_calibration,
        calibration_started_at=NOW - timedelta(hours=1),
        start_at=NOW - timedelta(minutes=10),
        end_at=NOW - timedelta(minutes=5),
    )
    response = spectrum_response("radiacode-110", (first, repeated_calibration))
    assert len(response.spectra) == 2

    comparison = comparison_response(
        (first, spectrum_row(device="radiacode-103g")),
        energy_bins=3,
    )
    assert comparison.rebinned is True
    assert len(comparison.energy_edges_kev) == 4
    for series in comparison.series:
        assert abs(sum(series.counts) - series.source_total) < 1e-8
        assert series.coverage == 1.0


def test_default_export_is_n42() -> None:
    start = utc_query(NOW - timedelta(hours=1))
    end = utc_query(NOW)
    with client() as api:
        n42 = api.get(f"/api/v1/exports?devices=radiacode-110&start={start}&end={end}")
    assert n42.status_code == 200
    assert n42.headers["content-type"].startswith("application/xml")
    assert b"RadInstrumentData" in n42.content


@pytest.mark.parametrize("format_name", ("iaea-spe", "radiacode-xml"))
def test_single_spectrum_formats_preserve_cross_epoch_selection_as_zip(
    format_name: str,
) -> None:
    class CrossEpochRepository(FakeRepository):
        def spectra(self, *_args: object, **_kwargs: object) -> list[SpectrumRow]:
            return [spectrum_row(epoch="epoch-a"), spectrum_row(epoch="epoch-b")]

    settings = WebSettings(database_dsn=SecretStr("postgresql://not-used"), static_dir=Path("/missing"))
    start = utc_query(NOW - timedelta(hours=1))
    end = utc_query(NOW)
    with TestClient(create_app(settings, CrossEpochRepository())) as api:  # type: ignore[arg-type]
        response = api.get(
            f"/api/v1/exports?devices=radiacode-110&start={start}&end={end}&format={format_name}"
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 2
        assert any("epoch-a" in name for name in archive.namelist())
        assert any("epoch-b" in name for name in archive.namelist())


def test_latest_spectrum_returns_the_latest_frame_without_range_aggregation() -> None:
    class LatestRepository(FakeRepository):
        def spectra(self, *_args: object, **kwargs: object) -> list[SpectrumRow]:
            older = spectrum_row()
            rows = [
                older,
                replace(
                    older,
                    start_at=NOW,
                    end_at=NOW + timedelta(minutes=5),
                    counts=(5, 6, 7, 8),
                ),
            ]
            return rows[-1:] if kwargs.get("latest") is True else rows

    settings = WebSettings(database_dsn=SecretStr("postgresql://not-used"), static_dir=Path("/missing"))
    start = utc_query(NOW - timedelta(hours=1))
    end = utc_query(NOW)
    with TestClient(create_app(settings, LatestRepository())) as api:  # type: ignore[arg-type]
        response = api.get(f"/api/v1/devices/radiacode-110/spectrum?start={start}&end={end}&mode=latest")
    assert response.status_code == 200
    assert response.json()["spectra"][0]["counts"] == [5, 6, 7, 8]
