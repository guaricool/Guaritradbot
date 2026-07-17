"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Accumulates the last `maxPoints` values of a live-refreshing number into
 * a small client-side rolling window, for a corner sparkline on hero KPI
 * cards. This is genuinely-live context (not decoration pretending to be
 * live): the source values come from the ~1s SSE position snapshots (see
 * server.py's _position_snapshot_loop), so each push reflects a real new
 * backend sample rather than a synthetic tick.
 *
 * Intentionally in-memory only (a ref, no localStorage) -- it's meant to
 * show "how has this been trending in this session", not a durable
 * historical record; that's what EquityChart's real 30-day series is for.
 */
export function useRollingHistory(
  value: number | null | undefined,
  maxPoints = 40,
): number[] {
  const [history, setHistory] = useState<number[]>(() =>
    value !== null && value !== undefined && !Number.isNaN(value) ? [value] : [],
  );
  const lastRef = useRef<number | null>(null);

  useEffect(() => {
    if (value === null || value === undefined || Number.isNaN(value)) return;
    if (lastRef.current === value) return;
    lastRef.current = value;
    setHistory((prev) => {
      const next = [...prev, value];
      return next.length > maxPoints ? next.slice(next.length - maxPoints) : next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, maxPoints]);

  return history;
}
