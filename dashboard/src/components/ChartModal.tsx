"use client";

/**
 * ChartModal -- Sprint 59
 *
 * Fullscreen overlay that shows a single PriceChart at full size
 * with the time-range selector visible. Triggered by clicking a
 * chart card on /charts.
 *
 * UX details:
 *  - Dark backdrop (ink-950/90) blurs the page behind it
 *  - Click on backdrop closes the modal (clicking the chart
 *    itself does NOT close it -- otherwise the user couldn't
 *    select a different range without losing focus)
 *  - ESC key closes the modal (added in a useEffect on mount)
 *  - Body scroll is locked while the modal is open so the page
 *    underneath doesn't scroll when the user uses the wheel
 *  - The close button is in the top-right with the asset label
 *    and a backdrop-styled X icon
 *  - The time-range selector resets to "1M" on open so each
 *    freshly-opened modal starts at the same default view
 */

import { useEffect, useState } from "react";
import { PriceChart } from "@/components/PriceChart";
import type { TimeRange } from "@/lib/types";

interface ChartModalProps {
  /** yfinance-formatted ticker (e.g. "BTC-USD", "EURUSD=X") */
  ticker: string;
  /** Human label shown in the modal header (e.g. "Bitcoin") */
  label: string;
  /** Set to the asset's category so we can show a "read-only" hint
   *  for forex / non-traded stocks on the modal header. */
  category: "crypto" | "forex" | "equity";
  onClose: () => void;
}

export function ChartModal({ ticker, label, category, onClose }: ChartModalProps) {
  const [range, setRange] = useState<TimeRange>("1M");

  // ESC to close + body scroll lock while the modal is open. The
  // effect cleanup removes both when the modal unmounts so
  // re-opening later isn't affected by stale state.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`${label} chart`}
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-950/90 p-4 backdrop-blur-sm"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[92vh] w-full max-w-5xl flex-col rounded-xl border border-ink-700 bg-ink-900 p-5 shadow-2xl"
      >
        {/* Header */}
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <div className="flex items-baseline gap-3">
              <h2 className="text-2xl font-semibold text-cream-50">{label}</h2>
              <span className="rounded bg-ink-800 px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                {ticker}
              </span>
              {category === "forex" && (
                <span className="rounded bg-blue-500/15 px-2 py-0.5 text-[10px] uppercase tracking-wider text-blue-300">
                  Read-only · forex
                </span>
              )}
            </div>
            <p className="mt-1 text-xs text-muted">
              Click anywhere outside the chart to close. Press ESC to dismiss.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-ink-700 px-3 py-1.5 text-sm text-cream-50 hover:bg-ink-800"
          >
            Close
          </button>
        </div>

        {/* Chart -- PriceChart owns its own swr fetch keyed by
            (ticker, range); the modal just owns the range state
            and passes it down. height=520 gives a much larger
            chart than the 180px card view. The PriceChart renders
            its own (compact) range selector below the line, so
            we don't duplicate it here -- one selector is enough. */}
        <div className="flex-1 overflow-hidden">
          <PriceChart
            asset={label}
            ticker={ticker}
            range={range}
            onRangeChange={setRange}
            height={520}
            subtitle="Sprint 59 · click outside to close"
          />
        </div>
      </div>
    </div>
  );
}
