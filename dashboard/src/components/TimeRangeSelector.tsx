"use client";

/**
 * TimeRangeSelector -- Sprint 59
 *
 * Six compact chips (1D / 5D / 1M / 3M / 1Y / ALL) that the user
 * clicks to change the chart's zoom level. Used both inline (above
 * the small chart card) and inside the fullscreen ChartModal.
 *
 * The component is purely presentational -- it doesn't fetch data
 * itself; the parent owns the selected range state and re-runs
 * `api.candlesRange(asset, range)` when the selection changes.
 *
 * Visual: a single row of uppercase chips. Active chip is filled
 * with the brand gold tone; the rest are muted text on a dark
 * chip. Touch target >= 28px (h-7) so it works on tablets.
 */

import { clsx } from "clsx";
import type { TimeRange } from "@/lib/types";

export const TIME_RANGES: TimeRange[] = ["1D", "5D", "1M", "3M", "1Y", "ALL"];

interface TimeRangeSelectorProps {
  value: TimeRange;
  onChange: (range: TimeRange) => void;
  /** "compact" = tiny chips for inside a chart card (default),
   *  "default" = bigger chips for inside the fullscreen modal. */
  size?: "compact" | "default";
  /** When true, disables all chips (e.g. while loading). */
  disabled?: boolean;
}

export function TimeRangeSelector({
  value,
  onChange,
  size = "compact",
  disabled = false,
}: TimeRangeSelectorProps) {
  const sizing =
    size === "compact"
      ? "h-6 px-1.5 text-[10px]"
      : "h-8 px-3 text-xs";
  return (
    <div className="inline-flex items-center gap-1 rounded-md border border-ink-700 bg-ink-900/40 p-0.5">
      {TIME_RANGES.map((r) => {
        const active = r === value;
        return (
          <button
            key={r}
            type="button"
            disabled={disabled}
            onClick={() => onChange(r)}
            className={clsx(
              "rounded font-mono uppercase tracking-wider transition",
              sizing,
              active
                ? "bg-gold/20 text-gold"
                : "text-muted hover:bg-ink-800 hover:text-cream-50",
              disabled && "opacity-40 cursor-not-allowed",
            )}
            aria-pressed={active}
          >
            {r}
          </button>
        );
      })}
    </div>
  );
}
