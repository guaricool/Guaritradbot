"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { fmtPct, fmtUsd, pnlClass } from "@/lib/format";
import { Spinner } from "./Spinner";
import type { PositionSummary } from "@/lib/types";

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
                <tr key={p.id} className="animate-fade-in">
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
                  <td>
                    <button
                      onClick={() => onClose(p)}
                      disabled={closing === p.id}
                      className="btn-ghost text-xs text-loss hover:bg-loss/10"
                    >
                      {closing === p.id ? <Spinner /> : "Close"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
