import type {
  ComparisonResponse,
  CurrentState,
  DeviceSummary,
  EventsResponse,
  HistoricalData,
  ScalarHistory,
  SpectrogramResponse,
  SpectrumResponse,
  TimeRange,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, "");
export const MAX_RANGE_MILLISECONDS = 31 * 24 * 60 * 60 * 1_000;

export class PublicApiError extends Error {
  constructor() {
    super("Dashboard data is temporarily unavailable.");
    this.name = "PublicApiError";
  }
}

export interface HistoricalFetch {
  data: HistoricalData;
  complete: boolean;
  failed: Array<keyof HistoricalData>;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) throw new PublicApiError();
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new PublicApiError();
  }
}

function checkedRange(range: TimeRange): URLSearchParams {
  const start = range.start.getTime();
  const end = range.end.getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) {
    throw new Error("Choose a valid range with an end after its start.");
  }
  if (end - start > MAX_RANGE_MILLISECONDS) {
    throw new Error("Choose a range of 31 days or less.");
  }
  return new URLSearchParams({ start: range.start.toISOString(), end: range.end.toISOString() });
}

function devicePath(slug: string): string {
  return `/devices/${encodeURIComponent(slug)}`;
}

export async function listDevices(signal?: AbortSignal): Promise<DeviceSummary[]> {
  const response = await getJson<{ devices: DeviceSummary[] }>("/devices", signal);
  return response.devices;
}

export function getCurrent(slug: string, signal?: AbortSignal): Promise<CurrentState> {
  return getJson<CurrentState>(`${devicePath(slug)}/current`, signal);
}

export async function getHistoricalData(
  slug: string,
  comparisonDevices: string[],
  range: TimeRange,
  signal?: AbortSignal,
): Promise<HistoricalFetch> {
  const params = checkedRange(range);
  const query = params.toString();
  const comparisonParams = new URLSearchParams(params);
  comparisonParams.set("devices", comparisonDevices.join(","));
  comparisonParams.set("energy_bins", "512");
  const spectrogramParams = new URLSearchParams(params);
  spectrogramParams.set("time_bins", "720");
  spectrogramParams.set("energy_bins", "256");
  const scalarParams = new URLSearchParams(params);
  scalarParams.set("max_points", "2000");
  const eventParams = new URLSearchParams(params);
  eventParams.set("limit", "500");

  const comparisonRequest =
    comparisonDevices.length > 1
      ? getJson<ComparisonResponse>(`/spectrum-comparison?${comparisonParams}`, signal)
      : Promise.resolve(null);

  const results = await Promise.allSettled([
    getJson<ScalarHistory>(`${devicePath(slug)}/scalar-history?${scalarParams}`, signal),
    getJson<EventsResponse>(`${devicePath(slug)}/events?${eventParams}`, signal),
    getJson<SpectrumResponse>(`${devicePath(slug)}/spectrum?${query}`, signal),
    getJson<SpectrumResponse>(`${devicePath(slug)}/spectrum?${query}&mode=latest`, signal),
    comparisonRequest,
    getJson<SpectrogramResponse>(`${devicePath(slug)}/spectrogram?${spectrogramParams}`, signal),
  ] as const);
  const [scalar, events, spectrum, latestSpectrum, comparison, spectrogram] = results;
  const emptySpectrum: SpectrumResponse = { device: slug, spectra: [], rebinned: false };
  const data: HistoricalData = {
    scalar:
      scalar.status === "fulfilled"
        ? scalar.value
        : {
            device: slug,
            start: range.start.toISOString(),
            end: range.end.toISOString(),
            resolution_seconds: 0,
            points: [],
          },
    events: events.status === "fulfilled" ? events.value : { events: [] },
    spectrum: spectrum.status === "fulfilled" ? spectrum.value : emptySpectrum,
    latestSpectrum:
      latestSpectrum.status === "fulfilled" ? latestSpectrum.value : emptySpectrum,
    comparison: comparison.status === "fulfilled" ? comparison.value : null,
    spectrogram:
      spectrogram.status === "fulfilled"
        ? spectrogram.value
        : {
            device: slug,
            time_edges: [],
            energy_edges_kev: [],
            counts: [],
            source_resolution: "unavailable",
            rebinned: true,
          },
  };
  return {
    data,
    complete: results.every((result) => result.status === "fulfilled"),
    failed: ([
      ["scalar", scalar],
      ["events", events],
      ["spectrum", spectrum],
      ["latestSpectrum", latestSpectrum],
      ["comparison", comparison],
      ["spectrogram", spectrogram],
    ] as const)
      .filter((entry) => entry[1].status === "rejected")
      .map((entry) => entry[0]),
  };
}
