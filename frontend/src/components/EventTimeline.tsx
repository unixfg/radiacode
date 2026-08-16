import { formatLocalDate } from "../format";
import type { DeviceEvent } from "../types";

interface TimelineEntry {
  event: DeviceEvent;
  count: number;
  firstAt: string;
  lastAt: string;
}

const GAP_GROUP_WINDOW_MS = 5 * 60 * 1_000;
const SPECTRUM_GAP_CODES = new Set([
  "calibration_change",
  "channel_count_change",
  "counts_changed_without_duration_change",
  "data_buf_sequence_gap",
  "duration_regression",
]);

function gapSignature(event: DeviceEvent): string | null {
  const code = event.code.toLowerCase();
  const name = event.name.toLowerCase();
  const isGap = SPECTRUM_GAP_CODES.has(code) || code.includes("gap") || name.includes("gap");
  return isGap ? `${code}\u0000${name}` : null;
}

export function summarizeTimelineEvents(events: DeviceEvent[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  const latestGroupBySignature = new Map<string, TimelineEntry>();

  for (const event of events) {
    const signature = gapSignature(event);
    const at = Date.parse(event.at);
    const prior = signature ? latestGroupBySignature.get(signature) : undefined;
    const priorLatest = prior ? Date.parse(prior.lastAt) : Number.NaN;
    const canGroup =
      prior !== undefined &&
      Number.isFinite(at) &&
      Number.isFinite(priorLatest) &&
      Math.abs(priorLatest - at) <= GAP_GROUP_WINDOW_MS;

    if (canGroup) {
      prior.count += 1;
      if (at < Date.parse(prior.firstAt)) prior.firstAt = event.at;
      if (at > Date.parse(prior.lastAt)) prior.lastAt = event.at;
      continue;
    }

    const entry = { event, count: 1, firstAt: event.at, lastAt: event.at };
    entries.push(entry);
    if (signature) latestGroupBySignature.set(signature, entry);
  }

  return entries.slice(0, 100);
}

export function EventTimeline({ events }: { events: DeviceEvent[] }) {
  if (events.length === 0) return <div className="chart-empty">No detector events in this range</div>;
  const entries = summarizeTimelineEvents(events);
  return (
    <ol className="timeline">
      {entries.map(({ event, count, firstAt, lastAt }, index) => (
        <li key={`${event.at}-${event.code}-${index}`}>
          <i aria-hidden="true" />
          <div>
            <strong>{event.name}</strong>
            <time dateTime={event.at}>
              {count === 1
                ? formatLocalDate(event.at)
                : `${formatLocalDate(firstAt)} – ${formatLocalDate(lastAt)}`}
            </time>
            {count > 1 && <p>{count} similar gap events grouped</p>}
            {event.parameter !== null && <p>{String(event.parameter)}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}
