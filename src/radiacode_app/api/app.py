from __future__ import annotations

import io
import re
import zipfile
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated, BinaryIO, Literal, cast

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from radiacode_app.exporters import (
    ExportSpectrum,
    export_csv,
    export_iaea_spe,
    export_n42_2012,
    export_npes_v2,
    export_radiacode_xml,
)
from radiacode_app.logging import logger

from .contracts import (
    CurrentState,
    DevicesResponse,
    DeviceSummary,
    EventsResponse,
    PublicEvent,
    ScalarHistoryResponse,
    ScalarPoint,
    ScalarValues,
    SpectrogramResponse,
    SpectrumComparisonResponse,
    SpectrumResponse,
)
from .ranges import PublicRequestError, bounded_utc_range, resolution_seconds, spectrum_resolution
from .repository import MAX_PUBLIC_SPECTRUM_SOURCE_ROWS, PublicRepository, SpectrumRow
from .service import aggregate_rows, comparison_response, spectrogram_response, spectrum_response

PUBLIC_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RADIACODE_", env_file=None, extra="ignore")

    database_dsn: SecretStr
    static_dir: Path = Path("/opt/radiacode/static")
    availability_seconds: float = Field(default=10.0, ge=1, le=300)
    public_max_range_days: int = Field(default=3_650, ge=1, le=36_500)
    database_pool_size: int = Field(default=10, ge=1, le=50)


def _slug(value: str) -> str:
    if PUBLIC_SLUG.fullmatch(value) is None:
        raise PublicRequestError("invalid device slug")
    return value


def _availability(last_seen_at: datetime | None, now: datetime, seconds: float) -> bool:
    return last_seen_at is not None and now - last_seen_at <= timedelta(seconds=seconds)


def _field_timestamps(row: dict[str, object]) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    realtime = row.get("realtime_observed_at")
    status = row.get("status_observed_at")
    charging = row.get("charging_observed_at")
    if isinstance(realtime, datetime):
        for name in ("cps", "dose_rate", "cps_uncertainty_pct", "dose_rate_uncertainty_pct"):
            if row.get(name) is not None:
                result[name] = realtime
    if isinstance(status, datetime):
        for name in (
            "accumulated_dose",
            "accumulated_duration_seconds",
            "temperature_c",
            "battery_pct",
        ):
            if row.get(name) is not None:
                result[name] = status
    if isinstance(charging, datetime) and row.get("charging") is not None:
        result["charging"] = charging
    return result


def _scalar_values(row: dict[str, object], prefix: str) -> ScalarValues | None:
    values = tuple(row.get(f"{prefix}_{name}") for name in ("min", "max", "avg", "latest"))
    if any(value is None for value in values):
        return None
    numeric = cast(tuple[float, float, float, float], values)
    return ScalarValues(min=numeric[0], max=numeric[1], avg=numeric[2], latest=numeric[3])


def _export_model(row: SpectrumRow) -> ExportSpectrum:
    return ExportSpectrum(
        device_slug=row.device,
        device_model=row.model,
        calibration_key=row.calibration_epoch,
        start_at=row.start_at,
        end_at=row.end_at,
        duration_seconds=row.duration_seconds,
        counts=tuple(row.counts),
        calibration=row.calibration,
        quality_flags=row.quality_flags,
    )


def _safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.") or "spectrum"


def _single_spectrum_archive(
    spectra: list[ExportSpectrum],
    format_name: Literal["iaea-spe", "radiacode-xml"],
) -> bytes:
    extension = "spe" if format_name == "iaea-spe" else "xml"
    writer = export_iaea_spe if format_name == "iaea-spe" else export_radiacode_xml
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for index, spectrum in enumerate(spectra, start=1):
            filename = _safe_filename(
                f"{index:03d}-{spectrum.device_slug}-{spectrum.calibration_key}.{extension}"
            )
            archive.writestr(filename, writer(spectrum))
    return output.getvalue()


