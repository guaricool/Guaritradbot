"use client";

/**
 * PriceChart — a single-asset candlestick line chart for the
 * /charts dashboard page. Uses recharts (already in package.json)
 * and the /api/candles endpoint (Sprint 58).
 *
 * The component is intentionally minimal: it pulls a small
 * slice of recent candles and renders the close-price line.
 * We don't draw full candlesticks (the high/low/open/close
 * rects) because recharts' built-in LineChart is sufficient
 * at a glance and the wire format already has all the OHLC
 * data if we want to upgrade later.
 *
 * Auto-refresh: the parent (the /charts page) uses swr with a
 * 15s refresh interval; this component is a pure renderer of
 * whatever the parent gives it.
 */
import { useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";

export interface Candle {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PriceChartProps {
  asset: string;
  candles: Candle[];
  /** Optional reference line (e.g. entry price for an open position). */
  referencePrice?: number;
  /** Container height in px. Default 180 (fits in a 3-col grid). */
  height?: number;
}

const COLORS = {
  gain: "#3fb950",
  loss: "#f85149",
  grid: "#30363d",
  axis: "#8b949e",
  text: "#c9d1d9",
};

function formatTime(ts: number) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(ts: number) {
  const d = new Date(ts * 1000);
  return d.toLocaleString([], {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatPrice(p: number) {
  if (p >= 1000) return `$${p.toFixed(0)}`;
  if (p >= 1) return `$${p.toFixed(2)}`;
  return `$${p.toFixed(4)}`;
}

export function PriceChart({ asset, candles, referencePrice, height = 180 }: PriceChartProps) {
  // Map the candles to recharts' data shape. We sort by ts asc
  // so the X axis goes left-to-right.
  const data = useMemo(
    () =>
      candles
        .slice()
        .sort((a, b) => a.ts - b.ts)
        .map((c) => ({
          ts: c.ts,
          label: formatTime(c.ts),
          close: c.close,
          high: c.high,
          low: c.low,
        })),
    [candles],
  );
  // Line color: green if last close >= first close, red otherwise.
  // The /charts page sorts newest-first in the wire format (we
  // re-sort asc here), so data[0] is oldest, data[data.length-1]
  // is newest.
  const first = data[0]?.close;
  const last = data[data.length - 1]?.close;
  const lineColor = first != null && last != null && last >= first ? COLORS.gain : COLORS.loss;
  // Pct change for the asset label.
  const pct = first && last ? ((last - first) / first) * 100 : 0;
  const pctStr = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";

  return (
    <div className="flex h-full flex-col rounded-lg border border-ink-700 bg-ink-900/50 p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <div>
          <div className="text-sm font-semibold text-cream-50">{asset}</div>
          <div className="text-[10px] uppercase tracking-wider text-muted">
            {data.length} candles · 1h
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
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={data}
            margin={{ top: 4, right: 4, bottom: 0, left: 4 }}
          >
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
              // recharts Tooltip formatter types value as
              // `ValueType | undefined` where ValueType can be
              // string | number | null. Normalize defensively
              // so we never render "$NaN" on a missing bar.
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
      </div>
    </div>
  );
}
