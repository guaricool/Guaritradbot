"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { api, ApiError } from "@/lib/api";
import { PageSpinner } from "@/components/Spinner";
import { clsx } from "clsx";
import { fmtTimestamp } from "@/lib/format";
import type {
  RiskConfig,
  RiskConfigResponse,
  TradingConfig,
  TradingConfigResponse,
} from "@/lib/types";

// Sprint 46D: fully editable trading settings. Carlos asked, verbatim:
// "todo debe ser modificable desde la interfaz del dashboard" (everything
// should be editable from the dashboard) and specifically wanted to see/
// set max simultaneous trades, risk per trade, and the $10 minimum order
// size. This form saves via POST /api/config, which writes to a JSON
// override file (never touches config.yaml's comments/formatting) — see
// src/api/state.py for why. The bot only re-reads trading config at
// startup, so a save alone isn't enough; the page also offers a
// "Restart now" button (POST /api/restart) to apply it immediately.

type FormState = {
  risk_per_trade_pct: string;
  max_open_trades: string;
  min_order_usd: string;
  max_capital_per_trade_pct: string;
  atr_stop_multiplier: string;
  atr_take_profit_multiplier: string;
  risk_reward_ratio: string;
  enable_position_replacement: boolean;
  replacement_score_threshold: string;
  min_profit_to_protect: string;
};

function toFormState(cfg: TradingConfig): FormState {
  return {
    risk_per_trade_pct: String(cfg.risk_per_trade_pct),
    max_open_trades: String(cfg.max_open_trades),
    min_order_usd: String(cfg.min_order_usd),
    max_capital_per_trade_pct: String(cfg.max_capital_per_trade_pct),
    atr_stop_multiplier: String(cfg.atr_stop_multiplier),
    atr_take_profit_multiplier: String(cfg.atr_take_profit_multiplier),
    risk_reward_ratio: String(cfg.risk_reward_ratio),
    enable_position_replacement: cfg.enable_position_replacement,
    replacement_score_threshold: String(cfg.replacement_score_threshold),
    min_profit_to_protect: String(cfg.min_profit_to_protect),
  };
}

// Field metadata: label, hint, and the same bounds the backend enforces
// (UpdateTradingConfigRequest in src/api/server.py) so the user gets
// instant feedback instead of a round-trip 422.
const NUMERIC_FIELDS: Array<{
  key: keyof Omit<FormState, "enable_position_replacement">;
  label: string;
  hint: string;
  min?: number;
  max?: number;
  step?: string;
  suffix?: string;
}> = [
  {
    key: "max_open_trades",
    label: "Max simultaneous trades",
    hint: "how many positions can be open at once",
    min: 1,
    max: 50,
    step: "1",
  },
  {
    key: "risk_per_trade_pct",
    label: "Risk per trade (%)",
    hint: "of account equity risked per position",
    min: 0.01,
    max: 100,
    step: "0.1",
  },
  {
    key: "min_order_usd",
    label: "Minimum order size ($)",
    hint: "Binance.US's real exchange minimum is $10 — this can't be set lower",
    min: 10,
    step: "0.5",
  },
  {
    key: "max_capital_per_trade_pct",
    label: "Max capital per trade (%)",
    hint: "cap on notional per single position",
    min: 0.01,
    max: 100,
    step: "1",
  },
  {
    key: "atr_stop_multiplier",
    label: "ATR stop multiplier",
    hint: "stop-loss distance = ATR × this",
    min: 0.1,
    step: "0.1",
  },
  {
    key: "atr_take_profit_multiplier",
    label: "ATR take-profit multiplier",
    hint: "take-profit distance = ATR × this",
    min: 0.1,
    step: "0.1",
  },
  {
    key: "risk_reward_ratio",
    label: "Risk:Reward ratio",
    hint: "target reward per unit of risk (informational)",
    min: 0.1,
    step: "0.1",
  },
  {
    key: "replacement_score_threshold",
    label: "Replacement score threshold",
    hint: "edge required over the worst open position to replace it (0-1)",
    min: 0,
    max: 1,
    step: "0.01",
  },
  {
    key: "min_profit_to_protect",
    label: "Min profit to protect ($)",
    hint: "floor before trailing protection kicks in",
    min: 0,
    step: "0.5",
  },
];

