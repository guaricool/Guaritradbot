"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Spinner } from "./Spinner";

export function LoginForm() {
  const { login } = useAuth();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(password);
    } catch (err: unknown) {
      const e = err as { status?: number; message?: string };
      setError(
        e?.status === 401
          ? "Wrong password."
          : e?.message || "Login failed. Is the bot API reachable?",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <div className="absolute inset-0 -z-10 bg-[radial-gradient(circle_at_30%_20%,rgba(230,169,59,0.08),transparent_50%),radial-gradient(circle_at_70%_80%,rgba(16,185,129,0.05),transparent_50%)]" />
      <form
        onSubmit={onSubmit}
        className="card w-full max-w-sm animate-fade-in p-6 shadow-2xl"
      >
        <div className="mb-6 flex flex-col items-center text-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-gold/15 text-gold shadow-[0_0_24px_rgba(230,169,59,0.2)]">
            <span className="text-2xl font-bold">G</span>
          </div>
          <h1 className="font-display text-xl font-semibold tracking-tight">
            Guaritradbot
          </h1>
          <p className="mt-1 text-xs uppercase tracking-wider text-muted">
            Trading desk — sign in
          </p>
        </div>

        <label className="block">
          <span className="kpi-label mb-1.5 block">Dashboard password</span>
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
            className="input font-mono"
            placeholder="••••••••"
          />
        </label>

        {error && (
          <div className="mt-3 rounded-lg border border-loss/30 bg-loss/10 px-3 py-2 text-xs text-loss">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={busy || !password}
          className="btn-primary mt-4 w-full"
        >
          {busy ? (
            <>
              <Spinner /> Signing in...
            </>
          ) : (
            "Sign in"
          )}
        </button>

        <p className="mt-4 text-center text-[10px] text-muted">
          The same password unlocks the bot&apos;s HTTP API (DASHBOARD_PASSWORD env).
        </p>
      </form>
    </div>
  );
}
