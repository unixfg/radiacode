import { afterEach, describe, expect, it, vi } from "vitest";

import { getHistoricalData } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("historical API bounds", () => {
  it("rejects an invalid range before making a request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const end = new Date("2026-08-16T12:00:00Z");
    await expect(getHistoricalData("rc-110", ["rc-110"], { start: end, end })).rejects.toThrow("valid range");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requests bounded server resolutions", async () => {
    const requested: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      requested.push(url);
      let body: object = { events: [] };
      if (url.includes("scalar-history")) body = { device: "rc-110", start: "", end: "", resolution_seconds: 60, points: [] };
      if (url.includes("/spectrum?")) body = { device: "rc-110", spectra: [], rebinned: false };
      if (url.includes("spectrogram")) body = { device: "rc-110", time_edges: [], energy_edges_kev: [], counts: [], source_resolution: "", rebinned: true };
      return { ok: true, json: async () => body } as Response;
    });

    await getHistoricalData(
      "rc-110",
      ["rc-110"],
      { start: new Date("2026-08-15T12:00:00Z"), end: new Date("2026-08-16T12:00:00Z") },
    );

    expect(requested.some((url) => url.includes("max_points=2000"))).toBe(true);
    expect(requested.some((url) => url.includes("time_bins=720") && url.includes("energy_bins=256"))).toBe(true);
  });

  it("preserves successful panels when one historical request fails", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("spectrogram")) throw new Error("temporary heatmap failure");
      let body: object = { events: [] };
      if (url.includes("scalar-history")) {
        body = {
          device: "rc-110",
          start: "2026-08-15T12:00:00Z",
          end: "2026-08-16T12:00:00Z",
          resolution_seconds: 60,
          points: [{ at: "2026-08-16T12:00:00Z", cps: null, dose_rate: null }],
        };
      }
      if (url.includes("/spectrum?")) body = { device: "rc-110", spectra: [], rebinned: false };
      return { ok: true, json: async () => body } as Response;
    });

    const result = await getHistoricalData(
      "rc-110",
      ["rc-110"],
      { start: new Date("2026-08-15T12:00:00Z"), end: new Date("2026-08-16T12:00:00Z") },
    );

    expect(result.complete).toBe(false);
    expect(result.failed).toEqual(["spectrogram"]);
    expect(result.data.scalar.points).toHaveLength(1);
    expect(result.data.spectrogram.counts).toEqual([]);
  });
});
