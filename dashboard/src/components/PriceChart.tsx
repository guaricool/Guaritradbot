"use client";

/**
 * PriceChart — Sprint 58 + Sprint 59
 *
 * Single-asset price chart for the /charts dashboard. Renders an
 * OHLC line over a `close`-keyed LineChart, with the asset label,
 * last close, percent change, and a 1D/5D/1M/3M/1Y/ALL time-range
 * selector at the top.
 *
 * Sprint 59 changes:
 *  - Component is now range-aware. The parent passes `range` and
 *    `onRangeChange`; the chart itself does the fetch (via swr)
 *    so a range click triggers a single re-render. This keeps the
 *    state model simple: the asset card and the modal can each
 *    own their own PriceChart instance, and each one is self-
 *    contained.
 *  - Default range is "1M" (1 month, daily bars) -- matches the
 *    pre-Sprint 59 default feel and is the most common zoom level.
 *  - The `height` prop is still accepted so the fullscreen modal
 *    can use a taller chart (~520px) than the card (180px).
 *
 * Recharts has `<Brush>` for in-chart range selection but that's
 * for "zoom into data we already have" -- different use case. The
 * here-range-selector re-fetches a new granularity from the API.
 */

import { useMemo } from "react";
import useSWR from "swr";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import { api } from "@/lib/api";
import { TimeRangeSelector } from "@/components/TimeRangeSelector";
import type { Candle, TimeRange, YfInterval } from "@/lib/types";

export type { Candle };

export interface PriceChartProps {
  asset: string;
  label?: string;
  /** Optional ticker yfinance uses internally (defaults to `asset`). */
  ticker?: string;
  /** When provided, the chart is range-aware. Default is "1M". */
  range?: TimeRange;
  /** Called when the user picks a new range chip. */
  onRangeChange?: (range: TimeRange) => void;
  /** Optional reference line (e.g. entry price for an open position). */
  referencePrice?: number;
  /** Container height in px. Default 180 (fits in a 3-col grid). */
  height?: number;
  /** Optional subtitle in the top-right (defaults to nothing). */
  subtitle?: string;
}

// Mirrors the (interval, limit) pairs from lib/api.ts::rangeToParams
// -- duplicated here so the swr cache key includes the interval
// and limit separately (candlesRange does this server-side).
const RANGE_PARAMS: Record<TimeRange, { interval: YfInterval; limit: number }> = {
  "1D":  { interval: "5m",  limit: 100 },
  "5D":  { interval: "15m", limit: 200 },
  "1M":  { interval: "1d",  limit: 35  },
  "3M":  { interval: "1d",  limit: 95  },
  "1Y":  { interval: "1d",  limit: 370 },
  "ALL": { interval: "1wk", limit: 520 },
};

const COLORS = {
  gain: "#3fb950",
  loss: "#f85149",
  grid: "#30363d",
  axis: "#8b949e",
  text: "#c9d1d9",
};

