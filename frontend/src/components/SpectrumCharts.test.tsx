import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ComparisonResponse } from "../types";
import { ComparisonChart, SpectrumChart } from "./SpectrumCharts";

describe("spectrum chart acquisition state", () => {
  it("explains why the selected detector has no spectrum yet", () => {
    render(<SpectrumChart spectra={[]} logarithmic />);
    expect(
      screen.getByText("No completed 5-minute spectrum frame in this range yet"),
    ).toBeInTheDocument();
  });

  it("explains why detector comparison has no spectra yet", () => {
    const comparison: ComparisonResponse = {
      energy_edges_kev: [],
      series: [],
      rebinned: true,
    };
    render(<ComparisonChart comparison={comparison} logarithmic />);
    expect(
      screen.getByText("No completed 5-minute spectrum frames from both detectors in this range yet"),
    ).toBeInTheDocument();
  });
});
