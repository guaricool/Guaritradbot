"use client";

import { useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import { Spinner } from "./Spinner";

// Carlos: "aunque sea ganar poco pero con muchas entradas" -- an A/B
// toggle between the normal swing profile (bigger positions, full
// ATR-based take-profit) and scalp mode (smaller positions, tighter
// take-profit, more sensitive SMART_PROFIT_TAKE reversal-exit) so the
// two approaches can be compared in paper without a restart. Paper-only
// by construction -- RiskManagerAgent/BotRuntime both gate their
// scalp_overrides on "not live", so this is a no-op while the bot is
// armed for live trading.
export function ScalpModeToggle({ size = "md" }: { size?: "sm" | "md" }) {
  const { data, mutate } = useSWR("scalp-mode", () => api.getScalpMode(), {
    refreshInterval: 15_000,
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function toggle() {
    if (!data) return;
    const next = !data.scalp_mode_enabled;
    setErr(null);
    setBusy(true);
    try {
      const res = await api.setScalpMode(next, "dashboard");
      await mutate(res.scalp_mode, { revalidate: false });
    } catch (e: unknown) {
      const error = e as { message?: string };
      setErr(error?.message || "Toggle failed");
    } finally {
      setBusy(false);
    }
  }

  const scalping = data?.scalp_mode_enabled ?? false;
  const sizeClass = size === "sm" ? "px-2.5 py-1 text-[11px]" : "px-3 py-1.5 text-xs";

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={toggle}
        disabled={busy || !data}
        className={`group inline-flex items-center gap-2 rounded-full border font-medium uppercase tracking-wider transition ${sizeClass} ${
          scalping
            ? "border-gold/50 bg-gold/15 text-gold hover:border-gold hover:bg-gold/25"
            : "border-muted/40 bg-muted/10 text-muted hover:border-muted hover:text-cream-50"
        }`}
        title={
          scalping && data?.switched_at
            ? `Scalp mode since ${new Date(data.switched_at * 1000).toLocaleString()}${
                data.switched_by ? ` by ${data.switched_by}` : ""
              } — tighter TP, smaller positions, more frequent small wins`
            : "Swing profile: normal position size and take-profit"
        }
      >
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            scalping ? "bg-gold shadow-[0_0_6px_currentColor]" : "bg-muted"
          }`}
        />
        {busy ? <Spinner /> : scalping ? "SCALPING" : "SWING"}
      </button>
      {err && <span className="text-[10px] text-loss">{err}</span>}
    </div>
  );
}
