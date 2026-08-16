import { energyForChannel, formatNumber } from "../format";
import type { ComparisonResponse, SpectrumEpoch } from "../types";

interface SpectrumProps {
  spectra: SpectrumEpoch[];
  logarithmic: boolean;
}

interface Series {
  label: string;
  color: string;
  points: Array<[number, number]>;
}

const COLORS = ["#6ce5b1", "#ffbd62", "#7eb6ff", "#ec77aa", "#b39cff"];
const WIDTH = 900;
const HEIGHT = 300;
const PAD = { top: 20, right: 22, bottom: 42, left: 58 };

function SpectrumPlot({
  series,
  logarithmic,
  emptyMessage,
}: {
  series: Series[];
  logarithmic: boolean;
  emptyMessage: string;
}) {
  const all = series.flatMap((item) => item.points);
  if (all.length === 0) return <div className="chart-empty">{emptyMessage}</div>;
  const minX = Math.min(...all.map(([x]) => x));
  const maxX = Math.max(...all.map(([x]) => x));
  const displayY = (value: number) => (logarithmic ? Math.log10(value + 1) : value);
  const maxY = Math.max(...all.map(([, value]) => displayY(value)), 1);
  const xScale = (value: number) => PAD.left + ((value - minX) / Math.max(1e-9, maxX - minX)) * (WIDTH - PAD.left - PAD.right);
  const yScale = (value: number) => HEIGHT - PAD.bottom - (displayY(value) / maxY) * (HEIGHT - PAD.top - PAD.bottom);

  return (
    <div className="spectrum-plot">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Energy spectrum">
        {[0, 0.25, 0.5, 0.75, 1].map((fraction) => {
          const y = HEIGHT - PAD.bottom - fraction * (HEIGHT - PAD.top - PAD.bottom);
          const displayValue = maxY * fraction;
          const value = logarithmic ? 10 ** displayValue - 1 : displayValue;
          return (
            <g key={fraction}>
              <line className="grid-line" x1={PAD.left} x2={WIDTH - PAD.right} y1={y} y2={y} />
              <text className="axis-label" x={PAD.left - 9} y={y + 4} textAnchor="end">{formatNumber(value, 0)}</text>
            </g>
          );
        })}
        {series.map((item) => {
          const path = item.points
            .map(([x, y], index) => `${index ? "L" : "M"}${xScale(x).toFixed(2)},${yScale(y).toFixed(2)}`)
            .join(" ");
          return <path key={item.label} d={path} fill="none" stroke={item.color} strokeWidth="1.7" vectorEffect="non-scaling-stroke" />;
        })}
        <text className="axis-label" x={PAD.left} y={HEIGHT - 11}>{formatNumber(minX, 0)} keV</text>
        <text className="axis-label" x={WIDTH - PAD.right} y={HEIGHT - 11} textAnchor="end">{formatNumber(maxX, 0)} keV</text>
        <text className="axis-label" transform={`translate(16 ${HEIGHT / 2}) rotate(-90)`} textAnchor="middle">Counts</text>
      </svg>
      <div className="chart-legend">
        {series.map((item) => (
          <span key={item.label}><i style={{ background: item.color }} />{item.label}</span>
        ))}
      </div>
    </div>
  );
}

export function SpectrumChart({ spectra, logarithmic }: SpectrumProps) {
  const series = spectra.map((spectrum, index) => ({
    label: `${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(spectrum.start))} epoch`,
    color: COLORS[index % COLORS.length],
    // The final physical channel is under/overflow metadata and is excluded
    // from calibrated plots. It remains available as overflow_count in the API.
    points: spectrum.counts.slice(0, -1).map((count, channel) => [energyForChannel(channel, spectrum.calibration), count] as [number, number]),
  }));
  return (
    <SpectrumPlot
      series={series}
      logarithmic={logarithmic}
      emptyMessage="No completed 5-minute spectrum frame in this range yet"
    />
  );
}

export function ComparisonChart({ comparison, logarithmic }: { comparison: ComparisonResponse; logarithmic: boolean }) {
  const series = comparison.series.map((item, index) => ({
    label: `${item.device} · ${formatNumber(item.coverage * 100, 0)}% coverage`,
    color: COLORS[index % COLORS.length],
    points: item.counts.map((count, bin) => {
      const start = comparison.energy_edges_kev[bin];
      const end = comparison.energy_edges_kev[bin + 1];
      return [start + (end - start) / 2, count] as [number, number];
    }),
  }));
  return (
    <SpectrumPlot
      series={series}
      logarithmic={logarithmic}
      emptyMessage="No completed 5-minute spectrum frames from both detectors in this range yet"
    />
  );
}
