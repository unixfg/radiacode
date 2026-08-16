import { formatLocalDate } from "../format";
import type { DeviceEvent } from "../types";

export function EventTimeline({ events }: { events: DeviceEvent[] }) {
  if (events.length === 0) return <div className="chart-empty">No detector events in this range</div>;
  return (
    <ol className="timeline">
      {events.slice(0, 100).map((event, index) => (
        <li key={`${event.at}-${event.code}-${index}`}>
          <i aria-hidden="true" />
          <div>
            <strong>{event.name}</strong>
            <time dateTime={event.at}>{formatLocalDate(event.at)}</time>
            {event.parameter !== null && <p>{String(event.parameter)}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}