// Sprint 46F: risk/mandate safety gates. These existed as
// RiskManagerAgent/DrawdownKillSwitch constructor params since Sprint
// 30/44/45 but main.py never read them from config.yaml at all — a
// truly dead settings surface. Same override-file/restart-required
// pattern as trading config above; see src/api/state.py's
// read_risk_config/write_risk_config and the Sprint 46F block near the
// top of main.py.
type RiskFormState = {
  drawdown_kill_threshold_pct: string;
  drawdown_cooldown_hours: string;
  max_asset_class_concentration_pct: string;
  max_avg_correlation_pct: string;
  max_cvar_95_pct: string;
  max_stress_drawdown_pct: string;
  mandate_allowed_symbols: string; // comma-separated in the UI
  max_daily_trades: string; // Sprint 46J: 0 = unlimited
};

function toRiskFormState(cfg: RiskConfig): RiskFormState {
  return {
    drawdown_kill_threshold_pct: String(cfg.drawdown_kill_threshold_pct),
    drawdown_cooldown_hours: String(cfg.drawdown_cooldown_hours),
    max_asset_class_concentration_pct: String(cfg.max_asset_class_concentration_pct),
    max_avg_correlation_pct: String(cfg.max_avg_correlation_pct),
    max_cvar_95_pct: String(cfg.max_cvar_95_pct),
    max_stress_drawdown_pct: String(cfg.max_stress_drawdown_pct),
    mandate_allowed_symbols: cfg.mandate_allowed_symbols.join(", "),
    max_daily_trades: String(cfg.max_daily_trades),
  };
}

const RISK_NUMERIC_FIELDS: Array<{
  key: keyof Omit<RiskFormState, "mandate_allowed_symbols">;
  label: string;
  hint: string;
  min?: number;
  max?: number;
  step?: string;
}> = [
  {
    key: "drawdown_kill_threshold_pct",
    label: "Drawdown kill-switch threshold (%)",
    hint: "pause new entries once equity falls this far from its peak",
    min: 1,
    max: 100,
    step: "0.5",
  },
  {
    key: "drawdown_cooldown_hours",
    label: "Drawdown cooldown (hours)",
    hint: "how long new entries stay paused after the kill switch trips",
    min: 0.5,
    max: 168,
    step: "0.5",
  },
  {
    key: "max_asset_class_concentration_pct",
    label: "Max asset-class concentration (%)",
    hint: "cap on portfolio share in a single asset class (crypto/equity/etc.)",
    min: 1,
    max: 100,
    step: "1",
  },
  {
    key: "max_avg_correlation_pct",
    label: "Max average correlation (%)",
    hint: "flags the book as poorly diversified above this",
    min: 1,
    max: 100,
    step: "1",
  },
  {
    key: "max_cvar_95_pct",
    label: "Max CVaR 95 (%)",
    hint: "tail-risk cap: expected loss in the worst 5% of scenarios",
    min: 1,
    max: 100,
    step: "1",
  },
  {
    key: "max_stress_drawdown_pct",
    label: "Max stress-test drawdown (%)",
    hint: "cap on modeled portfolio impact under stress scenarios",
    min: 1,
    max: 100,
    step: "1",
  },
  {
    key: "max_daily_trades",
    label: "Max new entries per day",
    hint: "rolling 24h rate limit on NEW positions opened (0 = unlimited). Separate from max open trades.",
    min: 0,
    max: 200,
    step: "1",
  },
];

