"use client";

import { useMemo } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { BarShapeProps } from "recharts/types/cartesian/Bar";
import type { Candle } from "@/lib/types";
import { LiveDot } from "./LiveDot";

const GAIN = "#10b981";
const LOSS = "#ef6b5a";
const GOLD = "#e6a93b";

interface CandleDatum {
  t: number;
  label: string;
  open: number;
  high: number;
  low: number;
  close: number;
  // Range tuple recharts uses to size the Bar's y/height — see the
  // rendering note in the component doc comment below.
  range: [number, number];
}

export interface CandlestickChartProps {
  candles: Candle[];
  height: number;
  entry?: number | null;
  stopLoss?: number | null;
  takeProfit?: number | null;
  /** Show a pulsing LiveDot marker on the last candle's close. */
  showLiveDot?: boolean;
  /**
   * Color for the LiveDot. If omitted while `showLiveDot` is true, falls
   * back to entry-relative coloring (green if last close >= entry, coral
   * if below) when `entry` is provided, else the candle's own up/down
   * color, else gold.
   */
  liveDotColor?: string;
  /** Cap on rendered candles after downsampling. Default 130 — candles
   * need more horizontal room per point than a line, so this is lower
   * than PositionChart's 250-point line cap. */
  maxPoints?: number;
  /** Formats an X-axis tick / tooltip label from a unix-seconds ts. */
  xTickFormatter?: (ts: number) => string;
  /** Formats a price for the Y axis and tooltip rows. */
  valueFormatter?: (v: number) => string;
  /** Label used in the tooltip header (e.g. asset ticker). */
  asset?: string;
}