function formatTime(ts: number, interval: YfInterval) {
  // Use time-of-day for intra-day ranges, date for daily+ so the X
  // axis stays readable without overcrowding.
  const d = new Date(ts * 1000);
  if (interval === "5m" || interval === "15m" || interval === "1h") {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (interval === "1d" || interval === "1wk") {
    return d.toLocaleDateString([], { month: "short", day: "2-digit" });
  }
  return d.toLocaleDateString([], { month: "short", year: "2-digit" });
}

function formatDateTime(ts: number) {
  const d = new Date(ts * 1000);
  return d.toLocaleString([], {
    month: "short",
    day: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatPrice(p: number) {
  if (p >= 1000) return `$${p.toFixed(0)}`;
  if (p >= 1) return `$${p.toFixed(2)}`;
  return `$${p.toFixed(4)}`;
}

export function PriceChart({
  asset,
  label,
  ticker,
  range = "1M",
  onRangeChange,
  referencePrice,
  height = 180,
  subtitle,
}: PriceChartProps) {
  const yfTicker = ticker ?? asset;
  const { interval, limit } = RANGE_PARAMS[range];

  // Fetch via the api helper -- this routes through the bot host
  // (not the dashboard host) so we get the new intervals (1wk, 1mo)
  // that Sprint 59 added. The swr cache key is the (ticker, range)
  // tuple so each range gets its own cache entry.
  const { data, error, isLoading } = useSWR(
    ["candles", yfTicker, range],
    () => api.candles(yfTicker, interval, limit),
    {
      refreshInterval: range === "1D" ? 60_000 : 0, // 1D auto-refresh; longer ranges are static
      revalidateOnFocus: false,
    },
  );

  // Map the candles to recharts' data shape. We sort by ts asc so
  // the X axis goes left-to-right. Yfinance returns oldest-first
  // for crypto/forex but newest-first for some stocks depending on
  // period; we don't trust the order and sort explicitly.
  const sorted = useMemo(() => {
    const candles = data?.candles ?? [];
    return [...candles]
      .sort((a, b) => a.ts - b.ts)
      .map((c: Candle) => ({
        ts: c.ts,
        label: formatTime(c.ts, interval),
        close: c.close,
        high: c.high,
        low: c.low,
      }));
  }, [data?.candles, interval]);

  // Pct change for the asset label: first close -> last close in
  // the loaded window. For 1D that's "today's change", for 1M
  // that's "this month's change", etc.
  const first = sorted[0]?.close;
  const last = sorted[sorted.length - 1]?.close;
  const lineColor = first != null && last != null && last >= first ? COLORS.gain : COLORS.loss;
  const pct = first && last ? ((last - first) / first) * 100 : 0;
  const pctStr = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
  const rangeLoading = isLoading && !data;

  return (
    <div className="flex h-full flex-col rounded-lg border border-ink-700 bg-ink-900/50 p-3">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-cream-50">
            {label ?? asset}
          </div>
          <div className="truncate text-[10px] uppercase tracking-wider text-muted">
            {sorted.length} candles · {interval}
            {subtitle && <> · {subtitle}</>}
          </div>
        </div>
        <div className="text-right">
          <div className="text-sm font-mono text-cream-50">
            {last != null ? formatPrice(last) : "—"}
          </div>
          <div
            className="text-[10px] font-mono"
            style={{ color: pct >= 0 ? COLORS.gain : COLORS.loss }}
          >
            {pctStr}
          </div>
        </div>
      </div>
      <div style={{ height }}>
        {rangeLoading ? (
          <div className="h-full animate-pulse rounded bg-ink-800/60" />
        ) : error ? (
          <div className="flex h-full items-center justify-center text-[10px] text-loss">
            {(error as Error).message}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={sorted} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
              <XAxis
                dataKey="label"
                stroke={COLORS.axis}
                fontSize={9}
                tickLine={false}
                axisLine={{ stroke: COLORS.grid }}
                interval="preserveStartEnd"
                minTickGap={32}
              />
              <YAxis
                stroke={COLORS.axis}
                fontSize={9}
                tickLine={false}
                axisLine={false}
                domain={["auto", "auto"]}
                tickFormatter={(v) => formatPrice(v as number)}
                width={48}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#0d1117",
                  border: `1px solid ${COLORS.grid}`,
                  borderRadius: 6,
                  fontSize: 11,
                }}
                labelFormatter={(label, payload) => {
                  if (payload && payload[0]) {
                    return formatDateTime(payload[0].payload.ts);
                  }
                  return String(label);
                }}
                formatter={(value) => {
                  if (value == null) return "—";
                  const n = typeof value === "number" ? value : Number(value);
                  return Number.isFinite(n) ? formatPrice(n) : "—";
                }}
              />
              {referencePrice != null && (
                <ReferenceLine
                  y={referencePrice}
                  stroke={COLORS.text}
                  strokeDasharray="3 3"
                  strokeOpacity={0.6}
                  label={{
                    value: `entry ${formatPrice(referencePrice)}`,
                    position: "insideTopRight",
                    fontSize: 9,
                    fill: COLORS.axis,
                  }}
                />
              )}
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
        )}
      </div>
      {/* Time-range selector sits at the bottom of the card so the
          chart line itself stays the visual focus. The modal puts
          it in the same spot but with bigger chips. */}
      {onRangeChange && (
        <div className="mt-2 flex justify-end">
          <TimeRangeSelector value={range} onChange={onRangeChange} />
        </div>
      )}
    </div>
  );
}