function RiskSettingsSection() {
  const { data, error, mutate } = useSWR<RiskConfigResponse>(
    "risk-config",
    () => api.riskConfig(),
  );
  const [form, setForm] = useState<RiskFormState | null>(null);
  const [saving, setSaving] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveNote, setSaveNote] = useState<string | null>(null);

  useEffect(() => {
    if (data && form === null) {
      setForm(toRiskFormState(data));
    }
  }, [data, form]);

  if (!data && !error) return <PageSpinner />;

  if (error) {
    return (
      <div className="rounded border border-loss/30 bg-loss/10 p-4 text-sm text-loss">
        Failed to load risk config: {String((error as { message?: string })?.message ?? error)}
      </div>
    );
  }

  if (!form || !data) return <PageSpinner />;

  function setField<K extends keyof RiskFormState>(key: K, value: RiskFormState[K]) {
    setForm((f) => (f ? { ...f, [key]: value } : f));
    setSaveNote(null);
  }

  async function handleSave() {
    if (!form) return;
    setSaving(true);
    setSaveError(null);
    setSaveNote(null);
    try {
      const updates = {
        drawdown_kill_threshold_pct: Number(form.drawdown_kill_threshold_pct),
        drawdown_cooldown_hours: Number(form.drawdown_cooldown_hours),
        max_asset_class_concentration_pct: Number(form.max_asset_class_concentration_pct),
        max_avg_correlation_pct: Number(form.max_avg_correlation_pct),
        max_cvar_95_pct: Number(form.max_cvar_95_pct),
        max_stress_drawdown_pct: Number(form.max_stress_drawdown_pct),
        mandate_allowed_symbols: form.mandate_allowed_symbols
          .split(",")
          .map((s) => s.trim())
          .filter((s) => s.length > 0),
        max_daily_trades: Number(form.max_daily_trades),
      };
      const res = await api.updateRiskConfig(updates);
      setForm(toRiskFormState(res));
      setSaveNote(res.note ?? "Saved.");
      await mutate();
    } catch (e) {
      setSaveError(
        e instanceof ApiError ? e.message : String((e as { message?: string })?.message ?? e),
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleRestart() {
    setRestarting(true);
    setSaveError(null);
    try {
      await api.restart();
      setSaveNote("Restart signal sent. The bot will be back in ~10-30s — refresh this page after that.");
    } catch (e) {
      setSaveError(
        e instanceof ApiError ? e.message : String((e as { message?: string })?.message ?? e),
      );
    } finally {
      setRestarting(false);
    }
  }

  const dirty = JSON.stringify(form) !== JSON.stringify(toRiskFormState(data));

  return (
    <div className="space-y-3">
      {saveError && (
        <div className="rounded border border-loss/30 bg-loss/10 p-3 text-sm text-loss">
          {saveError}
        </div>
      )}
      {saveNote && !saveError && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-gain/30 bg-gain/10 p-3 text-sm text-gain">
          <span>{saveNote}</span>
          <button
            onClick={handleRestart}
            disabled={restarting}
            className="btn-ghost text-xs disabled:opacity-50"
          >
            {restarting ? "Restarting…" : "Restart now"}
          </button>
        </div>
      )}

      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Risk &amp; safety gates</span>
        </div>
        {data.pending_restart && (
          <div className="border-b border-ink-700 bg-gold/10 px-4 py-2 text-xs text-gold">
            Unsaved changes pending restart
          </div>
        )}
        <div className="grid grid-cols-1 gap-4 p-4 md:grid-cols-2">
          {RISK_NUMERIC_FIELDS.map((f) => (
            <FieldInput
              key={f.key}
              field={f}
              value={form[f.key]}
              onChange={(v) => setField(f.key, v)}
            />
          ))}
        </div>
        <div className="border-t border-ink-700 p-4">
          <label className="block text-sm">
            <span className="text-cream-50/90">Mandate allowed symbols</span>
            <input
              type="text"
              value={form.mandate_allowed_symbols}
              onChange={(e) => setField("mandate_allowed_symbols", e.target.value)}
              placeholder="BTC-USD, ETH-USD, SPY (empty = no restriction)"
              className="mt-1 w-full rounded-lg border border-ink-700 bg-ink-800 px-3 py-2 text-cream-50 outline-none focus:border-gold/60"
            />
            <span className="mt-0.5 block text-[11px] text-muted">
              Comma-separated. When the mandate gate is enabled, only these
              symbols can be traded — leave empty to allow any symbol.
            </span>
          </label>
        </div>
      </section>

      <div className="flex items-center justify-between">
        <div className="text-[11px] text-muted">
          {data.updated_at
            ? `Last saved ${fmtTimestamp(data.updated_at)}${data.updated_by ? ` by ${data.updated_by}` : ""}`
            : "No dashboard changes saved yet — showing config.yaml defaults."}
        </div>
        <button
          onClick={handleSave}
          disabled={!dirty || saving}
          className={clsx(
            "rounded-lg px-4 py-2 text-sm font-medium transition",
            dirty && !saving
              ? "bg-gold text-ink-900 hover:bg-gold/90"
              : "bg-ink-800 text-muted",
          )}
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const { data, error, mutate } = useSWR<TradingConfigResponse>(
    "trading-config",
    () => api.config(),
  );
  const [form, setForm] = useState<FormState | null>(null);
  const [saving, setSaving] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveNote, setSaveNote] = useState<string | null>(null);

  // Sync form state from the server ONLY on first load — don't clobber
  // in-progress edits every time SWR revalidates in the background.
  useEffect(() => {
    if (data && form === null) {
      setForm(toFormState(data));
    }
  }, [data, form]);

  if (!data && !error) return <PageSpinner />;

  if (error) {
    return (
      <div className="rounded border border-loss/30 bg-loss/10 p-4 text-sm text-loss">
        Failed to load trading config: {String((error as { message?: string })?.message ?? error)}
      </div>
    );
  }

  if (!form || !data) return <PageSpinner />;

  function setField<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => (f ? { ...f, [key]: value } : f));
    setSaveNote(null);
  }

  async function handleSave() {
    if (!form) return;
    setSaving(true);
    setSaveError(null);
    setSaveNote(null);
    try {
      const updates = {
        risk_per_trade_pct: Number(form.risk_per_trade_pct),
        max_open_trades: Math.round(Number(form.max_open_trades)),
        min_order_usd: Number(form.min_order_usd),
        max_capital_per_trade_pct: Number(form.max_capital_per_trade_pct),
        atr_stop_multiplier: Number(form.atr_stop_multiplier),
        atr_take_profit_multiplier: Number(form.atr_take_profit_multiplier),
        risk_reward_ratio: Number(form.risk_reward_ratio),
        enable_position_replacement: form.enable_position_replacement,
        replacement_score_threshold: Number(form.replacement_score_threshold),
        min_profit_to_protect: Number(form.min_profit_to_protect),
      };
      const res = await api.updateConfig(updates);
      setForm(toFormState(res));
      setSaveNote(res.note ?? "Saved.");
      await mutate();
    } catch (e) {
      setSaveError(
        e instanceof ApiError ? e.message : String((e as { message?: string })?.message ?? e),
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleRestart() {
    setRestarting(true);
    setSaveError(null);
    try {
      await api.restart();
      setSaveNote("Restart signal sent. The bot will be back in ~10-30s — refresh this page after that.");
    } catch (e) {
      setSaveError(
        e instanceof ApiError ? e.message : String((e as { message?: string })?.message ?? e),
      );
    } finally {
      setRestarting(false);
    }
  }

  const dirty = JSON.stringify(form) !== JSON.stringify(toFormState(data));

  return (
    <div className="space-y-5 animate-fade-in">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">
            Trading Settings
          </h1>
          <p className="text-sm text-muted">
            Edit and save — changes take effect on the bot&apos;s next
            restart (main.py reads these once at startup).
          </p>
        </div>
        {data.pending_restart && (
          <span className="rounded-full border border-gold/40 bg-gold/10 px-3 py-1 text-xs text-gold">
            Unsaved changes pending restart
          </span>
        )}
      </header>

      {saveError && (
        <div className="rounded border border-loss/30 bg-loss/10 p-3 text-sm text-loss">
          {saveError}
        </div>
      )}
      {saveNote && !saveError && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-gain/30 bg-gain/10 p-3 text-sm text-gain">
          <span>{saveNote}</span>
          <button
            onClick={handleRestart}
            disabled={restarting}
            className="btn-ghost text-xs disabled:opacity-50"
          >
            {restarting ? "Restarting…" : "Restart now"}
          </button>
        </div>
      )}

      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Position sizing &amp; limits</span>
        </div>
        <div className="grid grid-cols-1 gap-4 p-4 md:grid-cols-2">
          {NUMERIC_FIELDS.slice(0, 4).map((f) => (
            <FieldInput
              key={f.key}
              field={f}
              value={form[f.key]}
              onChange={(v) => setField(f.key, v)}
            />
          ))}
        </div>
      </section>

      <section className="card overflow-hidden">
        <div className="card-header">
          <span>Stops, targets &amp; replacement</span>
        </div>
        <div className="grid grid-cols-1 gap-4 p-4 md:grid-cols-2">
          {NUMERIC_FIELDS.slice(4).map((f) => (
            <FieldInput
              key={f.key}
              field={f}
              value={form[f.key]}
              onChange={(v) => setField(f.key, v)}
            />
          ))}
        </div>
        <div className="border-t border-ink-700 p-4">
          <label className="flex items-center justify-between text-sm">
            <div>
              <div>Position replacement</div>
              <p className="mt-0.5 text-[11px] text-muted">
                When enabled, a new higher-scoring signal can replace a
                weaker open position once max simultaneous trades is
                reached.
              </p>
            </div>
            <input
              type="checkbox"
              checked={form.enable_position_replacement}
              onChange={(e) =>
                setField("enable_position_replacement", e.target.checked)
              }
              className="h-5 w-5 accent-gold"
            />
          </label>
        </div>
      </section>

      <div className="flex items-center justify-between">
        <div className="text-[11px] text-muted">
          {data.updated_at
            ? `Last saved ${fmtTimestamp(data.updated_at)}${data.updated_by ? ` by ${data.updated_by}` : ""}`
            : "No dashboard changes saved yet — showing config.yaml defaults."}
        </div>
        <button
          onClick={handleSave}
          disabled={!dirty || saving}
          className={clsx(
            "rounded-lg px-4 py-2 text-sm font-medium transition",
            dirty && !saving
              ? "bg-gold text-ink-900 hover:bg-gold/90"
              : "bg-ink-800 text-muted",
          )}
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>

      <header className="pt-2">
        <h2 className="font-display text-xl font-semibold tracking-tight">
          Risk &amp; Safety Gates
        </h2>
        <p className="text-sm text-muted">
          Drawdown kill-switch, portfolio-risk gate caps, and the mandate
          symbol allow-list. Same restart-required rule as above.
        </p>
      </header>
      <RiskSettingsSection />
    </div>
  );
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  // Loosely typed on purpose: shared by NUMERIC_FIELDS (trading config)
  // and RISK_NUMERIC_FIELDS (risk config) above, whose `key` string
  // literal unions differ.
  field: {
    key: string;
    label: string;
    hint: string;
    min?: number;
    max?: number;
    step?: string;
  };
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="text-cream-50/90">{field.label}</span>
      <input
        type="number"
        value={value}
        min={field.min}
        max={field.max}
        step={field.step ?? "any"}
        onChange={(e) => onChange(e.target.value)}
        className="num-cell mt-1 w-full rounded-lg border border-ink-700 bg-ink-800 px-3 py-2 text-cream-50 outline-none focus:border-gold/60"
      />
      <span className="mt-0.5 block text-[11px] text-muted">{field.hint}</span>
    </label>
  );
}
