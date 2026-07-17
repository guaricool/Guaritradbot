"use client";

import { useId, useMemo } from "react";

/**
 * MiniTrend -- an understated corner sparkline for hero KpiCards (see
 * KpiCard's optional `trend` prop). Deliberately NOT built on recharts:
 * this needs to render small, transparent, and non-interactive (no axes,
 * no tooltip, no resize observer) as pure background texture behind a
 * big number, so a hand-rolled polyline keeps it cheap to re-render on
 * every ~1s live sample.
 *
 * Colored by trend direction (last vs first sample) using the same
 * gain/loss palette as the rest of the dashboard -- never a neon/glow
 * treatment, just a thin translucent line + very light fill.
 */
export function MiniTrend({
  values,
  width = 96,
  height = 32,
  color,
}: {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
}) {
  const gradientId = `miniTrendFill-${useId()}`;

  const { path, areaPath, lineColor } = useMemo(() => {
    if (values.length < 2) {
      return { path: "", areaPath: "", lineColor: color ?? "#7d869e" };
    }
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const stepX = width / (values.length - 1);
    const points = values.map((v, i) => {
      const x = i * stepX;
      const y = height - ((v - min) / span) * height;
      return [x, y] as const;
    });
    const linePath = points
      .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`)
      .join(" ");
    const area = `${linePath} L${width},${height} L0,${height} Z`;
    const up = values[values.length - 1] >= values[0];
    return {
      path: linePath,
      areaPath: area,
      lineColor: color ?? (up ? "#10b981" : "#ef6b5a"),
    };
  }, [values, width, height, color]);

  if (!path) return null;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="pointer-events-none overflow-visible opacity-80"
      aria-hidden
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={lineColor} stopOpacity={0.25} />
          <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={areaPath} fill={`url(#${gradientId})`} stroke="none" />
      <path d={path} fill="none" stroke={lineColor} strokeWidth={1.25} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
