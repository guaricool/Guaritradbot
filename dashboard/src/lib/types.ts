// Types mirror the Pydantic models in src/api/state.py + server.py.
// Keep these in sync — if the bot API changes a field, update here too.

export type Direction = "long" | "short";

export interface PositionSummary {
  id: string;
  asset: string;
  direction: Direction;
  entry_price: number;
  current_price: number | null;
  current_price_source: "live" | "entry_fallback" | "fetch_failed";
  qty: number;
  notional_usd: number;
  unrealized_pnl_usd: number | null;
  unrealized_pnl_pct: number | null;
  stop_loss: number;
  take_profit: number;
  entry_ts: number;
  strategy: string;
  age_hours: number | null;
}

export interface ModeInfo {
  mode: "live" | "paper";
  mandate_enabled: boolean;
  use_testnet: boolean;
  switched_at: number | null;
  switched_by: string | null;
  mode_override_path: string;
}

export interface ScalpModeInfo {
  scalp_mode_enabled: boolean;
  switched_at: number | null;
  switched_by: string | null;
  scalp_mode_path: string;
}

// "live" = fetched from the broker just now (or cached within 15s).
// "unavailable" = broker configured but the last fetch failed (network/auth).
// "not_configured" = no credentials for this broker at all.
export type BalanceSource = "live" | "cache" | "unavailable" | "not_configured" | "unknown" | "broker" | "testnet_sim";

export interface StateSnapshot {
  mode: ModeInfo;
  balance_usd: number;
  balance_source: string;
  // Sprint 46C: real per-broker available cash (binance.us via ccxt,
  // Alpaca for equities/ETFs). null when the broker isn't configured
  // or the last fetch failed — see `balance_source` for why.
  binance_balance_usd: number | null;
  binance_balance_source: BalanceSource;
  alpaca_balance_usd: number | null;
  alpaca_balance_source: BalanceSource;
  // Sprint 62: paper-mode simulation fields.
  // - `effective_balance_usd`: the number the bot uses for position
  //   sizing. In paper mode = paper starting balance + realized P&L.
  //   In live mode = real broker balance.
  // - `effective_balance_source`: "broker_live" | "paper_simulated".
  // - `paper_starting_balance_usd`: null in live mode.
  effective_balance_usd: number | null;
  effective_balance_source: "broker_live" | "paper_simulated";
  paper_starting_balance_usd: number | null;
  positions: PositionSummary[];
  open_count: number;
  total_unrealized_usd: number;
  total_unrealized_pct: number;
  total_exposure_usd: number;
  daily_realized_pnl_usd: number;
  total_realized_pnl_usd: number;
  last_update_ts: number | null;
}

// Sprint 46C/D: config.yaml's `trading:` section merged with any
// dashboard-saved override, served by GET /api/config. Editable via
// POST /api/config (Sprint 46D) — see `pending_restart` below for why
// a save doesn't apply instantly.
export interface TradingConfig {
  risk_per_trade_pct: number;
  max_open_trades: number;
  min_order_usd: number;
  max_capital_per_trade_pct: number;
  atr_stop_multiplier: number;
  atr_take_profit_multiplier: number;
  risk_reward_ratio: number;
  enable_position_replacement: boolean;
  replacement_score_threshold: number;
  min_profit_to_protect: number;
}

// Response shape for both GET and POST /api/config. `pending_restart`
// is true when a dashboard save hasn't been picked up by the running
// bot yet (main.py reads trading config once at startup, not per
// cycle) — the Settings page uses this to prompt a restart.
export interface TradingConfigResponse extends TradingConfig {
  pending_restart: boolean;
  updated_at: number | null;
  updated_by: string | null;
  note?: string;
}

// Partial update body for POST /api/config — only send the fields
// that changed.
export type TradingConfigUpdate = Partial<TradingConfig> & { updated_by?: string };

// Sprint 46F: config.yaml's `risk:` section (drawdown kill-switch +
// portfolio-risk gate caps) plus `mandate.allowed_symbols`, merged with
// any dashboard-saved override, served by GET /api/risk-config and
// editable via POST /api/risk-config. Same restart-required semantics
// as TradingConfig above.
export interface RiskConfig {
  drawdown_kill_threshold_pct: number;
  drawdown_cooldown_hours: number;
  max_asset_class_concentration_pct: number;
  max_avg_correlation_pct: number;
  max_cvar_95_pct: number;
  max_stress_drawdown_pct: number;
  mandate_allowed_symbols: string[];
  // Sprint 46J: new-entry rate limit, rolling 24h. 0 = unlimited.
  max_daily_trades: number;
}

