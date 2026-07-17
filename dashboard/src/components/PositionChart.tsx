"use client";

import useSWR from "swr";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";
import { fmtUsd, fmtTimeOnly } from "@/lib/format";
import { Skeleton } from "./Skeleton";
import type { Candle } from "@/lib/types";

interface Props {
  positionId: string;
  interval?: "1m" | "5m" | "15m" | "1h" | "1d";
  height?: number;
}

export function PositionChart({
  positionId,
  interval = "15m",
  height = 360,
}: Props) {
  const { data, error, isLoading } = useSWR(
    ["candles", positionId, interval],
    () => api.positionCandles(positionId, interval, 200),
    { refreshInterval: 30_000, revalidateOnFocus: false },
  );

  if (isLoading) {
    return <Skeleton className="w-full" style={{ height }} />;
  }
  if (error || !data) {
    return (
      <div
        className="flex items-center justify-center text-loss/80"
        style={{ height }}
      >
        Failed to load candles.
      </div>
    );
  }

  return (
    <Chart
      candles={data.candles}
      asset={data.asset}
      entry={data.entry}
      stopLoss={data.stop_loss}
      takeProfit={data.take_profit}
      height={height}
    />
  );
}

function Chart({
  candles,
  asset,
  entry,
  stopLoss,
  takeProfit,
  height,
}: {
  candles: Candle[];
  asset: string;
  entry: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  height: number;
}) {
  if (!candles.length) {
    return (
      <div
        className="flex items-center justify-center text-muted"
        style={{ height }}
      >
        No candle data available.
      </div>
    );
  }
  // Downsample for chart performance: cap to ~250 points
  const step = Math.max(1, Math.ceil(candles.length / 250));
  const pts = candles
    .filter((_, i) => i % step === 0)
    .map((c) => ({ t: c.ts * 1000, close: c.close }));

  const min = Math.min(...pts.map((p) => p.close));
  const max = Math.max(...pts.map((p) => p.close));
  const refMin = Math.min(min, stopLoss ?? Infinity, entry ?? Infinity);
  const refMax = Math.max(max, takeProfit ?? -Infinity, entry ?? -Infinity);
  const pad = (refMax - refMin) * 0.08 || refMax * 0.01;
  const yDomain: [number, number] = [refMin - pad, refMax + pad];

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={pts}
          margin={{ top: 12, right: 16, left: 0, bottom: 8 }}
        >
          <defs>
            <linearGradient id="priceLine" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="#e6a93b" stopOpacity={0.3} />
              <stop offset="50%" stopColor="#e6a93b" stopOpacity={1} />
              <stop offset="100%" stopColor="#e6a93b" stopOpacity={0.3} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1c2438" strokeDasharray="3 3" />
          <XAxis
            dataKey="t"
            type="number"
            domain={["dataMin", "dataMax"]}
            tickFormatter={(t) => fmtTimeOnly(t / 1000)}
            stroke="#525a72"
            fontSize={11}
            minTickGap={50}
          />
          <YAxis
            domain={yDomain}
            tickFormatter={(v) =>
              v >= 1000 ? v.toFixed(0) : v.toFixed(2)
            }
            stroke="#525a72"
            fontSize={11}
            width={64}
          />
          <Tooltip
            contentStyle={{
              background: "#0c111e",
              border: "1px solid #1c2438",
              borderRadius: 8,
              fontSize: 12,
            }}
            labelStyle={{ color: "#7d869e" }}
            formatter={(v) => [fmtUsd(typeof v === "number" ? v : null, { decimals: 2 }), `${asset}`]}
            labelFormatter={(t) => new Date(t as number).toLocaleString()}
          />
          {entry !== null && (
            <ReferenceLine
              y={entry}
              stroke="#e6a93b"
              strokeDasharray="4 4"
              label={{
                value: `Entry ${fmtUsd(entry, { decimals: 2 })}`,
                position: "left",
                fill: "#e6a93b",
                fontSize: 10,
              }}
            />
          )}
          {stopLoss !== null && (
            <ReferenceLine
              y={stopLoss}
              stroke="#ef6b5a"
              strokeDasharray="4 4"
              label={{
                value: `SL ${fmtUsd(stopLoss, { decimals: 2 })}`,
                position: "left",
                fill: "#ef6b5a",
                fontSize: 10,
              }}
            />
          )}
          {takeProfit !== null && (
            <ReferenceLine
              y={takeProfit}
              stroke="#10b981"
              strokeDasharray="4 4"
              label={{
                value: `TP ${fmtUsd(takeProfit, { decimals: 2 })}`,
                position: "left",
                fill: "#10b981",
                fontSize: 10,
              }}
            />
          )}
          <Line
            type="monotone"
            dataKey="close"
            stroke="url(#priceLine)"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
