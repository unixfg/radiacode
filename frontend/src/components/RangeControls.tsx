import { useEffect, useState } from "react";

import { fromLocalInput, toLocalInput } from "../format";
import type { TimeRange } from "../types";

interface Props {
  range: TimeRange;
  onChange: (range: TimeRange) => void;
}

const PRESETS = [
  ["1h", 60 * 60 * 1_000],
  ["6h", 6 * 60 * 60 * 1_000],
  ["24h", 24 * 60 * 60 * 1_000],
  ["7d", 7 * 24 * 60 * 60 * 1_000],
  ["30d", 30 * 24 * 60 * 60 * 1_000],
] as const;

export function RangeControls({ range, onChange }: Props) {
  const [start, setStart] = useState(toLocalInput(range.start));
  const [end, setEnd] = useState(toLocalInput(range.end));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setStart(toLocalInput(range.start));
    setEnd(toLocalInput(range.end));
  }, [range]);

  function applyCustom() {
    const next = { start: fromLocalInput(start), end: fromLocalInput(end) };
    const duration = next.end.getTime() - next.start.getTime();
    if (!Number.isFinite(duration) || duration <= 0 || duration > 31 * 24 * 60 * 60 * 1_000) {
      setError("Select a range up to 31 days with the end after the start.");
      return;
    }
    setError(null);
    onChange(next);
  }

  return (
    <div className="range-controls" aria-label="History range">
      <div className="preset-list">
        {PRESETS.map(([label, milliseconds]) => (
          <button
            key={label}
            type="button"
            onClick={() => {
              const endAt = new Date();
              onChange({ start: new Date(endAt.getTime() - milliseconds), end: endAt });
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="custom-range">
        <label>
          <span>From</span>
          <input type="datetime-local" value={start} onChange={(event) => setStart(event.target.value)} />
        </label>
        <label>
          <span>To</span>
          <input type="datetime-local" value={end} onChange={(event) => setEnd(event.target.value)} />
        </label>
        <button type="button" className="button--accent" onClick={applyCustom}>
          Apply
        </button>
      </div>
      {error && <p className="input-error">{error}</p>}
    </div>
  );
}
