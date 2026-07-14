"use client";

/**
 * /history — Sprint 58: closed-position history with filters.
 *
 * Backed by /api/positions/history (Sprint 58). Filters:
 *  - Date range (from / to, in the user's local timezone)
 *  - Asset class (crypto | equity)
 *  - Direction (long | short)
 *  - Asset (exact match)
 *
 * The summary block at the top shows total trades, win/loss
 * breakdown, win rate, total P&L (USD), and total fees paid.
 * The table below is sorted newest-first by closed_ts.
 */
import { useMemo, useState } from "react";
import useSWR from "swr";
import { getToken } from "@/lib/api";

interface HistoryRow {
  id: string;
  asset: string;
  asset_class: "crypto" | "equity" | "other";
  direction: "long" | "short";
  entry_price: number;
  entry_ts: number;
  closed_price: number;
  closed_ts: number;
  close_reason: string;
  qty: number;
  notional_usd: number;
  realized_pnl_usd: number;
  fees_paid_usd: number;
  duration_hours: number | null;
  strategy: string;
}

interface HistoryResponse {
  positions: HistoryRow[];
  summary: {
    total_trades: number;
    win_count: number;
    loss_count: number;
    breakeven_count: number;
    win_rate_pct: number;
    total_pnl_usd: number;
    total_fees_usd: number;
  };
}

type AssetClassFilter = "" | "crypto" | "equity";
type DirectionFilter = "" | "long" | "short";

function todayUtcDate(): string {
  return new Date().toISOString().slice(0, 10);
}
function epochDaysAgoUtc(d: number): string {
  const t = Math.floor(Date.now() / 1000) - d * 86400;
  return new Date(t * 1000).toISOString().slice(0, 10);
}

