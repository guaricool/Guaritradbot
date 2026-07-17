"use client";

/**
 * /charts -- Sprint 58 + Sprint 59 + Sprint 60
 *
 * Sprint 58: 7 tradeable assets in a single grid.
 * Sprint 59: 15 assets across 3 categories (crypto / forex / equity),
 *   per-card time-range selector, click-to-expand ChartModal.
 * Sprint 60: MSN-style market tile design -- compact cards with
 *   name + price + % change + small sparkline (no axes, no
 *   legend). Click any tile to open the full chart modal where
 *   the same time-range selector zooms the full chart.
 *
 * The card's sparkline and the modal's full chart share the
 * same swr cache entry (key = ["candles", ticker, range]) so
 * opening the modal is instant -- no refetch.
 */

import { useMemo, useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import { ChartModal } from "@/components/ChartModal";
import { Sparkline, computePctChange, formatPriceShort } from "@/components/Sparkline";
import { TimeRangeSelector } from "@/components/TimeRangeSelector";
import {
  ASSET_UNIVERSE,
  CATEGORY_LABELS,
  CATEGORY_ORDER,
  groupAssetsByCategory,
  type AssetEntry,
} from "@/lib/assetUniverse";
import type { Candle, TimeRange, YfInterval } from "@/lib/types";

const BOT_TRADED_TICKERS = new Set<string>([
  "BTC-USD", "ETH-USD", "SOL-USD",
  "SPY", "QQQ", "GLD", "USO",
]);

// Mirrors lib/api.ts::rangeToParams -- duplicated here because
// the sparkline (in the card) and the full chart (in the modal)
// need the same mapping AND want to share the swr cache key.
// The mapping is the source of truth in lib/api.ts; if it
// changes, change both.
const RANGE_PARAMS: Record<TimeRange, { interval: YfInterval; limit: number }> = {
  "1D":  { interval: "5m",  limit: 100 },
  "5D":  { interval: "15m", limit: 200 },
  "1M":  { interval: "1d",  limit: 35  },
  "3M":  { interval: "1d",  limit: 95  },
  "1Y":  { interval: "1d",  limit: 370 },
  "ALL": { interval: "1wk", limit: 520 },
};

const TICKER_COLOR = {
  gain: "#3fb950",
  loss: "#f85149",
};

export default function ChartsPage() {
  const grouped = groupAssetsByCategory(ASSET_UNIVERSE);
  const [openAsset, setOpenAsset] = useState<AssetEntry | null>(null);

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">
            Live Charts
          </h1>
          <p className="text-sm text-muted">
            15 assets across crypto / forex / stocks+ETFs. Click any tile to
            expand. Use the time-range chips on each tile (or in the
            modal) to zoom in/out.
          </p>
        </div>
        <div className="text-[10px] uppercase tracking-wider text-muted">
          Sprint 60
        </div>
      </div>

      {CATEGORY_ORDER.map((cat) => {
        const assets = grouped[cat];
        if (assets.length === 0) return null;
        return (
          <section key={cat} className="space-y-3">
            <div className="flex items-baseline gap-3">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-cream-50">
                {CATEGORY_LABELS[cat]}
              </h2>
              <span className="text-[10px] text-muted">
                {assets.length} {assets.length === 1 ? "asset" : "assets"}
                {cat === "forex" && (
                  <span className="ml-2 rounded bg-blue-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-blue-300">
                    Read-only · bot does not trade forex
                  </span>
                )}
              </span>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {assets.map((a) => (
                <AssetCard
                  key={a.ticker}
                  asset={a}
                  onClick={() => setOpenAsset(a)}
                />
              ))}
            </div>
          </section>
        );
      })}

      {openAsset && (
        <ChartModal
          ticker={openAsset.ticker}
          label={openAsset.label}
          category={openAsset.category}
          onClose={() => setOpenAsset(null)}
        />
      )}
    </div>
  );
}

