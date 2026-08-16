import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SpectrogramResponse } from "../types";
import { Spectrogram } from "./Spectrogram";

function response(counts: number[][]): SpectrogramResponse {
  return {
    device: "radiacode-110",
    time_edges: ["2026-08-16T00:00:00Z", "2026-08-17T00:00:00Z"],
    energy_edges_kev: [0, 1],
    counts,
    source_resolution: "frame",
    rebinned: true,
  };
}

describe("spectrogram", () => {
  it("renders the default maximum data shape without spreading values as arguments", () => {
    const counts = Array.from({ length: 720 }, () => Array.from({ length: 256 }, () => 1));
    render(<Spectrogram data={response(counts)} />);
    expect(screen.getByRole("img", { name: /Time versus energy heatmap/ })).toBeInTheDocument();
  });

  it("explains that acquisition is waiting for a completed frame", () => {
    const { rerender } = render(<Spectrogram data={response([[1]])} />);
    rerender(<Spectrogram data={response([])} />);
    expect(screen.getByText(/No completed 5-minute spectrum frame in this range yet/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
