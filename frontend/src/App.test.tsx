import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const devices = {
  devices: [
    { slug: "rc-110", name: "RadiaCode RC-110", model: "RC-110", available: true, last_seen_at: "2026-08-16T12:00:00Z" },
    { slug: "rc-103g", name: "RadiaCode RC-103G", model: "RC-103G", available: true, last_seen_at: "2026-08-16T12:00:00Z" },
  ],
};

function responseFor(url: string): object {
  if (url.endsWith("/devices")) return devices;
  if (url.endsWith("/current")) {
    const device = url.includes("rc-103g") ? "rc-103g" : "rc-110";
    return {
      device,
      received_at: "2026-08-16T12:00:00Z",
      available: true,
      cps: device === "rc-110" ? 12.5 : 9.1,
      dose_rate: 0.08,
      cps_uncertainty_pct: 3.2,
      dose_rate_uncertainty_pct: 4.1,
      accumulated_dose: 4.2,
      accumulated_duration_seconds: 3600,
      temperature_c: 22.5,
      battery_pct: 87,
      charging: true,
      field_timestamps: {},
    };
  }
  if (url.includes("scalar-history")) {
    return {
      device: "rc-110",
      start: "2026-08-16T11:00:00Z",
      end: "2026-08-16T12:00:00Z",
      resolution_seconds: 60,
      points: [
        { at: "2026-08-16T11:00:00Z", cps: { min: 8, max: 14, avg: 11, latest: 12 }, dose_rate: { min: 0.05, max: 0.1, avg: 0.08, latest: 0.08 } },
        { at: "2026-08-16T12:00:00Z", cps: { min: 9, max: 15, avg: 12, latest: 12.5 }, dose_rate: { min: 0.06, max: 0.1, avg: 0.08, latest: 0.08 } },
      ],
    };
  }
  if (url.includes("/events")) return { events: [{ at: "2026-08-16T11:30:00Z", code: "reconnected", name: "Detector reconnected", parameter: null }] };
  if (url.includes("spectrum-comparison")) return { energy_edges_kev: [0, 100, 200], series: [{ device: "rc-110", counts: [3, 5], source_total: 8, coverage: 1 }, { device: "rc-103g", counts: [2, 6], source_total: 8, coverage: 1 }], rebinned: true };
  if (url.includes("/spectrogram")) return { device: "rc-110", time_edges: ["2026-08-16T11:00:00Z", "2026-08-16T11:30:00Z", "2026-08-16T12:00:00Z"], energy_edges_kev: [0, 100, 200], counts: [[1, 2], [3, 4]], source_resolution: "5 minute frames", rebinned: true };
  if (url.includes("/spectrum")) return { device: "rc-110", spectra: [{ epoch_started_at: "2026-08-01T00:00:00Z", start: "2026-08-16T11:00:00Z", end: "2026-08-16T12:00:00Z", duration_seconds: 3600, channel_count: 4, calibration: { a0: 0, a1: 1, a2: 0 }, counts: [1, 4, 2, 0], overflow_count: 0, quality_flags: [] }], rebinned: false };
  throw new Error(`Unhandled test URL: ${url}`);
}

function mockApi() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => ({
    ok: true,
    json: async () => responseFor(String(input)),
  }) as Response);
}

afterEach(() => vi.restoreAllMocks());

describe("public dashboard", () => {
  it("renders live values and historical panels from the public API", async () => {
    const fetchMock = mockApi();
    render(<App />);

    expect(screen.getByRole("heading", { name: "RadiaCode Observatory" })).toBeInTheDocument();
    expect(await screen.findByText("12.5")).toBeInTheDocument();
    expect(await screen.findByText("Detector reconnected")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "Energy spectrum" })).toHaveLength(2);
    expect(screen.getByRole("img", { name: /Time versus energy heatmap/ })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/devices/rc-110/current"))).toBe(true);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("max_points=2000"))).toBe(true);
  });

  it("never displays a backend or transport error body", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("postgres password leaked-in-error"));
    render(<App />);

    expect(await screen.findByText(/Dashboard data is temporarily unavailable/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/postgres password/)).not.toBeInTheDocument());
  });
});