async function historyFetcher(url: string): Promise<HistoryResponse> {
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export default function HistoryPage() {
  // Filters
  const [fromDate, setFromDate] = useState<string>(epochDaysAgoUtc(30));
  const [toDate, setToDate] = useState<string>(todayUtcDate());
  const [assetClass, setAssetClass] = useState<AssetClassFilter>("");
  const [direction, setDirection] = useState<DirectionFilter>("");

  const url = useMemo(() => {
    const q = new URLSearchParams();
    if (fromDate) q.set("from", String(Math.floor(new Date(fromDate).getTime() / 1000)));
    if (toDate) {
      // End of the chosen day (23:59:59 local time, expressed in UTC).
      const end = Math.floor(new Date(`${toDate}T23:59:59`).getTime() / 1000);
      q.set("to", String(end));
    }
    if (assetClass) q.set("asset_class", assetClass);
    if (direction) q.set("direction", direction);
    return `/api/positions/history?${q.toString()}`;
  }, [fromDate, toDate, assetClass, direction]);

  const { data, error, isLoading } = useSWR<HistoryResponse>(url, historyFetcher, {
    refreshInterval: 30_000,
    revalidateOnFocus: false,
  });

  const summary = data?.summary;
  const rows = data?.positions ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold text-cream-50">Trade History</h1>
          <p className="text-xs text-muted">
            Closed positions, newest first. Filter by date, asset class, and direction.
          </p>
        </div>
        <div className="text-[10px] uppercase tracking-wider text-muted">Sprint 58</div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-700 bg-ink-900/50 p-3">
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-muted">From</label>
          <input
            type="date"
            value={fromDate}
            onChange={(e) => setFromDate(e.target.value)}
            className="mt-1 rounded border border-ink-700 bg-ink-950 px-2 py-1 text-xs text-cream-50"
          />
        </div>
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-muted">To</label>
          <input
            type="date"
            value={toDate}
            onChange={(e) => setToDate(e.target.value)}
            className="mt-1 rounded border border-ink-700 bg-ink-950 px-2 py-1 text-xs text-cream-50"
          />
        </div>
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-muted">Class</label>
          <select
            value={assetClass}
            onChange={(e) => setAssetClass(e.target.value as AssetClassFilter)}
            className="mt-1 rounded border border-ink-700 bg-ink-950 px-2 py-1 text-xs text-cream-50"
          >
            <option value="">all</option>
            <option value="crypto">crypto</option>
            <option value="equity">equity</option>
          </select>
        </div>
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-muted">Direction</label>
          <select
            value={direction}
            onChange={(e) => setDirection(e.target.value as DirectionFilter)}
            className="mt-1 rounded border border-ink-700 bg-ink-950 px-2 py-1 text-xs text-cream-50"
          >
            <option value="">all</option>
            <option value="long">long</option>
            <option value="short">short</option>
          </select>
        </div>
        <div className="ml-auto text-[10px] text-muted">
          {isLoading
            ? "loading…"
            : error
              ? `error: ${(error as Error).message}`
              : `${rows.length} rows`}
        </div>
      </div>

      {/* Summary KPIs */}
      {summary && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
          <Kpi label="Trades" value={summary.total_trades.toString()} />
          <Kpi label="Win" value={summary.win_count.toString()} tone="gain" />
          <Kpi label="Loss" value={summary.loss_count.toString()} tone="loss" />
          <Kpi label="BE" value={summary.breakeven_count.toString()} />
          <Kpi label="Win rate" value={`${summary.win_rate_pct.toFixed(1)}%`} />
          <Kpi
            label="Total P&L"
            value={`$${summary.total_pnl_usd.toFixed(2)}`}
            tone={summary.total_pnl_usd >= 0 ? "gain" : "loss"}
          />
          <Kpi
            label="Fees"
            value={`$${summary.total_fees_usd.toFixed(2)}`}
            tone="muted"
          />
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-ink-700">
        <table className="w-full text-left text-xs">
          <thead className="bg-ink-800/60 text-[10px] uppercase tracking-wider text-muted">
            <tr>
              <th className="px-3 py-2">Closed</th>
              <th className="px-3 py-2">Asset</th>
              <th className="px-3 py-2">Dir</th>
              <th className="px-3 py-2 text-right">Entry</th>
              <th className="px-3 py-2 text-right">Close</th>
              <th className="px-3 py-2 text-right">Qty</th>
              <th className="px-3 py-2 text-right">Notional</th>
              <th className="px-3 py-2 text-right">P&L</th>
              <th className="px-3 py-2 text-right">Fees</th>
              <th className="px-3 py-2">Reason</th>
              <th className="px-3 py-2">Strategy</th>
              <th className="px-3 py-2 text-right">Duration</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-800">
            {rows.length === 0 && !isLoading && (
              <tr>
                <td colSpan={12} className="px-3 py-6 text-center text-muted">
                  No closed positions match the current filters.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <tr key={row.id} className="hover:bg-ink-900/40">
                <td className="px-3 py-2 font-mono text-cream-50">
                  {formatDateTime(row.closed_ts)}
                </td>
                <td className="px-3 py-2">
                  <span className="text-cream-50">{row.asset}</span>{" "}
                  <span className="text-[10px] uppercase tracking-wider text-muted">
                    {row.asset_class}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <span
                    className={
                      row.direction === "long"
                        ? "text-gain"
                        : "text-loss"
                    }
                  >
                    {row.direction}
                  </span>
                </td>
                <td className="px-3 py-2 text-right font-mono text-cream-50">
                  ${row.entry_price.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right font-mono text-cream-50">
                  ${row.closed_price.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right font-mono text-muted">
                  {row.qty}
                </td>
                <td className="px-3 py-2 text-right font-mono text-muted">
                  ${row.notional_usd.toFixed(2)}
                </td>
                <td
                  className={
                    "px-3 py-2 text-right font-mono " +
                    (row.realized_pnl_usd >= 0 ? "text-gain" : "text-loss")
                  }
                >
                  {row.realized_pnl_usd >= 0 ? "+" : ""}
                  ${row.realized_pnl_usd.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right font-mono text-muted">
                  ${row.fees_paid_usd.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-muted">{row.close_reason}</td>
                <td className="px-3 py-2 text-muted">{row.strategy}</td>
                <td className="px-3 py-2 text-right text-muted">
                  {row.duration_hours != null ? `${row.duration_hours.toFixed(1)}h` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Kpi({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "gain" | "loss" | "muted";
}) {
  const color =
    tone === "gain"
      ? "text-gain"
      : tone === "loss"
        ? "text-loss"
        : tone === "muted"
          ? "text-muted"
          : "text-cream-50";
  return (
    <div className="rounded-lg border border-ink-700 bg-ink-900/50 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-sm ${color}`}>{value}</div>
    </div>
  );
}

function formatDateTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
