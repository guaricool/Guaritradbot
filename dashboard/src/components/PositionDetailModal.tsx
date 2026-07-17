"use client";

/**
 * PositionDetailModal -- big popup opened by clicking a position row
 * (Overview/Positions tables), instead of only navigating to the
 * full /positions/[id] page. Same "click outside or ESC to close"
 * pattern as ChartModal (Sprint 59), sized up for a live chart +
 * KPI header.
 *
 * Carlos asked for exactly this: click an open position, see a big
 * on-screen popup with the entry line, price above it in green
 * (winning) and below it in red (losing), moving live.
 */

import { useEffect } from "react";
import { PositionChart } from "@/components/PositionChart";
import { fmtPct, fmtUsd, pnlClass } from "@/lib/format";
import { useAnimatedNumber } from "@/lib/useAnimatedNumber";
import type { PositionSummary } from "@/lib/types";

export function PositionDetailModal({
  position,
  onClose,
}: {
  position: PositionSummary;
  onClose: () => void;
}) {
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

  const animatedPnl = useAnimatedNumber(position.unrealized_pnl_usd);
  const animatedPnlPct = useAnimatedNumber(position.unrealized_pnl_pct);
  const dirClass = position.direction === "long" ? "text-gain" : "text-loss";
  const dirArrow = position.direction === "long" ? "▲" : "▼";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`${position.asset} position detail`}
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-950/90 p-4 backdrop-blur-sm"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[92vh] w-full max-w-4xl flex-col rounded-xl border border-ink-700 bg-ink-900 p-5 shadow-2xl"
      >
        {/* Header */}
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <div className="flex items-baseline gap-3">
              <h2 className="font-display text-2xl font-semibold tracking-tight">
                {position.asset}
              </h2>
              <span className={`text-sm font-semibold ${dirClass}`}>
                {dirArrow} {position.direction.toUpperCase()}
              </span>
              <span className="rounded bg-ink-800 px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                {position.strategy}
              </span>
            </div>
            <p className="mt-1 text-xs text-muted">
              Live price, updated every ~1s. Click outside or press ESC to
              close.
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

        {/* KPI strip */}
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="kpi">
            <span className="kpi-label">Entry</span>
            <div className="kpi-value text-lg">
              {fmtUsd(position.entry_price, { decimals: 2 })}
            </div>
          </div>
          <div className="kpi">
            <span className="kpi-label">Current</span>
            <div className="kpi-value text-lg">
              {fmtUsd(position.current_price ?? null, { decimals: 2 })}
            </div>
          </div>
          <div className="kpi">
            <span className="kpi-label">Unrealized P&L</span>
            <div className={`kpi-value text-lg ${pnlClass(position.unrealized_pnl_usd ?? null)}`}>
              {fmtUsd(animatedPnl, { signed: true, decimals: 2 })}
            </div>
          </div>
          <div className="kpi">
            <span className="kpi-label">P&L %</span>
            <div className={`kpi-value text-lg ${pnlClass(position.unrealized_pnl_pct ?? null)}`}>
              {fmtPct(animatedPnlPct, { signed: true, decimals: 2 })}
            </div>
          </div>
        </div>

        {/* Live chart -- green above entry / red below, per PositionChart's
            entry-relative gradient. */}
        <div className="flex-1 overflow-hidden">
          <PositionChart positionId={position.id} interval="5m" height={440} />
        </div>
      </div>
    </div>
  );
}
