"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import { fmtUsd, fmtTimeOnly } from "@/lib/format";
import { Skeleton } from "./Skeleton";
import { CandlestickChart } from "./CandlestickChart";
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
  return (
    <CandlestickChart
      candles={candles}
      height={height}
      entry={entry}
      stopLoss={stopLoss}
      takeProfit={takeProfit}
      asset={asset}
      // Carlos: "si pasa abajo de la linea se ve rojo... si sube verde" --
      // the LiveDot keeps the entry-relative coloring the old line chart
      // used (green above entry / coral below), NOT each candle's own
      // up/down color. Candles already carry per-candle color for the
      // last completed bar's direction; the dot's job is different --
      // it answers "am I in profit right now relative to my entry",
      // which is what the trader actually cares about on an open
      // position. CandlestickChart's own default (no `liveDotColor`
      // passed) would already compute this because `entry` is set, but
      // we pass it explicitly here for clarity since this is the one
      // spot where that semantic matters.
      showLiveDot
      liveDotColor={
        entry !== null
          ? candles[candles.length - 1]?.close >= entry
            ? "#10b981"
            : "#ef6b5a"
          : undefined
      }
      xTickFormatter={(ts) => fmtTimeOnly(ts)}
      valueFormatter={(v) => fmtUsd(v, { decimals: 2 })}
    />
  );
}
