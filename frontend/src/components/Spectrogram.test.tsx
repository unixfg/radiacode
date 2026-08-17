import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SpectrogramResponse } from "../types";
import { Spectrogram } from "./Spectrogram";

function response(counts: number[][]): SpectrogramResponse {
  const energyBins = counts[0]?.length ?? 0;
  return {
    device: "radiacode-110",
    time_edges: ["2026-08-16T00:00:00Z", "2026-08-17T00:00:00Z"],
    energy_edges_kev: Array.from({ length: energyBins + 1 }, (_, index) => index),
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

  it("draws calibrated energy markings on the Y axis", () => {
    const getContext = vi.mocked(HTMLCanvasElement.prototype.getContext);
    getContext.mockClear();
    const data = response([[1, 2, 3, 4]]);
    data.energy_edges_kev = [25, 125, 225, 325, 425];

    render(<Spectrogram data={data} />);

    const context = getContext.mock.results.at(-1)?.value as CanvasRenderingContext2D;
    expect(context.fillText).toHaveBeenCalledWith("25", expect.any(Number), expect.any(Number));
    expect(context.fillText).toHaveBeenCalledWith("225", expect.any(Number), expect.any(Number));
    expect(context.fillText).toHaveBeenCalledWith("425", expect.any(Number), expect.any(Number));
    expect(context.fillText).toHaveBeenCalledWith("Energy (keV)", 0, 0);
    expect(context.stroke).toHaveBeenCalledTimes(5);
  });
});
