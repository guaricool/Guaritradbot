"use client";

import useSWR from "swr";
import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import { fmtUsd, fmtDateOnly } from "@/lib/format";
import { Spinner } from "./Spinner";

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

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center text-muted">
        <Spinner className="h-5 w-5" />
      </div>
    );
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
            <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#10b981" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1c2438" strokeDasharray="3 3" />
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
            stroke="#10b981"
            strokeWidth={1.5}
            fill="url(#equityFill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
