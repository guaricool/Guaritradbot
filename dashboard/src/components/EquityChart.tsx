"use client";

import useSWR from "swr";
import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import { fmtUsd, fmtDateOnly } from "@/lib/format";
import { ChartSkeleton } from "./Skeleton";
import { LiveDot } from "./LiveDot";

export function EquityChart({ windowDays = 30 }: { windowDays?: number }) {
  const { data, error, isLoading } = useSWR(
    ["equity", windowDays],
    () => api.equity(windowDays),
    { refreshInterval: 30_000 },
  );

  const series = useMemo(() => data?.series ?? [], [data]);

  // Sprint 46K fix: the Y-axis previously hardcoded `decimals: 0`, which
  // rounds every tick label to "$0" on an account this small (positions
  // are $10-20, P&L moves in cents) — the chart LOOKED broken even when
  // the underlying data had real cent-level movement. Pick precision
  // based on how big the real numbers actually are, same idea as
  // EquityTracker's own precision=4 console logging in main.py.
  const axisDecimals = useMemo(() => {
    const maxAbs = series.reduce(
      (m, p) => Math.max(m, Math.abs(p.cumulative_usd ?? 0)),
      0,
    );
    if (maxAbs === 0) return 2;
    if (maxAbs < 1) return 4;
    if (maxAbs < 100) return 2;
    return 0;
  }, [series]);

  // Sprint 46L: Carlos wants the curve itself to read as gain/loss —
  // green "wave" above the zero line, red below it — instead of a
  // single always-green fill regardless of sign. Recharts doesn't
  // split an Area's color by value natively, so this uses the
  // standard trick: a vertical gradient whose stop OFFSET (not just
  // its color) lands exactly where cumulative_usd crosses zero within
  // the chart's own min/max range. Above that offset = green, below =
  // red — so the fill (and the line stroke) flips color exactly at
  // the zero crossing(s), no matter how many times the curve crosses
  // it.
  const zeroOffset = useMemo(() => {
    const values = series.map((p) => p.cumulative_usd ?? 0);
    const max = Math.max(0, ...values);
    const min = Math.min(0, ...values);
    if (max === min) return max >= 0 ? 0 : 1; // flat at exactly 0 → treat as green
    return max / (max - min);
  }, [series]);

  if (isLoading) {
    return <ChartSkeleton />;
  }
  if (error || !data) {
    return (
      <div className="flex h-64 items-center justify-center text-muted">
        No equity data yet.
      </div>
    );
  }
  if (!series.length) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-1 text-muted">
        <span className="text-2xl">∅</span>
        <span className="text-sm">No closed trades in this window.</span>
      </div>
    );
  }

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={series}
          margin={{ top: 8, right: 16, left: 0, bottom: 8 }}
        >
          <defs>
            {/* Richer fill: was a flat 2-stop wash on each side of zero
                (0.35 -> 0.05). Adding a mid stop gives the area some
                depth/"glass" quality near the line itself without ever
                introducing a blurred glow -- still plain flat color
                stops, just more of them, which is what recharts'
                gradient fill actually supports. */}
            <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity={0.4} />
              <stop offset={Math.max(0, zeroOffset - 0.15)} stopColor="#10b981" stopOpacity={0.16} />
              <stop offset={zeroOffset} stopColor="#10b981" stopOpacity={0.04} />
              <stop offset={zeroOffset} stopColor="#ef4444" stopOpacity={0.04} />
              <stop offset={Math.min(1, zeroOffset + 0.15)} stopColor="#ef4444" stopOpacity={0.16} />
              <stop offset="100%" stopColor="#ef4444" stopOpacity={0.4} />
            </linearGradient>
            <linearGradient id="equityStroke" x1="0" y1="0" x2="0" y2="1">
              <stop offset={zeroOffset} stopColor="#10b981" />
              <stop offset={zeroOffset} stopColor="#ef4444" />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1c2438" strokeDasharray="3 3" />
          <ReferenceLine y={0} stroke="#525a72" strokeDasharray="4 4" />
          <XAxis
            dataKey="date"
            tickFormatter={(d) => fmtDateOnly(d)}
            stroke="#525a72"
            fontSize={11}
            minTickGap={40}
          />
          <YAxis
            tickFormatter={(v) => fmtUsd(v, { decimals: axisDecimals })}
            stroke="#525a72"
            fontSize={11}
            width={70}
          />
          <Tooltip
            contentStyle={{
              background: "#0c111e",
              border: "1px solid #1c2438",
              borderRadius: 8,
              fontSize: 12,
            }}
            labelStyle={{ color: "#7d869e" }}
            formatter={(v) => [
              fmtUsd(typeof v === "number" ? v : null, { signed: true, decimals: 2 }),
              "Cumulative",
            ]}
          />
          <Area
            type="monotone"
            dataKey="cumulative_usd"
            stroke="url(#equityStroke)"
            strokeWidth={1.5}
            fill="url(#equityFill)"
            isAnimationActive={false}
            // Only the LAST point gets a marker -- a pulsing "live" dot
            // so the curve reads as "still updating", not a static
            // historical image, even though it refetches on a 30s
            // interval rather than every tick.
            dot={(props: { cx?: number; cy?: number; index?: number }) => {
              const isLast = props.index === series.length - 1;
              if (!isLast) return <g key={`dot-${props.index}`} />;
              const last = series[series.length - 1]?.cumulative_usd ?? 0;
              return (
                <LiveDot
                  key="equity-live-dot"
                  cx={props.cx}
                  cy={props.cy}
                  color={last >= 0 ? "#10b981" : "#ef4444"}
                />
              );
            }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
