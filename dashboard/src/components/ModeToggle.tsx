"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { Spinner } from "./Spinner";
import type { ModeInfo } from "@/lib/types";

export function ModeToggle({
  mode,
  size = "md",
}: {
  mode: ModeInfo;
  size?: "sm" | "md";
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function toggle() {
    const next = mode.mode === "live" ? "paper" : "live";
    const verb = next === "live" ? "go LIVE" : "switch to paper";
    if (next === "live" && !confirm(`Arm the bot to ${verb}? Real orders will be placed.`)) {
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      await api.setMode(next, "dashboard");
      router.refresh();
    } catch (e: unknown) {
      const err = e as { message?: string };
      setErr(err?.message || "Toggle failed");
    } finally {
      setBusy(false);
    }
  }

  const isLive = mode.mode === "live";
  const sizeClass =
    size === "sm" ? "px-2.5 py-1 text-[11px]" : "px-3 py-1.5 text-xs";

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        onClick={toggle}
        disabled={busy}
        className={`group inline-flex items-center gap-2 rounded-full border font-medium uppercase tracking-wider transition ${sizeClass} ${
          isLive
            ? "border-gain/50 bg-gain/15 text-gain hover:border-gain hover:bg-gain/25"
            : "border-muted/40 bg-muted/10 text-muted hover:border-muted hover:text-cream-50"
        }`}
        title={
          mode.switched_at
            ? `Last toggle: ${new Date(mode.switched_at * 1000).toLocaleString()} by ${mode.switched_by ?? "?"}`
            : "Bot default mode"
        }
      >
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            isLive ? "bg-gain shadow-[0_0_6px_currentColor]" : "bg-muted"
          }`}
        />
        {busy ? <Spinner /> : isLive ? "LIVE" : "PAPER"}
      </button>
      {err && <span className="text-[10px] text-loss">{err}</span>}
    </div>
  );
}
