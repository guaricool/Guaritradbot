"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import { PositionTable } from "@/components/PositionTable";
import { TableSkeleton } from "@/components/Skeleton";
import { useLive } from "@/lib/use-live";
import { useEffect, useMemo, useState } from "react";
import type { PositionSummary } from "@/lib/types";

export default function PositionsPage() {
  const { data, error, isLoading, mutate } = useSWR(
    "positions-list",
    () => api.positions(),
    { refreshInterval: 10_000 },
  );
  const [livePositions, setLivePositions] = useState<PositionSummary[] | null>(null);
  const { status } = useLive({
    onMessage: (msg) => {
      if (msg.type === "positions") setLivePositions(msg.positions);
    },
  });
  useEffect(() => {
    if (status !== "open") setLivePositions(null);
  }, [status]);

  const positions = useMemo<PositionSummary[]>(
    () => livePositions ?? data ?? [],
    [livePositions, data],
  );

  return (
    <div className="space-y-5 animate-fade-in">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">
            Positions
          </h1>
          <p className="text-sm text-muted">
            All open positions, live P&amp;L, and one-click close.
          </p>
        </div>
        <button
          onClick={() => mutate()}
          className="btn-ghost text-xs text-muted"
        >
          ↻ Refresh
        </button>
      </header>

      <section className="card overflow-hidden">
        <div className="p-4">
          {isLoading && !data ? (
            <TableSkeleton rows={4} />
          ) : error ? (
            <div className="rounded border border-loss/30 bg-loss/10 p-3 text-sm text-loss">
              Can&apos;t reach the bot&apos;s API — check it&apos;s running, then
              refresh.
            </div>
          ) : (
            <PositionTable
              positions={positions}
              emptyHint="Flat — no open positions."
            />
          )}
        </div>
      </section>
    </div>
  );
}
