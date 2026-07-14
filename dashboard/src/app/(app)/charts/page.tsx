"use client";

/**
 * /charts — Sprint 58: live candlestick line charts for every
 * trading asset in the bot's universe. One card per asset, all
 * rendered side-by-side. Auto-refreshes every 15s via swr.
 *
 * Backed by the new /api/candles endpoint (Sprint 58) — no
 * position_id is required, so this works for assets that don't
 * have an open position. We use the `api` helper from lib/api.ts
 * (not a raw `fetch()`) so the URL is prefixed with
 * NEXT_PUBLIC_API_URL and points at the bot host, not the
 * dashboard host. A relative fetch would 404 against the
 * dashboard's own port-3000 origin.
 */
import useSWR from "swr";
import { api } from "@/lib/api";
import type { CandlesResponse } from "@/lib/types";
import { PriceChart } from "@/components/PriceChart";

const REFRESH_MS = 15_000; // 15s feels live without hammering yfinance

const ASSETS = [
  // Crypto (24/7) — first three in the bot's operational universe
  { ticker: "BTC-USD", label: "Bitcoin",  kind: "crypto" as const },
  { ticker: "ETH-USD", label: "Ethereum", kind: "crypto" as const },
  { ticker: "SOL-USD", label: "Solana",   kind: "crypto" as const },
  // Equities (US market hours) — only show if data is fresh;
  // otherwise the PriceChart still renders the last close
  { ticker: "SPY",     label: "S&P 500 ETF",      kind: "equity" as const },
  { ticker: "QQQ",     label: "Nasdaq-100 ETF",   kind: "equity" as const },
  { ticker: "GLD",     label: "Gold ETF",         kind: "equity" as const },
  { ticker: "USO",     label: "Oil ETF",          kind: "equity" as const },
];

export default function ChartsPage() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold text-cream-50">Live Charts</h1>
          <p className="text-xs text-muted">
            1h candles · auto-refresh every 15s · last 100 bars per asset
          </p>
        </div>
        <div className="text-[10px] uppercase tracking-wider text-muted">
          Sprint 58
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {ASSETS.map((a) => (
          <AssetChart key={a.ticker} ticker={a.ticker} label={a.label} kind={a.kind} />
        ))}
      </div>
    </div>
  );
}

function AssetChart({ ticker, label, kind }: { ticker: string; label: string; kind: "crypto" | "equity" }) {
  const { data, error, isLoading } = useSWR<CandlesResponse>(
    // Tuple key: swr dedupes per (ticker, interval, limit). The
    // fetcher ignores the first element (the literal "candles"
    // tag) and calls api.candles with the rest. This routes
    // through lib/api.ts → NEXT_PUBLIC_API_URL → bot host.
    ["candles", ticker, "1h", 100],
    ([, ticker, interval, limit]) => api.candles(ticker as string, interval as string, limit as number),
    { refreshInterval: REFRESH_MS, revalidateOnFocus: false },
  );
  return (
    <div className="flex flex-col">
      {/* Small kind tag at the top of each card — visually
          distinguishes crypto (24/7) from equity (US market hours)
          and ensures `kind` is used (ESLint no-unused-vars). */}
      <div className="mb-1 px-1 text-[10px] uppercase tracking-wider text-muted">
        {kind}
      </div>
      {isLoading && !data ? (
        <ChartSkeleton label={label} />
      ) : error ? (
        <ChartError label={label} message={(error as Error).message} />
      ) : (
        <PriceChart asset={label} candles={data?.candles ?? []} />
      )}
    </div>
  );
}

function ChartSkeleton({ label }: { label: string }) {
  return (
    <div className="flex h-full flex-col rounded-lg border border-ink-700 bg-ink-900/50 p-3">
      <div className="mb-2 flex items-baseline justify-between">
        <div>
          <div className="text-sm font-semibold text-cream-50">{label}</div>
          <div className="text-[10px] uppercase tracking-wider text-muted">loading…</div>
        </div>
      </div>
      <div className="h-[180px] animate-pulse rounded bg-ink-800/60" />
    </div>
  );
}

function ChartError({ label, message }: { label: string; message: string }) {
  return (
    <div className="flex h-full flex-col rounded-lg border border-loss/40 bg-loss/5 p-3">
      <div className="text-sm font-semibold text-cream-50">{label}</div>
      <div className="mt-1 text-[10px] text-loss">{message}</div>
    </div>
  );
}
