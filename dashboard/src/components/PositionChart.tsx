"use client";

import { useId } from "react";
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
import { LiveDot } from "./LiveDot";
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
    // Was 30s -- state.py's _fetch_one_price now routes through the
    // live broker ticker (ccxt/Alpaca) instead of yfinance for most
    // assets, so a much shorter poll here actually shows new bars
    // instead of re-fetching the same stale candle.
    { refreshInterval: 5_000, revalidateOnFocus: false },
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
  // Bug fix: the gradient's SVG id used to be the hardcoded literal
  // "priceLine" -- harmless with one chart on a page, but PositionTable
  // renders one <PositionChart> per open position simultaneously, and
  // SVG element ids are global to the document. Every chart's <Line
  // stroke="url(#priceLine)"> resolved to whichever <linearGradient
  // id="priceLine"> the browser saw LAST, so every position's line
  // silently used one shared gradient instead of its own. useId()
  // gives each mounted chart a unique id.
  const gradientId = `priceLine-${useId()}`;
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

  // Carlos: "si pasa abajo de la linea se ve rojo... si sube verde" --
  // color the price line itself by whether it's above (gain, green) or
  // below (loss, red) the entry price, with a hard color-stop exactly
  // at the entry's height, instead of one flat gold line regardless of
  // P&L direction. `entryOffset` is the entry price's position within
  // the Y-domain as a 0..1 fraction from the TOP (SVG gradient objectBoundingBox
  // convention), so a vertical gradient can transition precisely there.
  const domainSpan = yDomain[1] - yDomain[0];
  const entryOffsetPct =
    entry !== null && domainSpan > 0
      ? Math.min(1, Math.max(0, ((yDomain[1] - entry) / domainSpan) * 100))
      : null;

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={pts}
          margin={{ top: 12, right: 16, left: 0, bottom: 8 }}
        >
          <defs>
            {entryOffsetPct !== null ? (
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#10b981" />
                <stop offset={`${entryOffsetPct}%`} stopColor="#10b981" />
                <stop offset={`${entryOffsetPct}%`} stopColor="#ef6b5a" />
                <stop offset="100%" stopColor="#ef6b5a" />
              </linearGradient>
            ) : (
              <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stopColor="#e6a93b" stopOpacity={0.3} />
                <stop offset="50%" stopColor="#e6a93b" stopOpacity={1} />
                <stop offset="100%" stopColor="#e6a93b" stopOpacity={0.3} />
              </linearGradient>
            )}
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
            stroke={`url(#${gradientId})`}
            strokeWidth={1.5}
            // isAnimationActive stays false: this poll every 5s
            // re-fetches the same fixed-size (~200 candle) window
            // rather than appending to a growing array, so recharts'
            // `matchByIndex` animation strategy would re-draw the
            // ENTIRE line from scratch on every poll (a visible
            // left-to-right sweep every 5s), and `matchAppend` doesn't
            // help either since the array length doesn't grow. A full
            // redraw-in on every tick would read as janky/flickery
            // rather than "live", so we keep the base line static and
            // get the "this is moving right now" read entirely from
            // the LiveDot marker below instead.
            isAnimationActive={false}
            dot={(props: { cx?: number; cy?: number; index?: number }) => {
              const isLast = props.index === pts.length - 1;
              if (!isLast) return <g key={`dot-${props.index}`} />;
              const lastClose = pts[pts.length - 1]?.close ?? 0;
              const color =
                entry !== null
                  ? lastClose >= entry
                    ? "#10b981"
                    : "#ef6b5a"
                  : "#e6a93b";
              return (
                <LiveDot
                  key="position-live-dot"
                  cx={props.cx}
                  cy={props.cy}
                  color={color}
                />
              );
            }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
