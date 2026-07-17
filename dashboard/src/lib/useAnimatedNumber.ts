"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Smoothly tweens a displayed number toward `target` whenever it
 * changes, instead of jumping straight to the new value. The backend
 * now pushes a fresh P&L snapshot every ~1s (see server.py's
 * _position_snapshot_loop) with live broker-ticker prices behind it
 * (state.py::_fetch_one_price) -- without this, the UI would still
 * just SNAP between those 1s values, which reads as "ticking" rather
 * than the continuous live movement Carlos asked for. Tweening each
 * step over the same ~1s window makes the number visibly glide
 * up/down between real updates.
 *
 * Returns `null` while `target` is null (nothing to animate toward).
 */
export function useAnimatedNumber(
  target: number | null | undefined,
  durationMs = 900,
): number | null {
  const [value, setValue] = useState<number | null>(target ?? null);
  const fromRef = useRef<number | null>(target ?? null);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (target === null || target === undefined || Number.isNaN(target)) {
      setValue(null);
      fromRef.current = null;
      return;
    }
    const from = fromRef.current ?? target;
    const to = target;
    if (from === to) return;

    const start = performance.now();
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);

    function tick(now: number) {
      const elapsed = now - start;
      const t = Math.min(1, elapsed / durationMs);
      // ease-out cubic -- fast start, settles gently instead of a
      // linear ramp that would feel mechanical for a P&L readout.
      const eased = 1 - Math.pow(1 - t, 3);
      const current = from + (to - from) * eased;
      setValue(current);
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = to;
      }
    }
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, durationMs]);

  return value;
}
