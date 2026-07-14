"use client";

/**
 * Sparkline -- Sprint 60 (MSN-style card design)
 *
 * Minimal line chart for the dashboard's market tile. No axes,
 * no grid, no legend, no tooltip, no header -- just the close
 * line, sized to the container. Same wire format as
 * `PriceChart` (an array of `Candle`) so the parent can share
 * the swr cache entry between the card and the modal.
 *
 * Visual: a single polyline that goes green if the close ended
 * up vs the first bar in the window, red otherwise. Color is
 * also exposed via a CSS-friendly class so a parent can use the
 * same color for the % change label without recomputing.
 */

import { useMemo } from "react";
import {
  LineChart,
  Line,
  YAxis,
  ResponsiveContainer,
} from "recharts";
import type { Candle } from "@/lib/types";

const COLORS = {
  gain: "#3fb950",
  loss: "#f85149",
};

/** Public so the parent card can color the % change label to
 *  match the sparkline without re-computing first/last. */
export function computePctChange(candles: Candle[]): { pct: number; up: boolean; first: number | null; last: number | null } {
  if (candles.length === 0) return { pct: 0, up: true, first: null, last: null };
  const first = candles[0].close;
  const last = candles[candles.length - 1].close;
  if (!first || !last) return { pct: 0, up: true, first: null, last: null };
  const pct = ((last - first) / first) * 100;
  return { pct, up: last >= first, first, last };
}

interface SparklineProps {
  /** Array of candles (any order -- we sort by ts inside). */
  candles: Candle[];
  /** Height in px. Default 60 fits comfortably in a 3-col tile
   *  while still showing 30+ data points of trend. */
  height?: number;
  /** Optional explicit color override. When omitted, we color
   *  green/red based on first-vs-last close. */
  color?: string;
}

export function Sparkline({ candles, height = 60, color }: SparklineProps) {
  const { up } = computePctChange(candles);
  const lineColor = color ?? (up ? COLORS.gain : COLORS.loss);

  // Sort + project to the minimum shape recharts needs. We avoid
  // the full OHLCV payload to keep the line lightweight even for
  // long windows (e.g. ALL = 520 weekly bars).
  const data = useMemo(() => {
    return [...candles]
      .sort((a, b) => a.ts - b.ts)
      .map((c) => ({ ts: c.ts, close: c.close }));
  }, [candles]);

  return (
    <div style={{ height, lineHeight: 0 }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 2, right: 0, bottom: 2, left: 0 }}>
          {/* Hidden Y axis keeps the line's "auto" domain so we
              don't waste vertical space on a fixed range. */}
          <YAxis hide domain={["auto", "auto"]} />
          <Line
            type="monotone"
            dataKey="close"
            stroke={lineColor}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Returns the canonical "$1,234" / "$1.23" / "$0.0001" format
 *  the dashboard uses for both the tile and the modal header.
 *  Duplicated from PriceChart to keep this component
 *  self-contained -- the two formats only differ in that
 *  forex is conventionally shown to 4 decimals (TODO: thread
 *  the category through if the visual difference matters). */
export function formatPriceShort(p: number): string {
  if (p >= 1000) return `$${p.toFixed(0)}`;
  if (p >= 1) return `$${p.toFixed(2)}`;
  return `$${p.toFixed(4)}`;
}