def _write_frame_archive(
    output: BinaryIO,
    rows: Iterable[SpectrumRow],
    format_name: Literal["n42-2012", "npes-v2", "csv"],
) -> None:
    extension = {"n42-2012": "n42", "npes-v2": "json", "csv": "csv"}[format_name]
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for index, row in enumerate(rows, start=1):
            spectrum = _export_model(row)
            if format_name == "n42-2012":
                payload = export_n42_2012((spectrum,))
            elif format_name == "npes-v2":
                payload = export_npes_v2((spectrum,))
            else:
                payload = export_csv((spectrum,))
            filename = _safe_filename(
                f"{index:05d}-{spectrum.device_slug}-{spectrum.calibration_key}.{extension}"
            )
            archive.writestr(filename, payload)
    output.seek(0)


def _temporary_file_stream(output: BinaryIO) -> Iterator[bytes]:
    try:
        output.seek(0)
        while chunk := output.read(1024 * 1024):
            yield chunk
    finally:
        output.close()


def create_app(
    settings: WebSettings | None = None,
    repository: PublicRepository | None = None,
) -> FastAPI:
    configured = settings or WebSettings()  # type: ignore[call-arg]
    repo = repository or PublicRepository(
        configured.database_dsn.get_secret_value(),
        max_size=configured.database_pool_size,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        repo.open()
        try:
            yield
        finally:
            repo.close()

    app = FastAPI(
        title="RadiaCode public API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.repository = repo

    @app.exception_handler(PublicRequestError)
    async def public_request_error(_request: Request, error: PublicRequestError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request", "message": str(error)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "message": "request parameters are invalid"},
        )

    @app.exception_handler(Exception)
    async def operational_error(request: Request, error: Exception) -> JSONResponse:
        logger().error(
            "public_request_failed",
            path=request.url.path,
            error_class=type(error).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"error": "temporarily_unavailable", "message": "service temporarily unavailable"},
        )

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def ready() -> dict[str, str]:
        try:
            if not repo.ping():
                raise RuntimeError("database ping failed")
        except Exception as error:
            raise HTTPException(status_code=503, detail="not ready") from error
        return {"status": "ready"}

    @app.get("/api/v1/devices", response_model=DevicesResponse)
    def devices() -> DevicesResponse:
        now = datetime.now(UTC)
        return DevicesResponse(
            devices=[
                DeviceSummary(
                    slug=row["slug"],
                    name=row["name"],
                    model=row["model"],
                    available=_availability(row["last_seen_at"], now, configured.availability_seconds),
                    last_seen_at=row["last_seen_at"],
                )
                for row in repo.devices()
            ]
        )

    @app.get("/api/v1/devices/{device}/current", response_model=CurrentState)
    def current(device: str) -> CurrentState:
        public_device = _slug(device)
        row = repo.current(public_device)
        if row is None:
            raise HTTPException(status_code=404, detail="device not found")
        now = datetime.now(UTC)
        return CurrentState(
            device=public_device,
            received_at=row["last_seen_at"],
            available=_availability(row["last_seen_at"], now, configured.availability_seconds),
            cps=row["cps"],
            dose_rate=row["dose_rate"],
            cps_uncertainty_pct=row["cps_uncertainty_pct"],
            dose_rate_uncertainty_pct=row["dose_rate_uncertainty_pct"],
            accumulated_dose=row["accumulated_dose"],
            accumulated_duration_seconds=row["accumulated_duration_seconds"],
            temperature_c=row["temperature_c"],
            battery_pct=row["battery_pct"],
            charging=row["charging"],
            field_timestamps=_field_timestamps(row),
        )

    @app.get("/api/v1/devices/{device}/scalar-history", response_model=ScalarHistoryResponse)
    def scalar_history(
        device: str,
        start: datetime,
        end: datetime,
        max_points: Annotated[int, Query(ge=1, le=2_000)] = 2_000,
    ) -> ScalarHistoryResponse:
        public_device = _slug(device)
        start_utc, end_utc = bounded_utc_range(start, end, max_days=configured.public_max_range_days)
        minimum = 60 if datetime.now(UTC) - start_utc > timedelta(days=30) else 1
        bucket = resolution_seconds(
            start_utc,
            end_utc,
            max_points,
            minimum=minimum,
        )
        rows = repo.scalar_history(
            public_device,
            start_utc,
            end_utc,
            bucket,
            use_rollups=bucket >= 60,
        )
        return ScalarHistoryResponse(
            device=public_device,
            start=start_utc,
            end=end_utc,
            resolution_seconds=bucket,
            points=[
                ScalarPoint(
                    at=row["at"],
                    cps=_scalar_values(row, "cps"),
                    dose_rate=_scalar_values(row, "dose_rate"),
                )
                for row in rows
            ],
        )

    @app.get("/api/v1/devices/{device}/events", response_model=EventsResponse)
    def events(
        device: str,
        start: datetime,
        end: datetime,
        limit: Annotated[int, Query(ge=1, le=2_000)] = 500,
    ) -> EventsResponse:
        public_device = _slug(device)
        start_utc, end_utc = bounded_utc_range(start, end, max_days=configured.public_max_range_days)
        rows = repo.events(public_device, start_utc, end_utc, limit)
        return EventsResponse(
            events=[
                PublicEvent(
                    at=row["at"],
                    code=row["code"],
                    name=row["name"],
                    parameter=row["parameter"],
                )
                for row in rows
            ],
        )

    @app.get("/api/v1/devices/{device}/spectrum", response_model=SpectrumResponse)
    def spectrum(
        device: str,
        start: datetime,
        end: datetime,
        mode: Literal["latest", "range"] = "range",
    ) -> SpectrumResponse:
        public_device = _slug(device)
        start_utc, end_utc = bounded_utc_range(start, end, max_days=configured.public_max_range_days)
        if mode == "latest":
            rows = repo.spectra(
                (public_device,),
                start_utc,
                end_utc,
                limit=1,
                latest=True,
            )
        else:
            resolution = spectrum_resolution(start_utc, end_utc, 1_000)
            rows = repo.spectra(
                (public_device,),
                start_utc,
                end_utc,
                resolution=resolution,
                limit=MAX_PUBLIC_SPECTRUM_SOURCE_ROWS,
            )
        return spectrum_response(public_device, rows)

    @app.get("/api/v1/spectrum-comparison", response_model=SpectrumComparisonResponse)
    def comparison(
        devices: str,
        start: datetime,
        end: datetime,
        energy_bins: Annotated[int, Query(ge=1, le=512)] = 512,
    ) -> SpectrumComparisonResponse:
        slugs = tuple(dict.fromkeys(_slug(value.strip()) for value in devices.split(",") if value.strip()))
        if not 2 <= len(slugs) <= 4:
            raise PublicRequestError("comparison requires between two and four devices")
        start_utc, end_utc = bounded_utc_range(start, end, max_days=configured.public_max_range_days)
        per_device_budget = min(1_000, MAX_PUBLIC_SPECTRUM_SOURCE_ROWS // len(slugs))
        resolution = spectrum_resolution(start_utc, end_utc, per_device_budget)
        rows = repo.spectra(
            slugs,
            start_utc,
            end_utc,
            resolution=resolution,
            limit=MAX_PUBLIC_SPECTRUM_SOURCE_ROWS,
        )
        return comparison_response(rows, energy_bins)

    @app.get("/api/v1/devices/{device}/spectrogram", response_model=SpectrogramResponse)
    def spectrogram(
        device: str,
        start: datetime,
        end: datetime,
        time_bins: Annotated[int, Query(ge=1, le=1_000)] = 1_000,
        energy_bins: Annotated[int, Query(ge=1, le=512)] = 512,
    ) -> SpectrogramResponse:
        public_device = _slug(device)
        start_utc, end_utc = bounded_utc_range(start, end, max_days=configured.public_max_range_days)
        resolution = spectrum_resolution(start_utc, end_utc, time_bins)
        rows = repo.spectra(
            (public_device,),
            start_utc,
            end_utc,
            resolution=resolution,
            limit=MAX_PUBLIC_SPECTRUM_SOURCE_ROWS,
        )
        return spectrogram_response(
            public_device,
            start_utc,
            end_utc,
            rows,
            time_bins=time_bins,
            energy_bins=energy_bins,
            source_resolution=resolution,
        )

    @app.get("/api/v1/exports")
    def exports(
        devices: str,
        start: datetime,
        end: datetime,
        format: Literal["n42-2012", "npes-v2", "csv", "iaea-spe", "radiacode-xml"] = "n42-2012",
        mode: Literal["aggregate", "frames"] = "aggregate",
    ) -> Response:
        slugs = tuple(dict.fromkeys(_slug(value.strip()) for value in devices.split(",") if value.strip()))
        if not slugs or len(slugs) > 16:
            raise PublicRequestError("between one and sixteen devices are required")
        if mode == "frames" and format not in {"n42-2012", "npes-v2", "csv"}:
            raise PublicRequestError("five-minute slices are supported only for N42, NPES, and CSV")
        start_utc, end_utc = bounded_utc_range(start, end, max_days=configured.public_max_range_days)
        stem = _safe_filename(f"radiacode-{start_utc:%Y%m%dT%H%M%SZ}-{end_utc:%Y%m%dT%H%M%SZ}")
        if mode == "frames":
            frame_format = cast(Literal["n42-2012", "npes-v2", "csv"], format)
            # StreamingResponse owns and closes this file after the client drains it.
            output = TemporaryFile(mode="w+b")  # noqa: SIM115
            try:
                with repo.spectrum_frame_export(slugs, start_utc, end_utc) as (
                    frame_count,
                    frame_rows,
                ):
                    if frame_count > 10_000:
                        raise PublicRequestError("five-minute export is capped at 10000 frames")
                    if frame_count == 0:
                        raise HTTPException(status_code=404, detail="no spectra in selection")
                    _write_frame_archive(output, frame_rows, frame_format)
            except BaseException:
                output.close()
                raise
            return StreamingResponse(
                _temporary_file_stream(output),
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{stem}-frames.zip"'},
            )

        per_device_budget = min(1_000, MAX_PUBLIC_SPECTRUM_SOURCE_ROWS // len(slugs))
        resolution = spectrum_resolution(start_utc, end_utc, per_device_budget)
        rows = repo.spectra(
            slugs,
            start_utc,
            end_utc,
            resolution=resolution,
            limit=MAX_PUBLIC_SPECTRUM_SOURCE_ROWS,
        )
        selected = aggregate_rows(rows)
        if not selected:
            raise HTTPException(status_code=404, detail="no spectra in selection")
        spectra = [_export_model(row) for row in selected]
        if format == "n42-2012":
            body, media_type, filename = export_n42_2012(spectra), "application/xml", f"{stem}.n42"
        elif format == "npes-v2":
            body, media_type, filename = export_npes_v2(spectra), "application/json", f"{stem}.json"
        elif format == "csv":
            body, media_type, filename = export_csv(spectra), "text/csv; charset=utf-8", f"{stem}.csv"
        elif len(spectra) == 1:
            if format == "iaea-spe":
                body, media_type, filename = export_iaea_spe(spectra[0]), "text/plain", f"{stem}.spe"
            else:
                body, media_type, filename = (
                    export_radiacode_xml(spectra[0]),
                    "application/xml",
                    f"{stem}.xml",
                )
        else:
            body = _single_spectrum_archive(spectra, format)
            media_type, filename = "application/zip", f"{stem}.zip"
        return Response(
            body,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    if configured.static_dir.is_dir():
        assets = configured.static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(configured.static_dir / "index.html")

    return app
