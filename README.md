# RadiaCode Observatory

RadiaCode Observatory continuously acquires radiation telemetry and spectra from USB RadiaCode detectors. It combines a crash-safe Python collector, PostgreSQL history, public FastAPI service, React dashboard, standards-based exports, Prometheus metrics, and optional publish-only MQTT telemetry in one container image.

This repository contains the application. The private cluster configuration, USB serial mappings, credentials, CNPG cluster, and public route live in [`unixfg/gitops`](https://github.com/unixfg/gitops).

## Safety and data contract

- A detector is owned by exactly one serialized collector. The application never calls dose or spectrum reset and exposes no device-control API.
- `DATA_BUF` is read through the low-level `radiacode==0.4.0` request path. The raw response is committed to a SQLite WAL spool before decoding and before the atomic PostgreSQL batch transaction.
- Host `received_at` UTC is authoritative. Signed device ticks and optional batch-relative times remain available with explicit quality values.
- Raw DATA_BUF exposure values are retained in the batch bytes; decoded dose and dose-rate fields are normalized from R and R/h to µSv and µSv/h with the protocol's fixed ×10,000 conversion.
- Cumulative spectra are sampled once per minute and converted into exact, unsplit deltas. Frames remain separated by device and calibration epoch.
- Spectrum encoding version 1 is exactly `channel_count * 4` little-endian unsigned bytes plus duration, total, SHA-256, calibration, and quality metadata. The final channel is retained as hardware under/overflow metadata and excluded from calibrated analysis.
- Public models and exporters contain public slugs only. USB serials, database identifiers, configuration, and operational error details are never returned.

PostgreSQL is authoritative. Daily one-second/raw partitions are retained for 30 days; minute scalar rollups, events, calibration metadata, five-minute frames, and hourly/daily spectrum rollups are retained. Maintenance is idempotent and protected by a PostgreSQL advisory lock.

## Commands

```text
radiacode collector --device <slug>
radiacode reallocator
radiacode web
radiacode migrate
radiacode maintenance
radiacode usb-probe --device <slug>
```

All settings use the `RADIACODE_` prefix. Device collectors require a public `DEVICE_SLUG`, private `DEVICE_SERIAL`, and either `DATABASE_DSN` or the `DB_HOST`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` fields. The web process needs only the reader database credential. MQTT is disabled unless a complete `mqtts://` URL, username, password, and readable CA file are present.

The non-destructive USB probe performs the library-required initialization, reads current firmware/data/spectrum information, and never resets a counter or changes detector configuration.

## Public API and exports

The read-only API is under `/api/v1` and includes devices/current state, bounded scalar history, events, aggregate spectra, energy-rebinned comparisons, spectrogram tiles, and exports. Scalar responses are capped at 2,000 points; spectrograms at 1,000 time bins by 512 energy bins. The server selects stored resolution for longer ranges.

Supported export identifiers are:

- `n42-2012` (default, multi-spectrum)
- `npes-v2` (multi-spectrum)
- `csv` (multi-spectrum; leading energy/count columns import directly in Gamma MCA)
- `iaea-spe` (one spectrum per file)
- `radiacode-xml` (RadiaCode/BecqMoni-compatible, one spectrum per file)

Five-minute slices are available for N42, NPES, and CSV and are capped at 10,000 frames. Selections crossing calibration epochs remain separate; single-spectrum formats return a ZIP with one file per epoch.

## Development

Python 3.13 and Node 24 are the supported toolchains.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy

cd frontend
npm ci
npm test
npm run build
npx playwright test
```

Exporter tests validate representative N42-2012 and NPESv2 output against the bundled upstream schemas. CI additionally verifies count and calibration fidelity while round-tripping N42, IAEA SPE, and RadiaCode XML through a pinned SpecUtils build, then imports NPESv2 and CSV with Gamma MCA's pinned parser. Database integration applies migrations twice against PostgreSQL 17, verifies role boundaries, tests atomic batch/replay behavior, and checks idempotent rollups.

## Image provenance

CI must pass Python, frontend, database/migration, exporter, and interoperability jobs before publishing. The release workflow builds one GHCR manifest for `linux/amd64` and `linux/arm64`, emits an SBOM and provenance attestation, then signs the immutable digest with keyless Sigstore/cosign.

Published images carry OCI source, revision, and license labels. The dashboard's
source link is compiled against that same Git commit. Exact-source retrieval and
downstream build instructions are documented in [`SOURCE.md`](SOURCE.md).

## License

Copyright 2026 unixfg contributors.

Licensed under the GNU Affero General Public License version 3 only (`AGPL-3.0-only`).

Third-party components retain their original compatible licenses. Copyright,
license, source, and vendored-file provenance are recorded in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and [`LICENSES/`](LICENSES/).
