"use client";

import useSWR from "swr";
import { Bitcoin, TrendingUp, Wallet, CircleDot } from "lucide-react";
import { api } from "@/lib/api";
import { KpiCard } from "@/components/KpiCard";
import { PositionTable } from "@/components/PositionTable";
import { ModeToggle } from "@/components/ModeToggle";
import { TradingPauseToggle } from "@/components/TradingPauseToggle";
import { ScalpModeToggle } from "@/components/ScalpModeToggle";
import { EquityChart } from "@/components/EquityChart";
import { OverviewPageSkeleton } from "@/components/Skeleton";
import { fmtPct, fmtUsd } from "@/lib/format";
import { useAnimatedNumber } from "@/lib/useAnimatedNumber";
import { useFlashOnChange } from "@/lib/useFlashOnChange";
import { useRollingHistory } from "@/lib/useRollingHistory";
import { MiniTrend } from "@/components/MiniTrend";
import { useLive } from "@/lib/use-live";
import { useEffect, useMemo, useState } from "react";
import type { PositionSummary } from "@/lib/types";
import { ApiError } from "@/lib/api";

// Sprint 46C: short, human hint under each broker balance card explaining
// WHY the value is "—" when it isn't a real live balance — a bare "$0.00"
// could be misread as "your account really has zero dollars".
function balanceHint(source: string): string {
  switch (source) {
    case "live":
      return "live from broker";
    case "cache":
      return "cached (<15s old)";
    case "not_configured":
      return "no API keys set";
    case "unavailable":
      return "fetch failed — check keys/network";
    default:
      return "unknown";
  }
}

