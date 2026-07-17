"use client";

import { use } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import { PositionChart } from "@/components/PositionChart";
import { KpiCard } from "@/components/KpiCard";
import { PositionDetailSkeleton } from "@/components/Skeleton";
import { fmtPct, fmtUsd, fmtTimestamp } from "@/lib/format";
import { ModeToggle } from "@/components/ModeToggle";
import { useState } from "react";
import Link from "next/link";

export default function PositionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { data: pos, error } = useSWR(["pos", id], () => api.position(id), {
    refreshInterval: 10_000,
  });
  const { data: state } = useSWR("state", () => api.state());
  const [interval, setInterval] = useState<"1m" | "5m" | "15m" | "1h" | "1d">(
    "15m",
  );

  if (error) {
    return (
      <div className="rounded border border-loss/30 bg-loss/10 p-4 text-sm text-loss">
        Position not found or already closed.
        <Link href="/positions" className="ml-3 text-gold underline">
          ← back to positions
        </Link>
      </div>
    );
  }
  if (!pos || !state) return <PositionDetailSkeleton />;

  const dirColor = pos.direction === "long" ? "text-gain" : "text-loss";
  const dirArrow = pos.direction === "long" ? "▲" : "▼";

  return (
    <div className="space-y-5 animate-fade-in">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Link
            href="/positions"
            className="text-xs text-muted hover:text-cream-50"
          >
            ← Positions
          </Link>
          <h1 className="mt-1 flex items-center gap-3 font-display text-2xl font-semibold tracking-tight">
            <span>{pos.asset}</span>
            <span className={`text-base ${dirColor}`}>
              {dirArrow} {pos.direction.toUpperCase()}
            </span>
            <span className="rounded-full bg-ink-800 px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted">
              {pos.strategy}
            </span>
          </h1>
          <p className="mt-0.5 text-sm text-muted">
            Opened {fmtTimestamp(pos.entry_ts)}
          </p>
        </div>
        <ModeToggle mode={state.mode} size="sm" />
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiCard
          label="Entry"
          value={fmtUsd(pos.entry_price, { decimals: 2 })}
        />
        <KpiCard
          label="Current"
          value={fmtUsd(pos.current_price, { decimals: 2 })}
          tone={
            pos.current_price_source === "fetch_failed" ? "neutral" : "gold"
          }
          hint={pos.current_price_source}
        />
        <KpiCard
          label="uPnL"
          value={fmtUsd(pos.unrealized_pnl_usd, { signed: true, decimals: 2 })}
          tone={
            (pos.unrealized_pnl_usd ?? 0) > 0
              ? "gain"
              : (pos.unrealized_pnl_usd ?? 0) < 0
                ? "loss"
                : "neutral"
          }
          hint={fmtPct(pos.unrealized_pnl_pct, { signed: true, decimals: 2 })}
        />
        <KpiCard
          label="Notional"
          value={fmtUsd(pos.notional_usd, { decimals: 0 })}
          hint={`qty ${pos.qty}`}
        />
      </div>

      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Price chart</span>
          <div className="flex items-center gap-1 rounded-md bg-ink-800 p-0.5 text-[11px]">
            {(["1m", "5m", "15m", "1h", "1d"] as const).map((iv) => (
              <button
                key={iv}
                onClick={() => setInterval(iv)}
                className={`rounded px-2 py-0.5 transition ${
                  iv === interval
                    ? "bg-gold/20 text-gold"
                    : "text-muted hover:text-cream-50"
                }`}
              >
                {iv}
              </button>
            ))}
          </div>
        </div>
        <div className="p-4">
          <PositionChart positionId={id} interval={interval} height={420} />
        </div>
      </section>

      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Position details</span>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 p-4 text-sm md:grid-cols-3">
          <Detail label="Position ID" value={pos.id} mono />
          <Detail label="Asset" value={pos.asset} />
          <Detail label="Direction" value={pos.direction.toUpperCase()} />
          <Detail label="Entry price" value={fmtUsd(pos.entry_price)} mono />
          <Detail label="Stop loss" value={fmtUsd(pos.stop_loss)} mono />
          <Detail label="Take profit" value={fmtUsd(pos.take_profit)} mono />
          <Detail label="Quantity" value={String(pos.qty)} mono />
          <Detail label="Notional (USD)" value={fmtUsd(pos.notional_usd, { decimals: 2 })} mono />
          <Detail label="Strategy" value={pos.strategy} />
        </div>
      </section>
    </div>
  );
}

function Detail({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="kpi-label mb-0.5">{label}</div>
      <div className={mono ? "font-mono num text-sm" : "text-sm"}>{value}</div>
    </div>
  );
}