export interface RiskConfigResponse extends RiskConfig {
  pending_restart: boolean;
  updated_at: number | null;
  updated_by: string | null;
  note?: string;
}

export type RiskConfigUpdate = Partial<RiskConfig> & { updated_by?: string };

// Sprint 46H: dashboard Stop/Start toggle for new entries (see
// src/api/state.py::read_trading_pause / write_trading_pause docstrings
// for why this is intentionally separate from the mode LIVE/PAPER
// toggle and from the filesystem KillSwitch — pausing new entries
// never pauses SL/TP protection on positions already open).
export interface TradingPauseState {
  paused: boolean;
  paused_at: number | null;
  paused_by: string | null;
}

// Sprint 46H: response shape for POST /api/positions/close-all.
export interface CloseAllPositionsResponse {
  closed_count: number;
  closed: Array<{
    position_id: string;
    asset: string;
    direction: string;
    entry_price: number;
    close_price: number;
    realized_pnl_usd: number;
  }>;
}

export interface AuditEvent {
  ts: number;
  iso: string;
  event_type: string;
  payload: Record<string, unknown>;
}

export interface Allocation {
  actual_weights: Record<string, number>;
  target_weights: Record<string, number>;
  drifts: Record<string, number>;
  max_abs_drift_pct: number;
  within_tolerance: boolean;
  classes_over_cap: string[];
  classes_under_floor: string[];
}

export interface StressScenario {
  name: string;
  description: string;
  drawdown_pct: number;
  portfolio_impact_usd: number;
}

export interface StressResult {
  scenarios: StressScenario[];
  worst_case: StressScenario | null;
  note?: string;
}

export interface CorrelationResult {
  assets: string[];
  matrix: number[][];
  avg_correlation: number;
  well_diversified: boolean;
}

export interface CVaRResult {
  cvar_95: number;
  cvar_99: number;
  var_95: number;
  var_99: number;
}

export interface EquityPoint {
  date: string;
  realized_pnl_usd: number;
  cumulative_usd: number;
}

export interface Candle {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface CandlesResponse {
  asset: string;
  interval: string;
  candles: Candle[];
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
}

// Sprint 58: closed-position history (asset-scoped candles don't
// need entry/SL/TP — the bot's positions store those separately).
// Mirrors the row shape in `src.api.server._history_impl`.
export interface HistoryRow {
  id: string;
  asset: string;
  asset_class: "crypto" | "equity" | "forex" | "other";
  direction: "long" | "short";
  entry_price: number;
  entry_ts: number;
  closed_price: number;
  closed_ts: number;
  close_reason: string;
  qty: number;
  notional_usd: number;
  realized_pnl_usd: number;
  fees_paid_usd: number;
  duration_hours: number | null;
  strategy: string;
}

export interface HistorySummary {
  total_trades: number;
  win_count: number;
  loss_count: number;
  breakeven_count: number;
  win_rate_pct: number;
  total_pnl_usd: number;
  total_fees_usd: number;
}

export interface HistoryResponse {
  positions: HistoryRow[];
  summary: HistorySummary;
}

// Sprint 59: chart assets + time-range selector.
// The dashboard shows assets the bot does NOT trade (forex + extra
// stocks) for visualization only -- the API accepts any ticker
// yfinance knows about, see src/api/server.py::_ASSET_CLASS_MAP.
export type AssetCategory = "crypto" | "forex" | "equity";

// Yfinance intervals the backend supports. Mirrors the regex
// in `/api/candles` Query (1m|5m|15m|1h|1d|1wk|1mo).
export type YfInterval = "1m" | "5m" | "15m" | "1h" | "1d" | "1wk" | "1mo";

// User-facing time-range buttons. Each maps to a (interval, limit)
// pair in lib/api.ts::rangeToParams so the dashboard re-fetches
// the right granularity for the chosen zoom level.
export type TimeRange = "1D" | "5D" | "1M" | "3M" | "1Y" | "ALL";

export interface Health {
  ok: boolean;
  ts: number;
  started_at: number | null;
  audit_path: string;
  positions_path: string;
  config_path: string;
  ws_clients: number;
}

export interface LoginResponse {
  token: string;
  expires_in_s: number;
  token_type: "Bearer";
}

// Live WebSocket message types
export type WsMessage =
  | {
      type: "hello";
      started_at: number | null;
      ts: number;
    }
  | {
      type: "heartbeat";
      ts: number;
    }
  | {
      type: "audit";
      event: AuditEvent;
    }
  | {
      type: "positions";
      positions: PositionSummary[];
      total_unrealized_usd: number;
      total_unrealized_pct: number;
      open_count: number;
      ts: number;
    };
