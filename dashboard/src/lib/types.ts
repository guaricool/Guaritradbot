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
  positions: PositionSummary[];
  open_count: number;
  total_unrealized_usd: number;
  total_unrealized_pct: number;
  total_exposure_usd: number;
  daily_realized_pnl_usd: number;
  total_realized_pnl_usd: number;
  last_update_ts: number | null;
}

// Sprint 46C: read-only view of config.yaml's `trading:` section,
// served by GET /api/config.
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
