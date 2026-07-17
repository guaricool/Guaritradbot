"use client";

/**
 * TickerStrip — full-width scrolling ticker tape across the top of the
 * dashboard, inspired by trading-terminal reference UIs (a row like
 * "BTC $67,240 +1.2%   SPY $521.30 -0.4%   TSLA $221.10 +2.9%").
 *
 * Data source: this dashboard has no dedicated "quote" endpoint, only
 * `api.candles(asset, interval, limit)` (see lib/api.ts). We fetch a
 * short recent window per ticker and derive last price + % change with
 * `computePctChange` from Sparkline.tsx (same math as the /charts page
 * uses, kept in one place so the two views can never disagree).
 *
 * Refresh: a single `useSWR` hook fetches all 15 tickers together
 * (via Promise.allSettled) on a 20s `refreshInterval`. These are
 * read-only visualization prices (not the bot's live position P&L,
 * which is pushed over SSE/WS already), so 20s is plenty fresh
 * without hammering the backend with 15 simultaneous yfinance-backed
 * requests every few seconds.
 *
 * Fail-open: a ticker whose fetch errors or returns <2 candles is
 * simply omitted from the strip (same philosophy as the rest of the
 * dashboard — one bad symbol shouldn't blank the whole component).
 *
 * Marquee: renders the ticker list twice back-to-back inside a track
 * that translateX's from 0 to -50% in a linear @keyframes loop (see
 * globals.css `.ticker-track` / `@keyframes ticker-scroll`) — a classic
 * seamless-loop trick, since the second copy lines up exactly where the
 * first one scrolled off. transform-only animation per DESIGN.md's
 * performance rule. Paused on hover (so a user can actually read a
 * value) and disabled entirely under `prefers-reduced-motion` (CSS
 * media query swaps to a static wrapped row).
 */

import { useMemo } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import { ASSET_UNIVERSE } from "@/lib/assetUniverse";
import { computePctChange, formatPriceShort } from "./Sparkline";

const REFRESH_MS = 20_000;

interface TickerDatum {
  ticker: string;
  label: string;
  price: number;
  pct: number;
  up: boolean;
}

// A single SWR entry drives all 15 tickers instead of one hook per
// ticker — this keeps the hook count static (satisfies rules-of-hooks
// without an eslint-disable) and lets us fail open per-symbol inside
// one fetcher via Promise.allSettled: one bad ticker rejects, the rest
// still resolve, and we just filter the failure out below.
async function fetchAllTickers(): Promise<TickerDatum[]> {
  const settled = await Promise.allSettled(
    ASSET_UNIVERSE.map((a) => api.candles(a.ticker, "1h", 2)),
  );
  const out: TickerDatum[] = [];
  settled.forEach((result, i) => {
    if (result.status !== "fulfilled") return;
    const candles = result.value.candles;
    if (!candles || candles.length < 2) return;
    const { pct, up, last } = computePctChange(candles);
    if (last === null) return;
    const asset = ASSET_UNIVERSE[i];
    out.push({ ticker: asset.ticker, label: asset.label, price: last, pct, up });
  });
  return out;
}

function useTickerStripData(): TickerDatum[] {
  const { data } = useSWR("ticker-strip-all", fetchAllTickers, {
    refreshInterval: REFRESH_MS,
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  return useMemo(() => data ?? [], [data]);
}

function TickerItem({ item }: { item: TickerDatum }) {
  return (
    <div className="flex shrink-0 items-center gap-2 px-4 text-xs">
      <span className="font-medium uppercase tracking-wide text-muted">
        {item.label}
      </span>
      <span className="font-mono num text-cream-50">
        {formatPriceShort(item.price)}
      </span>
      <span className={`font-mono num ${item.up ? "text-gain" : "text-loss"}`}>
        {item.up ? "+" : ""}
        {item.pct.toFixed(2)}%
      </span>
      <span className="text-ink-700">|</span>
    </div>
  );
}

function TickerStripInner({ data }: { data: TickerDatum[] }) {
  if (data.length === 0) return null;
  return (
    <div className="ticker-track flex w-max items-center">
      <div className="flex items-center">
        {data.map((item) => (
          <TickerItem key={item.ticker} item={item} />
        ))}
      </div>
      {/* Duplicate copy for the seamless -50% wraparound loop.
          aria-hidden: it's a visual repeat, not new content. */}
      <div className="flex items-center" aria-hidden="true">
        {data.map((item) => (
          <TickerItem key={`${item.ticker}-dup`} item={item} />
        ))}
      </div>
    </div>
  );
}

export function TickerStrip() {
  const data = useTickerStripData();

  if (data.length === 0) {
    // Nothing loaded yet (or everything failed) — render nothing rather
    // than a skeleton; this is a decorative strip, not primary content.
    return null;
  }

  return (
    <div className="h-9 w-full shrink-0 overflow-hidden border-b border-ink-700 bg-ink-900/80 backdrop-blur">
      <TickerStripInner data={data} />
    </div>
  );
}
