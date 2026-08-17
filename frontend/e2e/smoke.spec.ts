import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const url = route.request().url();
    let body: object;
    if (url.endsWith("/devices")) {
      body = { devices: [
        { slug: "rc-110", name: "RadiaCode RC-110", model: "RC-110", firmware_version: "4.12", available: true, last_seen_at: "2026-08-16T12:00:00Z" },
        { slug: "rc-103g", name: "RadiaCode RC-103G", model: "RC-103G", firmware_version: "5.01", available: true, last_seen_at: "2026-08-16T12:00:00Z" },
      ] };
    } else if (url.endsWith("/device-states")) {
      body = { states: [
        { device: "rc-110", received_at: "2026-08-16T12:00:00Z", available: true, cps: 12.5, dose_rate: 0.08, cps_uncertainty_pct: 3.2, dose_rate_uncertainty_pct: 4.1, accumulated_dose: 4.2, accumulated_duration_seconds: 3600, temperature_c: 22.5, battery_pct: 87, charging: true, field_timestamps: {} },
        { device: "rc-103g", received_at: "2026-08-16T12:00:00Z", available: true, cps: 12.5, dose_rate: 0.08, cps_uncertainty_pct: 3.2, dose_rate_uncertainty_pct: 4.1, accumulated_dose: 4.2, accumulated_duration_seconds: 3600, temperature_c: 22.5, battery_pct: 87, charging: true, field_timestamps: {} },
      ] };
    } else if (url.includes("scalar-history")) {
      body = { device: "rc-110", start: "2026-08-16T11:00:00Z", end: "2026-08-16T12:00:00Z", resolution_seconds: 60, points: [{ at: "2026-08-16T11:00:00Z", cps: { min: 8, max: 14, avg: 11, latest: 12 }, dose_rate: { min: 0.05, max: 0.1, avg: 0.08, latest: 0.08 } }, { at: "2026-08-16T12:00:00Z", cps: { min: 9, max: 15, avg: 12, latest: 12.5 }, dose_rate: { min: 0.06, max: 0.1, avg: 0.08, latest: 0.08 } }] };
    } else if (url.includes("/events")) {
      body = { events: [{ at: "2026-08-16T11:30:00Z", code: "reconnected", name: "Detector reconnected", parameter: null }] };
    } else if (url.includes("spectrum-comparison")) {
      body = { energy_edges_kev: [0, 100, 200], series: [{ device: "rc-110", counts: [3, 5], source_total: 8, coverage: 1 }, { device: "rc-103g", counts: [2, 6], source_total: 8, coverage: 1 }], rebinned: true };
    } else if (url.includes("/spectrogram")) {
      body = { device: "rc-110", time_edges: ["2026-08-16T11:00:00Z", "2026-08-16T11:30:00Z", "2026-08-16T12:00:00Z"], energy_edges_kev: [0, 100, 200], counts: [[1, 2], [3, 4]], source_resolution: "5 minute frames", rebinned: true };
    } else {
      body = { device: "rc-110", spectra: [{ epoch_started_at: "2026-08-01T00:00:00Z", start: "2026-08-16T11:00:00Z", end: "2026-08-16T12:00:00Z", duration_seconds: 3600, channel_count: 4, calibration: { a0: 0, a1: 1, a2: 0 }, counts: [1, 4, 2, 0], overflow_count: 0, quality_flags: [] }], rebinned: false };
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
});

test("shows live and historical detector data", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "RadiaCode Observatory" })).toBeVisible();
  await expect(page.locator("img.brand-mark")).toBeVisible();
  const faviconHref = await page.locator('link[rel="icon"]').getAttribute("href");
  expect(faviconHref).toBeTruthy();
  const favicon = await page.request.get(new URL(faviconHref!, page.url()).toString());
  expect(favicon.ok()).toBeTruthy();
  expect(favicon.headers()["content-type"]).toContain("image/svg+xml");
  await expect(page.getByText("12.5").first()).toBeVisible();
  await expect(page.getByText("CsI(Tl)")).toBeVisible();
  await expect(page.getByText("GAGG(Ce)")).toBeVisible();
  await expect(page.getByText("4.12")).toBeVisible();
  await expect(page.getByText("5.01")).toBeVisible();
  await expect(page.getByRole("heading", { name: "RC-110 vs RC-103G" })).toBeVisible();
  await expect(page.getByRole("img", { name: /Time versus energy heatmap/ })).toBeVisible();
});