/**
 * AssetCard -- Sprint 60 MSN-style market tile.
 *
 * Compact card (one-third the height of the Sprint 59 chart
 * card) with: asset name, ticker chip, current price, % change
 * color, mini sparkline, and the time-range selector at the
 * bottom. The whole card is clickable to open the full chart
 * modal; the time-range chips stopPropagation so they update
 * the sparkline without triggering the modal.
 *
 * Owns its own `range` state so each tile can be zoomed
 * independently. The default is "1M" (1 month, 35 daily bars)
 * because a 30-day trend is the most useful "at-a-glance"
 * picture of an asset's health -- 1D is too noisy, 1Y is too
 * coarse for a tile.
 *
 * The sparkline shares the swr cache key with the modal's
 * full chart (`["candles", ticker, range]`), so opening the
 * modal at the same range is instant.
 */
function AssetCard({
  asset,
  onClick,
}: {
  asset: AssetEntry;
  onClick: () => void;
}) {
  const [range, setRange] = useState<TimeRange>("1M");
  const { interval, limit } = RANGE_PARAMS[range];
  const traded = BOT_TRADED_TICKERS.has(asset.ticker);

  // Same key as the modal's PriceChart fetcher, so opening the
  // modal at the same range reuses the cached candles.
  const { data, isLoading, error } = useSWR(
    ["candles", asset.ticker, range],
    () => api.candles(asset.ticker, interval, limit),
    { refreshInterval: 0, revalidateOnFocus: false },
  );

  const candles: Candle[] = useMemo(
    () => (data?.candles ? [...data.candles].sort((a, b) => a.ts - b.ts) : []),
    [data?.candles],
  );
  const { pct, up, last } = computePctChange(candles);
  const tickColor = up ? TICKER_COLOR.gain : TICKER_COLOR.loss;

  // Loading skeleton: only the sparkline + price show the
  // "loading" state. The header (name, chip) is always rendered
  // so the user sees what's loading.
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
      className="group cursor-pointer rounded-lg border border-ink-700 bg-ink-900/50 p-3 transition hover:border-gold/40 focus:outline-none focus:border-gold/60"
      aria-label={`Expand ${asset.label} chart`}
    >
      {/* Header: name (left) + ticker chip + view-only badge (right) */}
      <div className="flex items-baseline justify-between gap-2">
        <div className="truncate text-sm font-semibold text-cream-50">
          {asset.label}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="rounded bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] uppercase text-muted">
            {asset.ticker}
          </span>
          {!traded && (
            <span className="rounded bg-blue-500/20 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-blue-200">
              view
            </span>
          )}
        </div>
      </div>

      {/* Middle: price + % change */}
      <div className="mt-1.5 flex items-baseline gap-2">
        <div className="font-mono text-lg leading-none text-cream-50">
          {last != null ? formatPriceShort(last) : isLoading ? "…" : "—"}
        </div>
        {last != null && (
          <div
            className="font-mono text-xs leading-none"
            style={{ color: tickColor }}
          >
            {pct >= 0 ? "+" : ""}
            {pct.toFixed(2)}%
          </div>
        )}
      </div>

      {/* Sparkline -- 60px high, full width. If the candles
          haven't loaded yet, we show a subtle skeleton block
          instead of "no data" so the layout doesn't jump. */}
      <div className="mt-2 h-[60px]">
        {error ? (
          <div className="flex h-full items-center justify-center rounded bg-loss/5 text-[10px] text-loss">
            fetch failed
          </div>
        ) : isLoading && candles.length === 0 ? (
          <div className="h-full animate-pulse rounded bg-ink-800/60" />
        ) : candles.length > 0 ? (
          <Sparkline candles={candles} height={60} />
        ) : (
          <div className="flex h-full items-center justify-center text-[10px] text-muted">
            no data
          </div>
        )}
      </div>

      {/* Time-range selector at the bottom-right. We
          stopPropagation on the wrapper so clicking a chip
          only updates the range -- NOT the card (which would
          also open the modal). */}
      <div
        className="mt-2 flex justify-end"
        onClick={(e) => e.stopPropagation()}
      >
        <TimeRangeSelector value={range} onChange={setRange} />
      </div>
    </div>
  );
}
