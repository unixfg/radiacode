import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DeviceEvent } from "../types";
import { EventTimeline, summarizeTimelineEvents } from "./EventTimeline";

function event(at: string, code: string, name: string): DeviceEvent {
  return { at, code, name, parameter: null };
}

describe("detector timeline", () => {
  it("groups repeated gap events by label and code without hiding alarms", () => {
    const events = [
      event("2026-08-16T16:42:42Z", "data_buf_sequence_gap", "Telemetry sequence gap"),
      event("2026-08-16T16:42:40Z", "count_rate_alarm_1", "count_rate_alarm_1"),
      event("2026-08-16T16:42:30Z", "data_buf_sequence_gap", "Telemetry sequence gap"),
      event("2026-08-16T16:41:50Z", "data_buf_sequence_gap", "Telemetry sequence gap"),
      event("2026-08-16T16:41:40Z", "duration_regression", "Spectrum acquisition gap"),
    ];

    const entries = summarizeTimelineEvents(events);
    expect(entries.map(({ event: item, count }) => [item.code, count])).toEqual([
      ["data_buf_sequence_gap", 3],
      ["count_rate_alarm_1", 1],
      ["duration_regression", 1],
    ]);

    render(<EventTimeline events={events} />);
    expect(screen.getByText("3 similar gap events grouped")).toBeInTheDocument();
    expect(screen.getByText("count_rate_alarm_1")).toBeInTheDocument();
    expect(screen.getAllByText("Telemetry sequence gap")).toHaveLength(1);
  });

  it("starts another group when the same gap label is more than five minutes older", () => {
    const entries = summarizeTimelineEvents([
      event("2026-08-16T16:42:42Z", "data_buf_sequence_gap", "Telemetry sequence gap"),
      event("2026-08-16T16:32:42Z", "data_buf_sequence_gap", "Telemetry sequence gap"),
    ]);

    expect(entries).toHaveLength(2);
    expect(entries.map(({ count }) => count)).toEqual([1, 1]);
  });
});