export default function HomePage() {
  const { data, error, isLoading, mutate } = useSWR("state", () => api.state(), {
    refreshInterval: 10_000,
  });
  const [livePositions, setLivePositions] = useState<PositionSummary[] | null>(null);
  const [closingAll, setClosingAll] = useState(false);
  const [closeAllMsg, setCloseAllMsg] = useState<string | null>(null);

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

  // Computed above the early returns (and useAnimatedNumber called
  // unconditionally right after) so hook order stays stable across
  // loading/error/loaded renders -- rules-of-hooks forbids calling a
  // hook only on some render paths.
  const unrealized = positions.reduce(
    (s, p) => s + (p.unrealized_pnl_usd ?? 0),
    0,
  );
  const exposure = positions.reduce((s, p) => s + p.notional_usd, 0);
  const unrealizedPct = exposure > 0 ? unrealized / exposure : 0;
  // Tween the hero P&L number between the ~1s live snapshots (see
  // server.py's _position_snapshot_loop + state.py's live-broker-ticker
  // price fix) so it visibly glides instead of stair-stepping.
  const animatedUnrealized = useAnimatedNumber(unrealized);
  // Flash the card background translucent green/red on each real
  // backend update (DESIGN.md's cell-flash spec), and keep a rolling
  // in-session history of the raw value for the corner MiniTrend --
  // both driven off the raw `unrealized` so they fire on genuine
  // updates rather than every eased animation frame.
  const unrealizedFlash = useFlashOnChange(unrealized);
  const unrealizedHistory = useRollingHistory(unrealized);
  const effectiveBalanceHistory = useRollingHistory(
    data?.effective_balance_usd ?? null,
  );

  if (isLoading) return <OverviewPageSkeleton />;
  if (error || !data) {
    return (
      <div className="rounded border border-loss/30 bg-loss/10 p-4 text-sm text-loss">
        Can&apos;t reach the bot&apos;s API. Check that it&apos;s running and
        reachable from this browser, then retry.
        <div className="mt-1 text-xs text-loss/70">
          {String(((error as { message?: string })?.message) ?? error)}
        </div>
        <button onClick={() => mutate()} className="btn-ghost mt-2 text-xs">
          Retry
        </button>
      </div>
    );
  }

  const dailyPnl = data.daily_realized_pnl_usd ?? 0;
  const totalPnl = data.total_realized_pnl_usd ?? 0;

  // Sprint 46H: "clean session" helper before flipping paper → live.
  // Closes every open position in the LOCAL repo (correct for paper —
  // see api.closeAllPositions' JSDoc for the live-mode caveat).
  async function handleCloseAll() {
    if (positions.length === 0) return;
    if (
      !confirm(
        `Close all ${positions.length} open position(s)? This clears the local repo ` +
          `(correct in PAPER mode). In LIVE mode this does NOT place real exchange orders.`,
      )
    ) {
      return;
    }
    setCloseAllMsg(null);
    setClosingAll(true);
    try {
      const res = await api.closeAllPositions();
      setCloseAllMsg(`Closed ${res.closed_count} position(s).`);
      setLivePositions(null);
      await mutate();
    } catch (e: unknown) {
      const err = e instanceof ApiError ? e.message : String((e as { message?: string })?.message ?? e);
      setCloseAllMsg(`Failed: ${err}`);
    } finally {
      setClosingAll(false);
    }
  }

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
        <div className="flex items-start gap-3">
          <TradingPauseToggle />
          <ScalpModeToggle />
          <ModeToggle mode={data.mode} />
        </div>
      </header>

      {/* KPIs — asymmetric bento (DESIGN.md: "destacar visualmente las
          metricas principales... de las secundarias" instead of a row
          of equal-weight cards). Balance + unrealized P&L are the two
          numbers that matter most moment-to-moment, so they get the
          hero slots; broker reference balances, open-position count,
          and today's realized P&L are supporting detail underneath. */}
      <div className="grid gap-3 md:grid-cols-2 stagger-children">
        <KpiCard
          size="lg"
          // Sprint 62 (+ gap fix): "Available balance" — cash NOT
          // currently locked in an open position. In paper mode this
          // is the virtual paper starting balance + realized P&L -
          // notional of open positions, so opening a position visibly
          // debits it (was previously starting + P&L only, which
          // never moved until a position closed — looked like the
          // full balance was still "available" mid-trade). The real
          // binance/alpaca balances are shown below as smaller
          // "Reference" cards so the user can still see them.
          label={data.mode.mode === "paper" ? "Paper balance available" : "Available balance"}
          value={
            data.effective_balance_usd !== null && data.effective_balance_usd !== undefined
              ? fmtUsd(data.effective_balance_usd, { decimals: 2 })
              : "—"
          }
          tone={data.mode.mode === "paper" ? "gain" : "gold"}
          hint={
            data.mode.mode === "paper"
              ? data.paper_starting_balance_usd
                ? `virtual $${data.paper_starting_balance_usd.toFixed(0)} start + P&L − open positions`
                : "virtual paper account"
              : "live from broker, minus open positions"
          }
          icon={<Wallet size={18} strokeWidth={2} />}
          trend={
            effectiveBalanceHistory.length >= 2 ? (
              <MiniTrend values={effectiveBalanceHistory} width={110} height={36} />
            ) : undefined
          }
        />
        <KpiCard
          size="lg"
          label="Unrealized P&L"
          value={fmtUsd(animatedUnrealized, { signed: true, decimals: 2 })}
          tone={unrealized > 0 ? "gain" : unrealized < 0 ? "loss" : "neutral"}
          hint={`${fmtPct(unrealizedPct, { signed: true, decimals: 2 })} of exposure`}
          flashClassName={unrealizedFlash}
          trend={
            unrealizedHistory.length >= 2 ? (
              <MiniTrend values={unrealizedHistory} width={110} height={36} />
            ) : undefined
          }
        />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 stagger-children">
        <KpiCard
          label="Binance.US"
          value={
            data.binance_balance_usd !== null && data.binance_balance_usd !== undefined
              ? fmtUsd(data.binance_balance_usd, { decimals: 2 })
              : "—"
          }
          tone="gold"
          hint={
            data.mode.mode === "paper"
              ? `${balanceHint(data.binance_balance_source)} · reference only`
              : balanceHint(data.binance_balance_source)
          }
          icon={<Bitcoin size={16} strokeWidth={2} />}
        />
        <KpiCard
          label="Alpaca"
          value={
            data.alpaca_balance_usd !== null && data.alpaca_balance_usd !== undefined
              ? fmtUsd(data.alpaca_balance_usd, { decimals: 2 })
              : "—"
          }
          tone="gold"
          hint={
            data.mode.mode === "paper"
              ? `${balanceHint(data.alpaca_balance_source)} · reference only`
              : balanceHint(data.alpaca_balance_source)
          }
          icon={<TrendingUp size={16} strokeWidth={2} />}
        />
        <KpiCard
          label="Open positions"
          value={positions.length}
          hint={
            <span>
              of {Math.max(positions.length, 5)} max · exposure{" "}
              {fmtUsd(exposure, { decimals: 0 })}
            </span>
          }
          icon={<CircleDot size={16} strokeWidth={2} />}
        />
        <KpiCard
          label="Realized today"
          value={fmtUsd(dailyPnl, { signed: true, decimals: 2 })}
          tone={dailyPnl > 0 ? "gain" : dailyPnl < 0 ? "loss" : "neutral"}
          hint={`Total ${fmtUsd(totalPnl, { signed: true, decimals: 2 })}`}
        />
      </div>

      {/* Positions table */}
      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Open Positions</span>
          <div className="flex items-center gap-3">
            <span className="text-xs text-muted">
              P&amp;L recomputed every 2s via WebSocket
            </span>
            {positions.length > 0 && (
              <button
                onClick={handleCloseAll}
                disabled={closingAll}
                className="btn-ghost text-xs text-loss disabled:opacity-50"
                title="Force-close every open position (repo-only in live — see docs)"
              >
                {closingAll ? "Closing…" : "Close all"}
              </button>
            )}
          </div>
        </div>
        {closeAllMsg && (
          <div className="border-b border-ink-700 px-4 py-2 text-xs text-muted">
            {closeAllMsg}
          </div>
        )}
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
