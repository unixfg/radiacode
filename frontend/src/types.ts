export interface DeviceSummary {
  slug: string;
  name: string;
  model: string;
  available: boolean;
  last_seen_at: string | null;
}

export interface CurrentState {
  device: string;
  received_at: string | null;
  available: boolean;
  cps: number | null;
  dose_rate: number | null;
  cps_uncertainty_pct: number | null;
  dose_rate_uncertainty_pct: number | null;
  accumulated_dose: number | null;
  accumulated_duration_seconds: number | null;
  temperature_c: number | null;
  battery_pct: number | null;
  charging: boolean | null;
  field_timestamps: Record<string, string>;
}

export interface ScalarAggregate {
  min: number;
  max: number;
  avg: number;
  latest: number;
}

export interface ScalarPoint {
  at: string;
  cps: ScalarAggregate | null;
  dose_rate: ScalarAggregate | null;
}

export interface ScalarHistory {
  device: string;
  start: string;
  end: string;
  resolution_seconds: number;
  points: ScalarPoint[];
}

export interface DeviceEvent {
  at: string;
  code: string;
  name: string;
  parameter: string | number | boolean | null;
}

export interface EventsResponse {
  events: DeviceEvent[];
}

export interface Calibration {
  a0: number;
  a1: number;
  a2: number;
}

export interface SpectrumEpoch {
  epoch_started_at: string;
  start: string;
  end: string;
  duration_seconds: number;
  channel_count: number;
  calibration: Calibration;
  counts: number[];
  overflow_count: number;
  quality_flags: string[];
}

export interface SpectrumResponse {
  device: string;
  spectra: SpectrumEpoch[];
  rebinned: false;
}

export interface ComparisonSeries {
  device: string;
  counts: number[];
  source_total: number;
  coverage: number;
}

export interface ComparisonResponse {
  energy_edges_kev: number[];
  series: ComparisonSeries[];
  rebinned: true;
}

export interface SpectrogramResponse {
  device: string;
  time_edges: string[];
  energy_edges_kev: number[];
  counts: number[][];
  source_resolution: string;
  rebinned: true;
}

export interface TimeRange {
  start: Date;
  end: Date;
}

export interface HistoricalData {
  scalar: ScalarHistory;
  events: EventsResponse;
  spectrum: SpectrumResponse;
  latestSpectrum: SpectrumResponse;
  comparison: ComparisonResponse | null;
  spectrogram: SpectrogramResponse;
}
