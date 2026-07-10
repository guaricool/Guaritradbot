"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import { KpiCard } from "@/components/KpiCard";
import { PositionTable } from "@/components/PositionTable";
import { ModeToggle } from "@/components/ModeToggle";
import { EquityChart } from "@/components/EquityChart";
import { PageSpinner } from "@/components/Spinner";
import { fmtPct, fmtUsd } from "@/lib/format";
import { useLive } from "@/lib/use-live";
import { useEffect, useMemo, useState } from "react";
import type { PositionSummary } from "@/lib/types";

export default function HomePage() {
  const { data, error, isLoading, mutate } = useSWR("state", () => api.state(), {
    refreshInterval: 10_000,
  });
  const [livePositions, setLivePositions] = useState<PositionSummary[] | null>(null);

  // Live updates via WS — when we get a positions update, refresh KPI math
  const { status } = useLive({
    onMessage: (msg) => {
      if (msg.type === "positions") {
        setLivePositions(msg.positions);
      }
      if (msg.type === "audit") {
        // No-op; the audit page handles its own refresh. We just bump state.
      }
    },
  });

  const positions = useMemo<PositionSummary[]>(() => {
    if (livePositions) return livePositions;
    return data?.positions ?? [];
  }, [livePositions, data?.positions]);

  // When WS disconnects, drop the live override and fall back to SWR
  useEffect(() => {
    if (status !== "open") setLivePositions(null);
  }, [status]);

  if (isLoading) return <PageSpinner />;
  if (error || !data) {
    return (
      <div className="rounded border border-loss/30 bg-loss/10 p-4 text-sm text-loss">
        Failed to load dashboard: {String(((error as { message?: string })?.message) ?? error)}
        <button onClick={() => mutate()} className="btn-ghost ml-3 text-xs">
          Retry
        </button>
      </div>
    );
  }

  const unrealized = positions.reduce(
    (s, p) => s + (p.unrealized_pnl_usd ?? 0),
    0,
  );
  const exposure = positions.reduce((s, p) => s + p.notional_usd, 0);
  const unrealizedPct = exposure > 0 ? unrealized / exposure : 0;
  const dailyPnl = data.daily_realized_pnl_usd ?? 0;
  const totalPnl = data.total_realized_pnl_usd ?? 0;

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">
            Overview
          </h1>
          <p className="text-sm text-muted">
            Live snapshot of the bot&apos;s positions, P&amp;L, and exposure.
          </p>
        </div>
        <ModeToggle mode={data.mode} />
      </header>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiCard
          label="Open positions"
          value={positions.length}
          hint={
            <span>
              of {Math.max(positions.length, 5)} max · exposure{" "}
              {fmtUsd(exposure, { decimals: 0 })}
            </span>
          }
          icon={<span>◉</span>}
        />
        <KpiCard
          label="Unrealized P&L"
          value={fmtUsd(unrealized, { signed: true, decimals: 2 })}
          tone={unrealized > 0 ? "gain" : unrealized < 0 ? "loss" : "neutral"}
          hint={fmtPct(unrealizedPct, { signed: true, decimals: 2 })}
        />
        <KpiCard
          label="Realized today"
          value={fmtUsd(dailyPnl, { signed: true, decimals: 2 })}
          tone={dailyPnl > 0 ? "gain" : dailyPnl < 0 ? "loss" : "neutral"}
          hint={`Total ${fmtUsd(totalPnl, { signed: true, decimals: 2 })}`}
        />
        <KpiCard
          label="Mode"
          value={
            <span className="text-base">
              {data.mode.mode === "live" ? "LIVE" : "PAPER"}
            </span>
          }
          tone={data.mode.mode === "live" ? "loss" : "neutral"}
          hint={data.mode.use_testnet ? "testnet broker" : "real broker"}
        />
      </div>

      {/* Positions table */}
      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Open Positions</span>
          <span className="text-xs text-muted">
            P&amp;L recomputed every 2s via WebSocket
          </span>
        </div>
        <div className="p-4">
          <PositionTable
            positions={positions}
            emptyHint="Flat — no open positions."
          />
        </div>
      </section>

      {/* Equity curve */}
      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Equity Curve</span>
          <span className="text-xs text-muted">Last 30 days · realized only</span>
        </div>
        <div className="p-4">
          <EquityChart windowDays={30} />
        </div>
      </section>
    </div>
  );
}
