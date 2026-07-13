"use client";

import { Fragment, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { fmtPct, fmtUsd, pnlClass } from "@/lib/format";
import { Spinner } from "./Spinner";
import { PositionChart } from "./PositionChart";
import type { PositionSummary } from "@/lib/types";

// Sprint 46S: Carlos wanted the entry chart to show up automatically as
// soon as a position opens, for ANY asset (crypto or equity) -- not just
// on the position detail page, which needed a click to reach. The
// underlying chart component and /api/positions/{id}/candles endpoint
// already worked for any asset (yfinance ticker lookup, asset-agnostic);
// this just surfaces it inline in the table itself so it's automatic.
// Kept collapsible (default OPEN) rather than unconditionally rendered,
// since each chart is its own ~300px-tall live-refreshing panel and
// max_open_trades can be up to 5 -- a collapse escape hatch avoids an
// unbounded-height page once more than 1-2 positions are open, without
// giving up the "shows automatically" behavior Carlos asked for.
const COLLAPSED_STORAGE_KEY = "guaritradbot_collapsed_position_charts";

function readCollapsedSet(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.sessionStorage.getItem(COLLAPSED_STORAGE_KEY);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch {
    return new Set();
  }
}

function writeCollapsedSet(s: Set<string>) {
  if (typeof window === "undefined") return;
  try {
    // Sprint 46Z (audit M13): use Array.from instead of `[...s]`.
    // Spreading a Set requires either --downlevelIteration or an
    // ES2015+ target; Array.from works under the default
    // tsconfig (no downlevel) and is also explicit about the
    // conversion. Same JSON output.
    window.sessionStorage.setItem(
      COLLAPSED_STORAGE_KEY,
      JSON.stringify(Array.from(s)),
    );
  } catch {
    // best-effort only
  }
}

export function PositionTable({
  positions,
  loading,
  emptyHint,
}: {
  positions: PositionSummary[];
  loading?: boolean;
  emptyHint?: string;
}) {
  const router = useRouter();
  const [closing, setClosing] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Sprint 46S: which position IDs have their auto-shown chart collapsed.
  // Default (id absent from the set) = EXPANDED, i.e. the chart shows up
  // automatically the moment a position appears in this table.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsedSet());

  function toggleChart(id: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeCollapsedSet(next);
      return next;
    });
  }

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center text-muted">
        <Spinner className="h-5 w-5" />
      </div>
    );
  }

  if (!positions.length) {
    return (
      <div className="flex h-40 flex-col items-center justify-center gap-1 text-muted">
        <span className="text-2xl">∅</span>
        <span className="text-sm">
          {emptyHint ?? "No open positions."}
        </span>
      </div>
    );
  }

  async function onClose(p: PositionSummary) {
    if (
      !confirm(
        `Close ${p.direction.toUpperCase()} ${p.asset} at entry price? (bot will reconcile on next cycle)`,
      )
    ) {
      return;
    }
    setErr(null);
    setClosing(p.id);
    try {
      await api.closePosition(p.id);
      router.refresh();
    } catch (e: unknown) {
      const err = e as { message?: string };
      setErr(err?.message || "Failed to close");
    } finally {
      setClosing(null);
    }
  }

  return (
    <div className="space-y-2">
      {err && (
        <div className="rounded border border-loss/30 bg-loss/10 px-3 py-1.5 text-xs text-loss">
          {err}
        </div>
      )}
      <div className="overflow-x-auto scrollbar-thin">
        <table className="table-zebra">
          <thead>
            <tr>
              <th>Asset</th>
              <th>Side</th>
              <th className="text-right">Qty</th>
              <th className="text-right">Entry</th>
              <th className="text-right">Now</th>
              <th className="text-right">SL / TP</th>
              <th className="text-right">Notional</th>
              <th className="text-right">uPnL</th>
              <th className="text-right">uPnL %</th>
              <th className="text-right">Age</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => {
              const dirClass =
                p.direction === "long" ? "text-gain" : "text-loss";
              const dirArrow = p.direction === "long" ? "▲" : "▼";
              return (
                <Fragment key={p.id}>
                <tr className="animate-fade-in">
                  <td className="font-semibold">
                    <a
                      href={`/positions/${p.id}`}
                      className="hover:text-gold hover:underline"
                    >
                      {p.asset}
                    </a>
                    <div className="text-[10px] uppercase tracking-wider text-muted">
                      {p.strategy}
                    </div>
                  </td>
                  <td>
                    <span className={dirClass}>
                      {dirArrow} {p.direction.toUpperCase()}
                    </span>
                  </td>
                  <td className="num-cell">{p.qty}</td>
                  <td className="num-cell">
                    {fmtUsd(p.entry_price, { decimals: 2 })}
                  </td>
                  <td className="num-cell">
                    {fmtUsd(p.current_price ?? null, { decimals: 2 })}
                    {p.current_price_source === "entry_fallback" && (
                      <div className="text-[10px] text-gold/80">fallback</div>
                    )}
                    {p.current_price_source === "fetch_failed" && (
                      <div className="text-[10px] text-loss/80">offline</div>
                    )}
                  </td>
                  <td className="num-cell text-[11px]">
                    <div className="text-loss">
                      {fmtUsd(p.stop_loss, { decimals: 2 })}
                    </div>
                    <div className="text-gain">
                      {fmtUsd(p.take_profit, { decimals: 2 })}
                    </div>
                  </td>
                  <td className="num-cell">
                    {fmtUsd(p.notional_usd, { decimals: 0 })}
                  </td>
                  <td className={`num-cell ${pnlClass(p.unrealized_pnl_usd ?? null)}`}>
                    {fmtUsd(p.unrealized_pnl_usd ?? null, {
                      signed: true,
                      decimals: 2,
                    })}
                  </td>
                  <td className={`num-cell ${pnlClass(p.unrealized_pnl_pct ?? null)}`}>
                    {fmtPct(p.unrealized_pnl_pct ?? null, {
                      signed: true,
                      decimals: 2,
                    })}
                  </td>
                  <td className="num-cell text-muted">
                    {p.age_hours !== null
                      ? p.age_hours < 48
                        ? `${p.age_hours.toFixed(1)}h`
                        : `${(p.age_hours / 24).toFixed(1)}d`
                      : "—"}
                  </td>
                  <td className="whitespace-nowrap">
                    <button
                      onClick={() => toggleChart(p.id)}
                      className="btn-ghost mr-1 text-xs text-muted hover:text-gold"
                      title={
                        collapsed.has(p.id) ? "Show entry chart" : "Hide entry chart"
                      }
                    >
                      {collapsed.has(p.id) ? "▸ Chart" : "▾ Chart"}
                    </button>
                    <button
                      onClick={() => onClose(p)}
                      disabled={closing === p.id}
                      className="btn-ghost text-xs text-loss hover:bg-loss/10"
                    >
                      {closing === p.id ? <Spinner /> : "Close"}
                    </button>
                  </td>
                </tr>
                {/* Sprint 46S: auto-shown entry chart -- same component/
                    endpoint the position detail page uses, works for any
                    asset (crypto or equity) since /api/positions/{id}/candles
                    is a generic yfinance lookup keyed on the position's own
                    asset field. Defaults to visible (see `collapsed`'s
                    docstring above) so it appears automatically the moment
                    a position shows up in this table -- no click required. */}
                {!collapsed.has(p.id) && (
                  <tr key={`${p.id}-chart`} className="bg-ink-900/40">
                    <td colSpan={11} className="p-3">
                      <PositionChart positionId={p.id} interval="15m" height={300} />
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
