"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import { fmtTimestamp } from "@/lib/format";
import { Spinner } from "./Spinner";

const EVENT_BADGES: Record<string, string> = {
  TRADE_APPROVED: "bg-gain/15 text-gain",
  TRADE_REJECTED: "bg-loss/15 text-loss",
  TRADE_CLOSED: "bg-muted/15 text-muted",
  POSITION_REPLACED: "bg-gold/15 text-gold",
  POSITION_OPENED: "bg-gain/15 text-gain",
  HYPOTHESIS_GENERATED: "bg-cream-50/10 text-cream-50",
  RISK_BLOCKED: "bg-loss/15 text-loss",
  MODE_CHANGED: "bg-gold/15 text-gold",
  SYSTEM_ERROR: "bg-loss/20 text-loss",
  MANUAL_CLOSE: "bg-gold/15 text-gold",
  KILL_SWITCH: "bg-loss/25 text-loss",
};

function badge(eventType: string) {
  return EVENT_BADGES[eventType] ?? "bg-muted/15 text-muted";
}

export function AuditTable({ initialLimit = 100 }: { initialLimit?: number }) {
  const [limit, setLimit] = useState(initialLimit);
  const [filter, setFilter] = useState<string>("");
  const { data, error, isLoading } = useSWR(
    ["audit", limit],
    () => api.audit(limit),
    { refreshInterval: 5_000 },
  );

  const rows = useMemo(() => {
    if (!data) return [];
    if (!filter.trim()) return data;
    const f = filter.toLowerCase();
    return data.filter(
      (e) =>
        e.event_type.toLowerCase().includes(f) ||
        JSON.stringify(e.payload).toLowerCase().includes(f),
    );
  }, [data, filter]);

  if (error) {
    return (
      <div className="rounded border border-loss/30 bg-loss/10 p-3 text-sm text-loss">
        Failed to load audit log: {String(((error as { message?: string })?.message) ?? error)}
      </div>
    );
  }

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 border-b border-ink-700 px-4 py-3">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by event type or payload text..."
          className="input max-w-md text-xs"
        />
        <div className="ml-auto flex items-center gap-2 text-xs text-muted">
          <label htmlFor="audit-limit" className="text-[11px] uppercase tracking-wider">
            Limit
          </label>
          <select
            id="audit-limit"
            value={limit}
            onChange={(e) => setLimit(parseInt(e.target.value))}
            className="input w-auto py-1 text-xs"
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={250}>250</option>
            <option value={1000}>1000</option>
          </select>
        </div>
      </div>

      {isLoading ? (
        <div className="flex h-40 items-center justify-center text-muted">
          <Spinner className="h-5 w-5" />
        </div>
      ) : !rows.length ? (
        <div className="flex h-40 flex-col items-center justify-center text-muted">
          <span className="text-2xl">∅</span>
          <span className="text-sm">No events match.</span>
        </div>
      ) : (
        <div className="max-h-[calc(100vh-260px)] overflow-y-auto scrollbar-thin">
          <table className="table-zebra">
            <thead className="sticky top-0 z-10 bg-ink-900">
              <tr>
                <th className="w-44">Time</th>
                <th className="w-44">Event</th>
                <th>Payload</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e, i) => (
                <tr key={`${e.ts}-${i}`} className="animate-fade-in">
                  <td className="font-mono text-[11px] text-muted">
                    {fmtTimestamp(e.ts)}
                  </td>
                  <td>
                    <span
                      className={`inline-block rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${badge(e.event_type)}`}
                    >
                      {e.event_type}
                    </span>
                  </td>
                  <td>
                    <code className="block max-w-2xl truncate font-mono text-[11px] text-cream-50/80">
                      {JSON.stringify(e.payload)}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
