"use client";

import { useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import { Spinner } from "./Spinner";

// Sprint 46H — Carlos: "en el dashboard hay manera de tener como un
// stop y un start? para que mientras esté en paper se puedan detener
// las entradas que están abiertas, y así quede la sesión completamente
// limpia... a la hora de pasarlo a live el sistema pueda correr
// limpio." This toggle pauses/resumes NEW entries only — the bot's
// PositionMonitor keeps protecting (SL/TP) any position already open
// regardless of this switch. It's intentionally separate from the
// LIVE/PAPER ModeToggle and from the filesystem KillSwitch (see
// src/api/state.py::read_trading_pause's docstring for the full
// rationale) — a softer, always-safe-to-flip control.
export function TradingPauseToggle({ size = "md" }: { size?: "sm" | "md" }) {
  const { data, mutate } = useSWR("trading-pause", () => api.getTradingPause(), {
    refreshInterval: 15_000,
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function toggle() {
    if (!data) return;
    const next = !data.paused;
    setErr(null);
    setBusy(true);
    try {
      const res = await api.setTradingPause(next, "dashboard");
      await mutate(res, { revalidate: false });
    } catch (e: unknown) {
      const error = e as { message?: string };
      setErr(error?.message || "Toggle failed");
    } finally {
      setBusy(false);
    }
  }

  const paused = data?.paused ?? false;
  const sizeClass = size === "sm" ? "px-2.5 py-1 text-[11px]" : "px-3 py-1.5 text-xs";

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={toggle}
        disabled={busy || !data}
        className={`group inline-flex items-center gap-2 rounded-full border font-medium uppercase tracking-wider transition ${sizeClass} ${
          paused
            ? "border-loss/50 bg-loss/15 text-loss hover:border-loss hover:bg-loss/25"
            : "border-gain/40 bg-gain/10 text-gain hover:border-gain hover:bg-gain/20"
        }`}
        title={
          paused && data?.paused_at
            ? `Paused since ${new Date(data.paused_at * 1000).toLocaleString()}${
                data.paused_by ? ` by ${data.paused_by}` : ""
              } — new entries skipped, SL/TP still active`
            : "Bot is generating new entries normally"
        }
      >
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            paused ? "bg-loss" : "bg-gain shadow-[0_0_6px_currentColor]"
          }`}
        />
        {busy ? <Spinner /> : paused ? "STOPPED" : "RUNNING"}
      </button>
      {err && <span className="text-[10px] text-loss">{err}</span>}
    </div>
  );
}
