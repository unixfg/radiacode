import { formatLocalDate, formatNumber } from "../format";
import type { ScalarAggregate, ScalarPoint } from "../types";

interface Props {
  points: ScalarPoint[];
  metric: "cps" | "dose_rate";
  label: string;
  unit: string;
  color: string;
  resolutionSeconds: number;
}

const WIDTH = 760;
const HEIGHT = 220;
const PADDING = { top: 18, right: 18, bottom: 34, left: 54 };

function pathFor(
  points: Array<{ x: number; value: number }>,
  minX: number,
  maxX: number,
  maxY: number,
): string {
  return points
    .map(({ x, value }, index) => {
      const px = PADDING.left + ((x - minX) / Math.max(1, maxX - minX)) * (WIDTH - PADDING.left - PADDING.right);
      const py = HEIGHT - PADDING.bottom - (value / Math.max(1e-9, maxY)) * (HEIGHT - PADDING.top - PADDING.bottom);
      return `${index === 0 ? "M" : "L"}${px.toFixed(2)},${py.toFixed(2)}`;
    })
    .join(" ");
}

export function ScalarChart({ points, metric, label, unit, color, resolutionSeconds }: Props) {
  const usable = points
    .map((point) => ({ at: new Date(point.at), aggregate: point[metric] as ScalarAggregate | null }))
    .filter((point) => Number.isFinite(point.at.getTime()) && point.aggregate !== null);
  if (usable.length === 0) return <div className="chart-empty">No samples in this range</div>;

  const minX = usable[0].at.getTime();
  const maxX = usable.at(-1)!.at.getTime();
  const maxY = Math.max(...usable.map(({ aggregate }) => aggregate!.max), 0.001);
  const segments: Array<typeof usable> = [];
  for (const point of usable) {
    const previous = segments.at(-1)?.at(-1);
    const gap = previous ? point.at.getTime() - previous.at.getTime() : 0;
    if (!previous || resolutionSeconds <= 0 || gap <= resolutionSeconds * 1_500) {
      if (segments.length === 0) segments.push([]);
      segments.at(-1)!.push(point);
    } else {
      segments.push([point]);
    }
  }

  return (
    <figure className="chart">
      <figcaption>
        <span>{label}</span>
        <strong>{formatNumber(usable.at(-1)!.aggregate!.latest, metric === "cps" ? 1 : 3)} {unit}</strong>
      </figcaption>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${label} history`}>
        {[0, 0.5, 1].map((fraction) => {
          const y = HEIGHT - PADDING.bottom - fraction * (HEIGHT - PADDING.top - PADDING.bottom);
          return (
            <g key={fraction}>
              <line className="grid-line" x1={PADDING.left} x2={WIDTH - PADDING.right} y1={y} y2={y} />
              <text className="axis-label" x={PADDING.left - 8} y={y + 4} textAnchor="end">
                {formatNumber(maxY * fraction, metric === "cps" ? 0 : 2)}
              </text>
            </g>
          );
        })}
        {segments.map((segment, index) => {
          const line = pathFor(
            segment.map(({ at, aggregate }) => ({ x: at.getTime(), value: aggregate!.avg })),
            minX,
            maxX,
            maxY,
          );
          const upper = pathFor(
            segment.map(({ at, aggregate }) => ({ x: at.getTime(), value: aggregate!.max })),
            minX,
            maxX,
            maxY,
          );
          const lower = pathFor(
            [...segment].reverse().map(({ at, aggregate }) => ({ x: at.getTime(), value: aggregate!.min })),
            minX,
            maxX,
            maxY,
          ).replace(/^M/, "L");
          return (
            <g key={`${segment[0].at.toISOString()}-${index}`}>
              <path d={`${upper} ${lower} Z`} fill={color} opacity="0.12" />
              <path d={line} fill="none" stroke={color} strokeWidth="2.5" vectorEffect="non-scaling-stroke" />
            </g>
          );
        })}
        <text className="axis-label" x={PADDING.left} y={HEIGHT - 8}>{formatLocalDate(usable[0].at)}</text>
        <text className="axis-label" x={WIDTH - PADDING.right} y={HEIGHT - 8} textAnchor="end">
          {formatLocalDate(usable.at(-1)!.at)}
        </text>
      </svg>
    </figure>
  );
}
