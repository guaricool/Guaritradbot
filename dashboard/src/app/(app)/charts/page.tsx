"use client";

/**
 * /charts -- Sprint 58 + Sprint 59
 *
 * Live candlestick line charts for every asset in the dashboard's
 * universe. Sprint 58: 7 tradeable assets in a single grid.
 * Sprint 59: expanded to 15 assets across 3 categories
 * (crypto / forex / equity), with:
 *   - Per-asset time-range selector (1D/5D/1M/3M/1Y/ALL)
 *   - Click any card to open a fullscreen ChartModal with the
 *     chart at 520px height (vs 180px in the card)
 *   - "Read-only" badge on forex + non-traded stocks so Carlos
 *     knows those don't drive bot decisions
 *
 * Each card owns its own PriceChart instance, which in turn owns
 * its own swr cache entry. This means a range change in one
 * card doesn't re-fetch the others, and the modal opening
 * doesn't disturb the card view.
 */

import { useState } from "react";
import { PriceChart } from "@/components/PriceChart";
import { ChartModal } from "@/components/ChartModal";
import {
  ASSET_UNIVERSE,
  CATEGORY_LABELS,
  CATEGORY_ORDER,
  groupAssetsByCategory,
  type AssetEntry,
} from "@/lib/assetUniverse";
import type { TimeRange } from "@/lib/types";

const BOT_TRADED_TICKERS = new Set<string>([
  "BTC-USD", "ETH-USD", "SOL-USD",
  "SPY", "QQQ", "GLD", "USO",
]);

export default function ChartsPage() {
  const grouped = groupAssetsByCategory(ASSET_UNIVERSE);
  const [openAsset, setOpenAsset] = useState<AssetEntry | null>(null);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold text-cream-50">Live Charts</h1>
          <p className="text-xs text-muted">
            15 assets across crypto / forex / stocks+ETFs. Click any card to
            expand. Use the time-range selector to zoom in (intraday) or out
            (multi-year).
          </p>
        </div>
        <div className="text-[10px] uppercase tracking-wider text-muted">
          Sprint 59
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
 * AssetCard -- a single clickable chart card.
 *
 * Owns its own `range` state so each card can be zoomed
 * independently. The whole card is the click target for opening
 * the modal; the chart's time-range buttons stay clickable
 * because PriceChart's stopPropagation on its own buttons is
 * handled by the swr click handler (no propagation issue with
 * React button events here).
 */
function AssetCard({
  asset,
  onClick,
}: {
  asset: AssetEntry;
  onClick: () => void;
}) {
  // Each card keeps its own range state so a 1D click on one card
  // doesn't reset everyone else. The default "1M" is the most
  // common zoom level; users can re-pick per card.
  const [range, setRange] = useState<TimeRange>("1M");
  const traded = BOT_TRADED_TICKERS.has(asset.ticker);
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
      className="cursor-pointer rounded-lg ring-1 ring-transparent transition hover:ring-gold/40 focus:outline-none focus:ring-gold/60"
      aria-label={`Expand ${asset.label} chart`}
    >
      <div className="relative">
        <PriceChart
          asset={asset.label}
          ticker={asset.ticker}
          range={range}
          onRangeChange={setRange}
          height={200}
        />
        {/* "Read-only" badge overlays the chart's top-right so
            it's visible without consuming header space. Hidden
            for tradeable assets to keep the UI quiet. */}
        {!traded && (
          <span className="pointer-events-none absolute right-3 top-3 rounded bg-blue-500/20 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-blue-200">
            view-only
          </span>
        )}
      </div>
    </div>
  );
}
