"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import { KpiCard } from "@/components/KpiCard";
import { CardSkeleton } from "@/components/Skeleton";
import { fmtPct, fmtUsd } from "@/lib/format";
import { clsx } from "clsx";
import type { Allocation, CorrelationResult, CVaRResult, StressResult } from "@/lib/types";

export default function AllocationPage() {
  const { data: alloc, error: allocErr } = useSWR<Allocation>(
    "allocation",
    () => api.allocation(),
  );
  const { data: stress, error: stressErr } = useSWR<StressResult>(
    "risk-stress",
    () => api.riskStress(),
  );
  const { data: corr, error: corrErr } = useSWR<CorrelationResult>(
    "risk-corr",
    () => api.riskCorrelation(),
  );
  const { data: cvar, error: cvarErr } = useSWR<CVaRResult>(
    "risk-cvar",
    () => api.riskCvar(),
  );

  if (!alloc && !allocErr) {
    return (
      <div className="space-y-5">
        <header>
          <div className="h-7 w-56 animate-pulse rounded-md bg-ink-800" />
          <div className="mt-2 h-4 w-96 animate-pulse rounded-md bg-ink-800" />
        </header>
        <div className="card overflow-hidden">
          <CardSkeleton lines={4} />
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="card overflow-hidden">
            <CardSkeleton lines={3} />
          </div>
          <div className="card grid grid-cols-2 gap-3 overflow-hidden p-4">
            <div className="kpi">
              <div className="h-3 w-16 animate-pulse rounded-md bg-ink-700" />
              <div className="mt-1 h-7 w-20 animate-pulse rounded-md bg-ink-700" />
            </div>
            <div className="kpi">
              <div className="h-3 w-16 animate-pulse rounded-md bg-ink-700" />
              <div className="mt-1 h-7 w-20 animate-pulse rounded-md bg-ink-700" />
            </div>
            <div className="kpi">
              <div className="h-3 w-16 animate-pulse rounded-md bg-ink-700" />
              <div className="mt-1 h-7 w-20 animate-pulse rounded-md bg-ink-700" />
            </div>
            <div className="kpi">
              <div className="h-3 w-16 animate-pulse rounded-md bg-ink-700" />
              <div className="mt-1 h-7 w-20 animate-pulse rounded-md bg-ink-700" />
            </div>
          </div>
        </div>
        <div className="card overflow-hidden">
          <CardSkeleton lines={3} />
        </div>
      </div>
    );
  }

  const targets = alloc?.target_weights ?? {};
  const actuals = alloc?.actual_weights ?? {};
  const drifts = alloc?.drifts ?? {};
  const classes = Object.keys({ ...targets, ...actuals });

  return (
    <div className="space-y-5 animate-fade-in">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">
          Allocation &amp; Risk
        </h1>
        <p className="text-sm text-muted">
          Drift, stress, correlation, and tail-risk snapshots over the current
          portfolio.
        </p>
      </header>

      {/* Allocation drift */}
      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Asset-class allocation</span>
          {alloc && (
            <span
              className={clsx(
                "text-xs",
                alloc.within_tolerance ? "text-gain" : "text-loss",
              )}
            >
              {alloc.within_tolerance
                ? "Within tolerance"
                : `Max drift ${alloc.max_abs_drift_pct.toFixed(1)}%`}
            </span>
          )}
        </div>
        <div className="p-4">
          {allocErr ? (
            <div className="text-sm text-loss">Couldn&apos;t load allocation data — check the bot API connection.</div>
          ) : !classes.length ? (
            <div className="text-sm text-muted">No open positions to allocate.</div>
          ) : (
            <div className="space-y-2">
              {classes.map((cls) => {
                const target = targets[cls] ?? 0;
                const actual = actuals[cls] ?? 0;
                const drift = drifts[cls] ?? 0;
                const driftPct = drift * 100;
                return (
                  <div key={cls} className="grid grid-cols-12 items-center gap-2 text-xs">
                    <div className="col-span-2 font-medium">{cls}</div>
                    <div className="col-span-7">
                      <div className="relative h-3 overflow-hidden rounded-full bg-ink-800">
                        <div
                          className="absolute inset-y-0 left-0 bg-gold/30"
                          style={{ width: `${Math.min(100, target * 100)}%` }}
                        />
                        <div
                          className="absolute inset-y-0 left-0 bg-gain"
                          style={{ width: `${Math.min(100, actual * 100)}%` }}
                        />
                        <div
                          className="absolute inset-y-0 w-px bg-cream-50/60"
                          style={{ left: `${Math.min(100, target * 100)}%` }}
                        />
                      </div>
                    </div>
                    <div className="col-span-2 num-cell">
                      {(actual * 100).toFixed(1)}% / {(target * 100).toFixed(1)}%
                    </div>
                    <div
                      className={clsx(
                        "col-span-1 num-cell text-[11px]",
                        Math.abs(drift) < 0.05
                          ? "text-muted"
                          : drift > 0
                            ? "text-gain"
                            : "text-loss",
                      )}
                    >
                      {driftPct > 0 ? "+" : ""}
                      {driftPct.toFixed(1)}%
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>

      <div className="grid gap-3 md:grid-cols-2">
        {/* Stress */}
        <section className="card overflow-hidden">
          <div className="card-header">
            <span>Recession stress test</span>
          </div>
          <div className="p-4">
            {stressErr ? (
              <div className="text-sm text-loss">Couldn&apos;t load stress scenarios.</div>
            ) : !stress?.scenarios?.length ? (
              <div className="text-sm text-muted">No open positions to stress.</div>
            ) : (
              <div className="space-y-3">
                {stress.scenarios.map((s) => (
                  <div
                    key={s.name}
                    className="rounded-lg border border-ink-700 bg-ink-800 p-3"
                  >
                    <div className="flex items-center justify-between">
                      <div className="text-sm font-medium">{s.name}</div>
                      <div className="num-cell text-xs text-loss">
                        −{(s.drawdown_pct * 100).toFixed(1)}%
                      </div>
                    </div>
                    <div className="mt-1 text-[11px] text-muted">
                      {s.description}
                    </div>
                    <div className="mt-2 num-cell text-sm text-cream-50/90">
                      Impact: {fmtUsd(s.portfolio_impact_usd, { signed: true })}
                    </div>
                  </div>
                ))}
                {stress.worst_case && (
                  <div className="rounded-lg border border-loss/30 bg-loss/10 p-3">
                    <div className="text-xs uppercase tracking-wider text-loss/90">
                      Worst case
                    </div>
                    <div className="mt-1 text-sm font-medium">
                      {stress.worst_case.name}
                    </div>
                    <div className="num-cell text-sm">
                      {fmtUsd(stress.worst_case.portfolio_impact_usd, { signed: true })}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </section>

        {/* CVaR */}
        <section className="card overflow-hidden">
          <div className="card-header">
            <span>Tail risk (CVaR)</span>
          </div>
          <div className="grid grid-cols-2 gap-3 p-4 stagger-children">
            {cvarErr ? (
              <div className="col-span-2 text-sm text-loss">Couldn&apos;t load tail-risk metrics.</div>
            ) : !cvar ? (
              <div className="col-span-2 text-sm text-muted">Loading...</div>
            ) : (
              <>
                <KpiCard
                  label="CVaR 95%"
                  value={fmtUsd(cvar.cvar_95, { signed: true, decimals: 2 })}
                  tone="loss"
                  hint="expected loss in worst 5% of days"
                />
                <KpiCard
                  label="CVaR 99%"
                  value={fmtUsd(cvar.cvar_99, { signed: true, decimals: 2 })}
                  tone="loss"
                  hint="expected loss in worst 1% of days"
                />
                <KpiCard
                  label="VaR 95%"
                  value={fmtPct(cvar.var_95, { signed: true, decimals: 2 })}
                  hint="loss threshold at 5% confidence"
                />
                <KpiCard
                  label="VaR 99%"
                  value={fmtPct(cvar.var_99, { signed: true, decimals: 2 })}
                  hint="loss threshold at 1% confidence"
                />
              </>
            )}
          </div>
        </section>
      </div>

      {/* Correlation */}
      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Asset correlation</span>
          {corr && (
            <span
              className={clsx(
                "text-xs",
                corr.well_diversified ? "text-gain" : "text-gold",
              )}
            >
              Avg {corr.avg_correlation.toFixed(2)} ·{" "}
              {corr.well_diversified ? "diversified" : "concentrated"}
            </span>
          )}
        </div>
        <div className="p-4">
          {corrErr ? (
            <div className="text-sm text-loss">Couldn&apos;t load correlation matrix.</div>
          ) : !corr || corr.assets.length < 2 ? (
            <div className="text-sm text-muted">
              Need at least 2 distinct assets to compute correlation.
            </div>
          ) : (
            <CorrelationMatrix assets={corr.assets} matrix={corr.matrix} />
          )}
        </div>
      </section>
    </div>
  );
}

function CorrelationMatrix({
  assets,
  matrix,
}: {
  assets: string[];
  matrix: number[][];
}) {
  return (
    <div className="overflow-x-auto scrollbar-thin">
      <table className="text-xs">
        <thead>
          <tr>
            <th></th>
            {assets.map((a) => (
              <th key={a} className="px-2 py-1 text-left text-muted">
                {a}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {assets.map((rowAsset, i) => (
            <tr key={rowAsset}>
              <td className="pr-2 text-right text-muted">{rowAsset}</td>
              {matrix[i]?.map((v, j) => {
                const intensity = Math.abs(v);
                const bg = `rgba(230, 169, 59, ${intensity * 0.6})`;
                return (
                  <td
                    key={j}
                    className="px-2 py-1 text-center font-mono num"
                    style={{ background: bg }}
                    title={`${rowAsset} ↔ ${assets[j]}: ${v.toFixed(3)}`}
                  >
                    {v.toFixed(2)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
