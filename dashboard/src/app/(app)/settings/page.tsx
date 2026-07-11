"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import { KpiCard } from "@/components/KpiCard";
import { PageSpinner } from "@/components/Spinner";
import type { TradingConfig } from "@/lib/types";

// Sprint 46C: read-only view of config.yaml's `trading:` section.
// Carlos asked, specifically: "donde se puedan ver todas las opciones de
// cuantas operaciones quieres abrir simultaneamente [y] cuanto quieres
// arriesgar, y aparte sabiendo que lo minimo para una entrada es $10"
// — i.e. a page to SEE (not necessarily edit yet) max simultaneous
// trades, risk per trade, and the $10 minimum order size.
//
// This is intentionally display-only for now: config.yaml still has to
// be edited + the bot restarted to change these values. A future sprint
// could add a POST /api/config to make this editable from here.
export default function SettingsPage() {
  const { data, error } = useSWR<TradingConfig>("trading-config", () => api.config());

  if (!data && !error) return <PageSpinner />;

  return (
    <div className="space-y-5 animate-fade-in">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">
          Trading Settings
        </h1>
        <p className="text-sm text-muted">
          Current values from <code className="text-gold">config.yaml</code>{" "}
          (the running bot&apos;s actual configuration). Read-only — edit
          config.yaml and restart the bot to change these.
        </p>
      </header>

      {error ? (
        <div className="rounded border border-loss/30 bg-loss/10 p-4 text-sm text-loss">
          Failed to load trading config: {String((error as { message?: string })?.message ?? error)}
        </div>
      ) : (
        <>
          {/* Position sizing & limits */}
          <section className="card overflow-hidden">
            <div className="card-header">
              <span>Position sizing &amp; limits</span>
            </div>
            <div className="grid grid-cols-2 gap-3 p-4 md:grid-cols-4">
              <KpiCard
                label="Max simultaneous trades"
                value={data!.max_open_trades}
                icon={<span>◉</span>}
                hint="how many positions can be open at once"
              />
              <KpiCard
                label="Risk per trade"
                value={`${data!.risk_per_trade_pct.toFixed(2)}%`}
                tone="gold"
                hint="of account equity risked per position"
              />
              <KpiCard
                label="Minimum order size"
                value={`$${data!.min_order_usd.toFixed(2)}`}
                hint="smallest entry the bot will place"
              />
              <KpiCard
                label="Max capital per trade"
                value={`${data!.max_capital_per_trade_pct.toFixed(1)}%`}
                hint="cap on notional per single position"
              />
            </div>
          </section>

          {/* Stops, targets & replacement */}
          <section className="card overflow-hidden">
            <div className="card-header">
              <span>Stops, targets &amp; replacement</span>
            </div>
            <div className="grid grid-cols-2 gap-3 p-4 md:grid-cols-4">
              <KpiCard
                label="ATR stop multiplier"
                value={data!.atr_stop_multiplier.toFixed(2)}
                hint="stop-loss distance = ATR × this"
              />
              <KpiCard
                label="ATR take-profit multiplier"
                value={data!.atr_take_profit_multiplier.toFixed(2)}
                hint="take-profit distance = ATR × this"
              />
              <KpiCard
                label="Risk:Reward ratio"
                value={`1 : ${data!.risk_reward_ratio.toFixed(1)}`}
                hint="target reward per unit of risk"
              />
              <KpiCard
                label="Min profit to protect"
                value={`$${data!.min_profit_to_protect.toFixed(2)}`}
                hint="floor before trailing protection kicks in"
              />
            </div>
            <div className="border-t border-ink-700 p-4">
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted">Position replacement</span>
                <span
                  className={
                    data!.enable_position_replacement ? "text-gain" : "text-muted"
                  }
                >
                  {data!.enable_position_replacement ? "Enabled" : "Disabled"}
                  {data!.enable_position_replacement &&
                    ` · score threshold ${data!.replacement_score_threshold.toFixed(2)}`}
                </span>
              </div>
              <p className="mt-1 text-[11px] text-muted">
                When enabled, a new higher-scoring signal can replace a
                weaker open position once max simultaneous trades is
                reached.
              </p>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
