import { useEffect, useRef } from "react";

import { formatLocalDate, formatNumber } from "../format";
import type { SpectrogramResponse } from "../types";

function thermalColor(value: number): string {
  const normalized = Math.max(0, Math.min(1, value));
  const stops = [
    [8, 18, 24],
    [20, 75, 76],
    [34, 150, 112],
    [205, 193, 89],
    [255, 238, 172],
  ];
  const scaled = normalized * (stops.length - 1);
  const lower = Math.floor(scaled);
  const upper = Math.min(stops.length - 1, lower + 1);
  const fraction = scaled - lower;
  return `rgb(${stops[lower].map((channel, index) => Math.round(channel + (stops[upper][index] - channel) * fraction)).join(",")})`;
}

export function Spectrogram({ data }: { data: SpectrogramResponse }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const hasData = data.counts.length > 0 && data.counts[0].length > 0;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const draw = () => {
      const context = canvas.getContext("2d");
      if (!context) return;
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(320, canvas.clientWidth);
      const height = Math.max(220, canvas.clientHeight);
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);

      if (!hasData) {
        context.fillStyle = "#8da29c";
        context.font = "14px Inter, system-ui, sans-serif";
        context.fillText("No spectrogram data in this range", 24, 36);
        return;
      }

      const left = 58;
      const right = 18;
      const top = 12;
      const bottom = 36;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      let max = 1;
      for (const row of data.counts) {
        for (const count of row) max = Math.max(max, count);
      }
      const timeBins = data.counts.length;
      const energyBins = data.counts[0].length;
      data.counts.forEach((row, timeIndex) => {
        row.forEach((count, energyIndex) => {
          const intensity = Math.log1p(count) / Math.log1p(max);
          context.fillStyle = thermalColor(intensity);
          const x = left + (timeIndex / timeBins) * plotWidth;
          const y = top + plotHeight - ((energyIndex + 1) / energyBins) * plotHeight;
          context.fillRect(x, y, plotWidth / timeBins + 1, plotHeight / energyBins + 1);
        });
      });
      context.fillStyle = "#9db4ad";
      context.font = "12px Inter, system-ui, sans-serif";
      context.fillText(formatLocalDate(data.time_edges[0]), left, height - 10);
      const endText = formatLocalDate(data.time_edges.at(-1));
      const textWidth = context.measureText(endText).width;
      context.fillText(endText, width - right - textWidth, height - 10);
      context.save();
      context.translate(14, top + plotHeight / 2);
      context.rotate(-Math.PI / 2);
      context.textAlign = "center";
      context.fillText(`Energy · 0–${formatNumber(data.energy_edges_kev.at(-1), 0)} keV`, 0, 0);
      context.restore();
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [data, hasData]);

  return (
    <div className="spectrogram-wrap">
      <canvas
        ref={canvasRef}
        className="spectrogram"
        role="img"
        aria-label={
          hasData
            ? `Time versus energy heatmap for ${data.device}`
            : `No spectrogram data for ${data.device}`
        }
      />
      <div className="heat-legend"><span>Lower counts</span><i /><span>Higher counts</span></div>
    </div>
  );
}
