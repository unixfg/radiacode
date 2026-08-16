import { formatDuration, formatLocalDate, formatNumber } from "../format";
import type { CurrentState, DeviceSummary } from "../types";

interface Props {
  device: DeviceSummary;
  state?: CurrentState;
}

function Metric({ label, value, unit, detail }: { label: string; value: string; unit?: string; detail?: string }) {
  return (
    <div className="metric">
      <span className="metric__label">{label}</span>
      <span className="metric__value">
        {value} {unit && <small>{unit}</small>}
      </span>
      {detail && <span className="metric__detail">{detail}</span>}
    </div>
  );
}

export function CurrentReadout({ device, state }: Props) {
  const available = state?.available ?? device.available;
  const uncertaintyCps = state?.cps_uncertainty_pct;
  const uncertaintyDose = state?.dose_rate_uncertainty_pct;
  const observed = (field: string): string | undefined => {
    const timestamp = state?.field_timestamps[field];
    return timestamp ? `As of ${formatLocalDate(timestamp)}` : undefined;
  };
  return (
    <article className={`detector-card ${available ? "detector-card--online" : "detector-card--offline"}`}>
      <header className="detector-card__header">
        <div>
          <span className="eyebrow">{device.model}</span>
          <h2>{device.name}</h2>
        </div>
        <span className="status-pill" aria-label={available ? "Available" : "Unavailable"}>
          <i aria-hidden="true" /> {available ? "Live" : "Offline"}
        </span>
      </header>
      <div className="primary-readings">
        <Metric
          label="Count rate"
          value={formatNumber(state?.cps, 1)}
          unit="CPS"
          detail={uncertaintyCps == null ? "Uncertainty unavailable" : `± ${formatNumber(uncertaintyCps, 1)}%`}
        />
        <Metric
          label="Dose rate"
          value={formatNumber(state?.dose_rate, 3)}
          unit="µSv/h"
          detail={uncertaintyDose == null ? "Uncertainty unavailable" : `± ${formatNumber(uncertaintyDose, 1)}%`}
        />
      </div>
      <div className="secondary-readings">
        <Metric
          label="Accumulated"
          value={formatNumber(state?.accumulated_dose, 2)}
          unit="µSv"
          detail={observed("accumulated_dose")}
        />
        <Metric
          label="Duration"
          value={formatDuration(state?.accumulated_duration_seconds)}
          detail={observed("accumulated_duration_seconds")}
        />
        <Metric
          label="Temperature"
          value={formatNumber(state?.temperature_c, 1)}
          unit="°C"
          detail={observed("temperature_c")}
        />
        <Metric
          label="Battery"
          value={formatNumber(state?.battery_pct, 0)}
          unit="%"
          detail={[
            state?.charging == null ? undefined : state.charging ? "Charging" : "On battery",
            observed("battery_pct"),
          ].filter(Boolean).join(" · ") || undefined}
        />
      </div>
      <footer>Last valid sample {formatLocalDate(state?.received_at ?? device.last_seen_at)}</footer>
    </article>
  );
}
