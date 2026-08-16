# Dashboard API contract

All responses are public, read-only JSON under `/api/v1`. Timestamps are RFC 3339 UTC strings. The server must never
include hardware serials, database identifiers, internal configuration, stack traces, or operational error details.
Non-2xx responses may use a generic public error body; the dashboard deliberately does not display its contents.

## Endpoints

- `GET /devices` returns `{ "devices": DeviceSummary[] }`.
- `GET /devices/{slug}/current` returns `CurrentState`. The dashboard polls this once every five seconds.
  `received_at` is null until the first valid real-time sample arrives.
- `GET /devices/{slug}/scalar-history?start={utc}&end={utc}&max_points=2000` returns `ScalarHistory`.
- `GET /devices/{slug}/events?start={utc}&end={utc}&limit=500` returns `{ "events": DeviceEvent[] }`.
- `GET /devices/{slug}/spectrum?start={utc}&end={utc}` returns `SpectrumResponse`. Each calibration epoch stays a
  separate spectrum; the browser never adds epochs together.
- `GET /spectrum-comparison?devices={comma-separated-slugs}&start={utc}&end={utc}&energy_bins=512` returns an
  energy-rebinned `ComparisonResponse`.
- `GET /devices/{slug}/spectrogram?start={utc}&end={utc}&time_bins=720&energy_bins=256` returns a rebinned
  `SpectrogramResponse`.

The UI limits ranges to 31 days. The API remains authoritative and must reject invalid/unbounded ranges, cap scalar
responses at 2,000 points, cap comparison/spectrogram energy bins at 512, and cap spectrogram time bins at 1,000.
Exact TypeScript shapes live in `src/types.ts` and are the normative field-name reference for the dashboard.
