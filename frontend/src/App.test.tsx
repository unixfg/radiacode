import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const devices = {
  devices: [
    { slug: "rc-110", name: "RadiaCode RC-110", model: "RC-110", firmware_version: "4.12", available: true, last_seen_at: "2026-08-16T12:00:00Z" },
    { slug: "rc-103g", name: "RadiaCode RC-103G", model: "RC-103G", firmware_version: "5.01", available: true, last_seen_at: "2026-08-16T12:00:00Z" },
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

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("public dashboard", () => {
  it("renders live values and historical panels from the public API", async () => {
    const fetchMock = mockApi();
    render(<App />);

    expect(screen.getByRole("heading", { name: "RadiaCode Observatory" })).toBeInTheDocument();
    expect(await screen.findByText("12.5")).toBeInTheDocument();
    expect(screen.getByText("CsI(Tl)")).toBeInTheDocument();
    expect(screen.getByText("GAGG(Ce)")).toBeInTheDocument();
    expect(screen.getByText("4.12")).toBeInTheDocument();
    expect(screen.getByText("5.01")).toBeInTheDocument();
    expect(screen.queryByText("Temperature")).not.toBeInTheDocument();
    expect(screen.queryByText("Battery")).not.toBeInTheDocument();
    expect(screen.queryByText(/Continuous environmental gamma radiation/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No device controls/)).not.toBeInTheDocument();
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

  it("advances a rolling range and discovers a newly completed spectrum frame", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-16T12:00:00Z"));
    const historyEnds: string[] = [];

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      const requestEnd = new URL(url, "http://dashboard.test").searchParams.get("end");
      if (url.includes("scalar-history")) {
        historyEnds.push(requestEnd!);
      }
      let body = responseFor(url);
      if (requestEnd === "2026-08-16T12:00:00.000Z") {
        if (url.includes("spectrum-comparison")) {
          body = { energy_edges_kev: [], series: [], rebinned: true };
        } else if (url.includes("/spectrogram")) {
          body = { device: "rc-110", time_edges: [], energy_edges_kev: [], counts: [], source_resolution: "frame", rebinned: true };
        } else if (url.includes("/spectrum")) {
          body = { device: "rc-110", spectra: [], rebinned: false };
        }
      }
      return { ok: true, json: async () => body } as Response;
    });

    render(<App />);
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(historyEnds).toHaveLength(1);
    expect(screen.getAllByText(/No completed 5-minute spectrum frame/)).not.toHaveLength(0);

    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(historyEnds).toHaveLength(2);
    expect(Date.parse(historyEnds[1]) - Date.parse(historyEnds[0])).toBe(60_000);
    expect(screen.getAllByRole("img", { name: "Energy spectrum" })).toHaveLength(2);

    await act(async () => vi.advanceTimersByTimeAsync(299_999));
    expect(historyEnds).toHaveLength(2);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(historyEnds).toHaveLength(3);
  });

  it("keeps a custom range fixed without periodic refreshes", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-16T12:00:00Z"));
    const historyEnds: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("scalar-history")) {
        historyEnds.push(new URL(url, "http://dashboard.test").searchParams.get("end")!);
      }
      return { ok: true, json: async () => responseFor(url) } as Response;
    });

    render(<App />);
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(historyEnds).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-08-15T08:00" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-08-15T10:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(historyEnds).toHaveLength(2);
    const fixedEnd = historyEnds[1];

    await act(async () => vi.advanceTimersByTimeAsync(300_000));
    expect(historyEnds).toHaveLength(2);
    expect(historyEnds[1]).toBe(fixedEnd);
  });
});