function defaultTimeFormatter(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function defaultValueFormatter(v: number) {
  if (v >= 1000) return v.toFixed(0);
  if (v >= 1) return v.toFixed(2);
  return v.toFixed(4);
}

/**
 * CandlestickChart — shared OHLC candlestick renderer used by both
 * PositionChart (per-position, 15m default interval) and PriceChart
 * (the /charts page's range-aware asset view).
 *
 * Rendering technique (recharts has no built-in candlestick type):
 * a `<Bar dataKey="range" shape={CandleShape}>` where `range` is the
 * tuple `[low, high]`. Recharts natively supports array-valued
 * dataKeys for range/floating bars, so the shape function receives
 * `x`/`y`/`width`/`height` that already correspond exactly to the
 * low-high span (y = pixel(high), y+height = pixel(low)) via the
 * chart's real y-axis scale/margins. From that we derive a local
 * `pxPerUnit = height / (high - low)` and compute the open/close
 * pixel offsets ourselves — no need to reach into recharts' internal
 * scale objects. The wick is a 1px vertical line spanning the full
 * y/height; the body is a rect from open to close, colored per-candle
 * (green #10b981 if close >= open, coral #ef6b5a otherwise).
 *
 * The X axis is category-typed (one tick per candle's formatted label)
 * rather than the numeric-time axis PositionChart's old LineChart used
 * — recharts' Bar layout needs a discrete band per data point to size
 * bars correctly, which a continuous numeric axis doesn't give you.
 *
 * The LiveDot is placed via a second, invisible series (`<Line
 * dataKey="close" stroke="none" dot={...}>`) sharing the same data
 * and y-axis, rather than computed from our own candle math — this
 * reuses recharts' own scale for the dot's cy so it lines up with
 * the candle body pixel-for-pixel, and reuses the exact dot-render
 * pattern PositionChart already had (see LiveDot.tsx).
 */
export function CandlestickChart({
  candles,
  height,
  entry = null,
  stopLoss = null,
  takeProfit = null,
  showLiveDot = false,
  liveDotColor,
  maxPoints = 130,
  xTickFormatter = defaultTimeFormatter,
  valueFormatter = defaultValueFormatter,
  asset,
}: CandlestickChartProps) {
  const pts = useMemo<CandleDatum[]>(() => {
    if (!candles.length) return [];
    const step = Math.max(1, Math.ceil(candles.length / maxPoints));
    return candles
      .filter((_, i) => i % step === 0)
      .map((c) => ({
        t: c.ts,
        label: xTickFormatter(c.ts),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
        range: [c.low, c.high] as [number, number],
      }));
  }, [candles, maxPoints, xTickFormatter]);

  if (!pts.length) {
    return (
      <div
        className="flex items-center justify-center text-muted"
        style={{ height }}
      >
        No candle data available.
      </div>
    );
  }

  const lows = pts.map((p) => p.low);
  const highs = pts.map((p) => p.high);
  const dataMin = Math.min(...lows);
  const dataMax = Math.max(...highs);
  const refMin = Math.min(dataMin, stopLoss ?? Infinity, entry ?? Infinity);
  const refMax = Math.max(dataMax, takeProfit ?? -Infinity, entry ?? -Infinity);
  const pad = (refMax - refMin) * 0.08 || refMax * 0.01 || 1;
  const yDomain: [number, number] = [refMin - pad, refMax + pad];

  const lastCandle = pts[pts.length - 1];
  const resolvedLiveDotColor =
    liveDotColor ??
    (entry !== null
      ? lastCandle.close >= entry
        ? GAIN
        : LOSS
      : lastCandle.close >= lastCandle.open
        ? GAIN
        : LOSS);

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={pts}
          margin={{ top: 12, right: 16, left: 0, bottom: 8 }}
        >
          <CartesianGrid stroke="#1c2438" strokeDasharray="3 3" />
          <XAxis
            dataKey="label"
            stroke="#525a72"
            fontSize={11}
            minTickGap={50}
            tickLine={false}
          />
          <YAxis
            domain={yDomain}
            tickFormatter={(v) => valueFormatter(v as number)}
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
            content={({ active, payload, label }) => {
              if (!active || !payload || !payload.length) return null;
              const d = payload[0].payload as CandleDatum;
              const up = d.close >= d.open;
              return (
                <div
                  style={{
                    background: "#0c111e",
                    border: "1px solid #1c2438",
                    borderRadius: 8,
                    fontSize: 12,
                    padding: "8px 10px",
                  }}
                >
                  <div style={{ color: "#7d869e", marginBottom: 4 }}>
                    {asset ? `${asset} · ` : ""}
                    {label as string}
                  </div>
                  <div style={{ color: up ? GAIN : LOSS, fontFamily: "var(--font-mono, monospace)" }}>
                    O {valueFormatter(d.open)} &nbsp; H {valueFormatter(d.high)}
                    <br />
                    L {valueFormatter(d.low)} &nbsp; C {valueFormatter(d.close)}
                  </div>
                </div>
              );
            }}
          />
          {entry !== null && (
            <ReferenceLine
              y={entry}
              stroke={GOLD}
              strokeDasharray="4 4"
              label={{
                value: `Entry ${valueFormatter(entry)}`,
                position: "left",
                fill: GOLD,
                fontSize: 10,
              }}
            />
          )}
          {stopLoss !== null && (
            <ReferenceLine
              y={stopLoss}
              stroke={LOSS}
              strokeDasharray="4 4"
              label={{
                value: `SL ${valueFormatter(stopLoss)}`,
                position: "left",
                fill: LOSS,
                fontSize: 10,
              }}
            />
          )}
          {takeProfit !== null && (
            <ReferenceLine
              y={takeProfit}
              stroke={GAIN}
              strokeDasharray="4 4"
              label={{
                value: `TP ${valueFormatter(takeProfit)}`,
                position: "left",
                fill: GAIN,
                fontSize: 10,
              }}
            />
          )}
          <Bar
            dataKey="range"
            isAnimationActive={false}
            shape={(shapeProps: unknown) => (
              <CandleShape {...(shapeProps as BarShapeProps)} />
            )}
          />
          {showLiveDot && (
            <Line
              dataKey="close"
              stroke="none"
              isAnimationActive={false}
              dot={(props: { cx?: number; cy?: number; index?: number }) => {
                const isLast = props.index === pts.length - 1;
                if (!isLast) return <g key={`live-dot-${props.index}`} />;
                return (
                  <LiveDot
                    key="candle-live-dot"
                    cx={props.cx}
                    cy={props.cy}
                    color={resolvedLiveDotColor}
                  />
                );
              }}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Renders one candle: a thin wick (low→high) and a filled body (open→close). */
function CandleShape(props: BarShapeProps) {
  const { x, y, width, height, payload } = props;
  const candle = payload as CandleDatum | undefined;
  if (!candle || width <= 0) return null;

  const { open, high, low, close } = candle;
  const span = high - low;
  const pxPerUnit = span > 0 ? height / span : 0;

  const openY = y + (high - open) * pxPerUnit;
  const closeY = y + (high - close) * pxPerUnit;
  const bodyTop = Math.min(openY, closeY);
  const bodyHeight = Math.max(1, Math.abs(closeY - openY));

  const up = close >= open;
  const color = up ? GAIN : LOSS;

  const bodyWidth = Math.max(1, width * 0.6);
  const bodyX = x + (width - bodyWidth) / 2;
  const wickX = x + width / 2;

  return (
    <g>
      <line
        x1={wickX}
        x2={wickX}
        y1={y}
        y2={y + height}
        stroke={color}
        strokeWidth={1}
      />
      <rect
        x={bodyX}
        y={bodyTop}
        width={bodyWidth}
        height={bodyHeight}
        fill={color}
      />
    </g>
  );
}
