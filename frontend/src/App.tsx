import { useEffect, useMemo, useRef, useState } from "react";

import { getCurrent, getHistoricalData, listDevices, PublicApiError } from "./api";
import { CurrentReadout } from "./components/CurrentReadout";
import { EventTimeline } from "./components/EventTimeline";
import { RangeControls } from "./components/RangeControls";
import { ScalarChart } from "./components/ScalarChart";
import { Spectrogram } from "./components/Spectrogram";
import { ComparisonChart, SpectrumChart } from "./components/SpectrumCharts";
import { formatLocalDate } from "./format";
import type { CurrentState, DeviceSummary, HistoricalData, TimeRange } from "./types";

function initialRange(): TimeRange {
  const end = new Date();
  return { start: new Date(end.getTime() - 24 * 60 * 60 * 1_000), end };
}

function SectionHeading({ eyebrow, title, detail }: { eyebrow: string; title: string; detail?: string }) {
  return (
    <header className="panel-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
      </div>
      {detail && <span className="panel-heading__detail">{detail}</span>}
    </header>
  );
}

function App() {
  const [devices, setDevices] = useState<DeviceSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [current, setCurrent] = useState<Record<string, CurrentState>>({});
  const [history, setHistory] = useState<HistoricalData | null>(null);
  const [range, setRange] = useState<TimeRange>(initialRange);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [deviceRetry, setDeviceRetry] = useState(0);
  const [historyRetry, setHistoryRetry] = useState(0);
  const [deviceError, setDeviceError] = useState(false);
  const [currentError, setCurrentError] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [spectrumMode, setSpectrumMode] = useState<"latest" | "range">("latest");
  const [logarithmic, setLogarithmic] = useState(true);
  const historyRequestKey = useRef("");

  useEffect(() => {
    const controller = new AbortController();
    let retryTimer: number | undefined;
    listDevices(controller.signal)
      .then((items) => {
        setDevices(items);
        setSelected((value) =>
          value && items.some((item) => item.slug === value)
            ? value
            : items[0]?.slug ?? null,
        );
        setDeviceError(false);
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setDeviceError(true);
          retryTimer = window.setTimeout(() => setDeviceRetry((value) => value + 1), 5_000);
        }
      });
    return () => {
      controller.abort();
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [deviceRetry]);

  useEffect(() => {
    if (devices.length === 0) return;
    let active = true;
    let inFlight = false;
    const controllers = new Set<AbortController>();
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      const controller = new AbortController();
      controllers.add(controller);
      const timeout = window.setTimeout(() => controller.abort(), 4_500);
      const results = await Promise.allSettled(
        devices.map((device) => getCurrent(device.slug, controller.signal)),
      );
      window.clearTimeout(timeout);
      controllers.delete(controller);
      inFlight = false;
      if (!active) return;
      const updates: Record<string, CurrentState> = {};
      results.forEach((result) => {
        if (result.status === "fulfilled") updates[result.value.device] = result.value;
      });
      setCurrent((existing) => {
        const next = { ...existing, ...updates };
        results.forEach((result, index) => {
          if (result.status === "rejected") {
            const slug = devices[index].slug;
            const cached = next[slug];
            const receivedAtValue = cached?.received_at ?? devices[index].last_seen_at;
            const receivedAt = receivedAtValue ? Date.parse(receivedAtValue) : Number.NaN;
            next[slug] = {
              device: slug,
              received_at: receivedAtValue,
              available: Number.isFinite(receivedAt) && Date.now() - receivedAt <= 10_000,
              cps: cached?.cps ?? null,
              dose_rate: cached?.dose_rate ?? null,
              cps_uncertainty_pct: cached?.cps_uncertainty_pct ?? null,
              dose_rate_uncertainty_pct: cached?.dose_rate_uncertainty_pct ?? null,
              accumulated_dose: cached?.accumulated_dose ?? null,
              accumulated_duration_seconds: cached?.accumulated_duration_seconds ?? null,
              temperature_c: cached?.temperature_c ?? null,
              battery_pct: cached?.battery_pct ?? null,
              charging: cached?.charging ?? null,
              field_timestamps: cached?.field_timestamps ?? {},
            };
          }
        });
        return next;
      });
      if (Object.keys(updates).length > 0) {
        setLastRefresh(new Date());
      }
      if (results.some((result) => result.status === "rejected")) {
        setCurrentError(true);
      } else {
        setCurrentError(false);
      }
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 5_000);
    return () => {
      active = false;
      window.clearInterval(interval);
      controllers.forEach((controller) => controller.abort());
    };
  }, [devices]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let retryTimer: number | undefined;
    const requestKey = `${selected}:${range.start.toISOString()}:${range.end.toISOString()}`;
    if (historyRequestKey.current !== requestKey) {
      historyRequestKey.current = requestKey;
      setHistory(null);
    }
    setHistoryLoading(true);
    getHistoricalData(selected, devices.map((device) => device.slug), range, controller.signal)
      .then((result) => {
        setHistory((previous) => {
          if (previous === null || result.failed.length === 0) return result.data;
          const failed = new Set<keyof HistoricalData>(result.failed);
          return {
            scalar: failed.has("scalar") ? previous.scalar : result.data.scalar,
            events: failed.has("events") ? previous.events : result.data.events,
            spectrum: failed.has("spectrum") ? previous.spectrum : result.data.spectrum,
            latestSpectrum: failed.has("latestSpectrum")
              ? previous.latestSpectrum
              : result.data.latestSpectrum,
            comparison: failed.has("comparison") ? previous.comparison : result.data.comparison,
            spectrogram: failed.has("spectrogram")
              ? previous.spectrogram
              : result.data.spectrogram,
          };
        });
        setHistoryError(!result.complete);
        if (!result.complete) {
          retryTimer = window.setTimeout(() => setHistoryRetry((value) => value + 1), 5_000);
        }
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setHistoryError(true);
          retryTimer = window.setTimeout(() => setHistoryRetry((value) => value + 1), 5_000);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setHistoryLoading(false);
      });
    return () => {
      controller.abort();
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [devices, historyRetry, range, selected]);

  const selectedDevice = devices.find((device) => device.slug === selected);
  const publicError = deviceError || currentError || historyError;
  const displayedSpectra = useMemo(() => {
    if (spectrumMode === "range") return history?.spectrum.spectra ?? [];
    return history?.latestSpectrum.spectra ?? [];
  }, [history, spectrumMode]);

  return (
    <div className="app-shell">
      <header className="masthead">
        <a href="#main" className="skip-link">Skip to dashboard</a>
        <div className="brand-mark" aria-hidden="true"><span /><span /><span /></div>
        <div className="masthead__title">
          <span className="eyebrow">DOESTHINGS.ONLINE · PUBLIC TELEMETRY</span>
          <h1>RadiaCode Observatory</h1>
          <p>Continuous environmental gamma radiation, measured independently by two detectors.</p>
        </div>
        <div className="refresh-state" aria-live="polite">
          <span className={devices.some((device) => current[device.slug]?.available) ? "pulse" : "pulse pulse--quiet"} />
          <div><strong>Live acquisition</strong><small>Updated {formatLocalDate(lastRefresh)}</small></div>
        </div>
      </header>

      {publicError && (
        <div className="public-error" role="status">
          {new PublicApiError().message} Retrying automatically.
        </div>
      )}

      <main id="main">
        <section className="live-section" aria-labelledby="live-title">
          <div className="section-intro">
            <div><span className="eyebrow">NOW</span><h2 id="live-title">Current conditions</h2></div>
            <p>Availability requires a valid realtime sample within the last ten seconds.</p>
          </div>
          <div className="detector-grid">
            {devices.length > 0
              ? devices.map((device) => <CurrentReadout key={device.slug} device={device} state={current[device.slug]} />)
              : [0, 1].map((item) => <div className="detector-card skeleton" key={item} aria-hidden="true" />)}
          </div>
        </section>

        <section className="history-toolbar" aria-labelledby="history-title">
          <div className="section-intro">
            <div><span className="eyebrow">ARCHIVE</span><h2 id="history-title">Explore the record</h2></div>
            <p>Times are shown in your browser’s local timezone.</p>
          </div>
          <div className="device-tabs" role="tablist" aria-label="Detector">
            {devices.map((device) => (
              <button
                type="button"
                role="tab"
                aria-selected={selected === device.slug}
                key={device.slug}
                onClick={() => setSelected(device.slug)}
              >
                {device.name}
              </button>
            ))}
          </div>
          <RangeControls range={range} onChange={setRange} />
        </section>

        <div className={`dashboard-grid ${historyLoading ? "dashboard-grid--loading" : ""}`} aria-busy={historyLoading}>
          <section className="panel panel--wide">
            <SectionHeading
              eyebrow="SCALAR HISTORY"
              title={`${selectedDevice?.name ?? "Detector"} activity`}
              detail={history ? `${history.scalar.resolution_seconds}s server-selected resolution` : undefined}
            />
            <div className="scalar-grid">
              <ScalarChart
                points={history?.scalar.points ?? []}
                metric="cps"
                label="Count rate"
                unit="CPS"
                color="#62dba6"
                resolutionSeconds={history?.scalar.resolution_seconds ?? 0}
              />
              <ScalarChart
                points={history?.scalar.points ?? []}
                metric="dose_rate"
                label="Dose rate"
                unit="µSv/h"
                color="#f1b85b"
                resolutionSeconds={history?.scalar.resolution_seconds ?? 0}
              />
            </div>
          </section>

          <section className="panel panel--timeline">
            <SectionHeading eyebrow="EVENTS" title="Detector timeline" detail={`${history?.events.events.length ?? 0} in range`} />
            <EventTimeline events={history?.events.events ?? []} />
          </section>

          <section className="panel panel--full">
            <div className="panel-heading panel-heading--controls">
              <div><span className="eyebrow">ENERGY SPECTRUM</span><h2>Gamma energy distribution</h2></div>
              <div className="segmented-controls">
                <div role="group" aria-label="Spectrum period">
                  <button type="button" aria-pressed={spectrumMode === "latest"} onClick={() => setSpectrumMode("latest")}>Latest</button>
                  <button type="button" aria-pressed={spectrumMode === "range"} onClick={() => setSpectrumMode("range")}>Range</button>
                </div>
                <div role="group" aria-label="Count scale">
                  <button type="button" aria-pressed={!logarithmic} onClick={() => setLogarithmic(false)}>Linear</button>
                  <button type="button" aria-pressed={logarithmic} onClick={() => setLogarithmic(true)}>Log</button>
                </div>
              </div>
            </div>
            <SpectrumChart spectra={displayedSpectra} logarithmic={logarithmic} />
            {displayedSpectra.length > 0 && (
              <p className="plot-note">The final hardware channel is reported as overflow metadata and excluded from the calibrated plot.</p>
            )}
          </section>

          <section className="panel panel--full">
            <SectionHeading eyebrow="CALIBRATED COMPARISON" title="RC-110 vs RC-103G" detail="Count-conserving energy rebin" />
            {history?.comparison
              ? <ComparisonChart comparison={history.comparison} logarithmic={logarithmic} />
              : <div className="chart-empty">Two detectors are required for comparison</div>}
            <p className="plot-note">Counts are rebinned onto common energy edges by the server; source spectra are never added channel-for-channel.</p>
          </section>

          <section className="panel panel--full">
            <SectionHeading
              eyebrow="SPECTROGRAM"
              title="Radiation through time and energy"
              detail={history?.spectrogram.source_resolution}
            />
            {history?.spectrogram
              ? <Spectrogram data={history.spectrogram} />
              : <div className="chart-empty chart-empty--tall">No spectrogram bins in this range</div>}
            <p className="plot-note">Color intensity uses a logarithmic count scale. Energy bins are explicitly rebinned.</p>
          </section>
        </div>
      </main>

      <footer className="site-footer">
        <p>
          Open environmental radiation data · Read-only public dashboard ·{" "}
          <a href="https://github.com/unixfg/radiacode">AGPL-3.0 source</a>
        </p>
        <p>No device controls, serial numbers, or operational details are exposed.</p>
      </footer>
    </div>
  );
}

export default App;
