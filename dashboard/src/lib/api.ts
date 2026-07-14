// Tiny fetch wrapper that injects the bearer token and handles 401/redirect.
// The token is stored in localStorage by AuthProvider; we read it on every call.

import type {
  Allocation,
  AuditEvent,
  CandlesResponse,
  CloseAllPositionsResponse,
  CorrelationResult,
  CVaRResult,
  EquityPoint,
  Health,
  HistoryResponse,
  LoginResponse,
  ModeInfo,
  RiskConfigResponse,
  RiskConfigUpdate,
  StateSnapshot,
  StressResult,
  TimeRange,
  TradingConfigResponse,
  TradingConfigUpdate,
  TradingPauseState,
  YfInterval,
} from "./types";

const API_BASE =
  (typeof window !== "undefined" &&
    (window as unknown as { __API_BASE__?: string }).__API_BASE__) ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8080";

// Token store — single source of truth, mirrored to localStorage.
// We keep this here (not in React state) so the same module instance
// is reachable from both server and client components without needing
// a provider for read-only access.
const TOKEN_KEY = "guaritradbot_token";
const TOKEN_EXP_KEY = "guaritradbot_token_exp";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getTokenExpiry(): number | null {
  if (typeof window === "undefined") return null;
  const v = window.localStorage.getItem(TOKEN_EXP_KEY);
  return v ? parseInt(v, 10) : null;
}

export function setToken(token: string, expiresInS: number) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
  window.localStorage.setItem(
    TOKEN_EXP_KEY,
    String(Date.now() + expiresInS * 1000),
  );
}

export function clearToken() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(TOKEN_EXP_KEY);
}

export function isTokenValid(): boolean {
  const t = getToken();
  const exp = getTokenExpiry();
  if (!t || !exp) return false;
  return Date.now() < exp;
}

class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
  }
}

async function request<T>(
  path: string,
  opts: RequestInit & { auth?: boolean } = {},
): Promise<T> {
  const url = `${API_BASE.replace(/\/+$/, "")}${path}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((opts.headers as Record<string, string>) || {}),
  };
  if (opts.auth !== false) {
    const t = getToken();
    if (t) headers["Authorization"] = `Bearer ${t}`;
  }
  const res = await fetch(url, { ...opts, headers });
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) {
    // 401: drop the token so the user gets bounced to /login
    if (res.status === 401) {
      clearToken();
      // Soft-redirect handled by AuthProvider's reactive subscription;
      // we don't navigate here to avoid coupling this lib to router.
    }
    const detail =
      (body && typeof body === "object" && (body as { detail?: string }).detail) ||
      res.statusText ||
      "Request failed";
    throw new ApiError(res.status, String(detail), body);
  }
  return body as T;
}

// ---------------- Endpoints ----------------

export const api = {
  baseUrl: API_BASE,

  health: () => request<Health>("/api/health"),

  // Auth
  login: (password: string) =>
    request<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
      auth: false,
    }),

  // State
  state: () => request<StateSnapshot>("/api/state"),
  positions: () => request<StateSnapshot["positions"]>("/api/positions"),
  position: (id: string) => request<StateSnapshot["positions"][number]>(`/api/positions/${id}`),
  positionCandles: (id: string, interval = "15m", window = 200) =>
    request<CandlesResponse>(
      `/api/positions/${id}/candles?interval=${interval}&window=${window}`,
    ),

  // Sprint 58: asset-scoped candle endpoint. Works for any asset
  // in the bot's universe (no position_id required), so the
  // /charts page can render live charts for assets that don't
  // have an open position yet.
  //
  // Sprint 59: extended to accept all yfinance intervals
  // (1m|5m|15m|1h|1d|1wk|1mo). The dashboard's time-range selector
  // uses `candlesRange()` (below) which picks the right interval
  // + limit for the chosen zoom level; this low-level `candles()`
  // is kept for callers that want fine-grained control.
  candles: (asset: string, interval: YfInterval = "1h", limit = 100) =>
    request<CandlesResponse>(
      `/api/candles?asset=${encodeURIComponent(asset)}&interval=${interval}&limit=${limit}`,
    ),

  // Sprint 59: convenience for the dashboard's time-range selector.
  // Internally maps a user-facing range (1D/5D/1M/3M/1Y/ALL) to
  // the appropriate (interval, limit) pair. The mapping lives in
  // `rangeToParams` below so it's testable in isolation.
  candlesRange: (asset: string, range: TimeRange) => {
    const { interval, limit } = rangeToParams(range);
    return request<CandlesResponse>(
      `/api/candles?asset=${encodeURIComponent(asset)}&interval=${interval}&limit=${limit}`,
    );
  },

  // Sprint 58: closed-position history with filters. All filter
  // fields are optional -- omit them to get "no filter on this
  // field". The bot sorts newest-first and applies the limit
  // AFTER filtering.
  history: (params: {
    from?: number;
    to?: number;
    assetClass?: "crypto" | "equity";
    direction?: "long" | "short";
    asset?: string;
    limit?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    if (params.from != null) qs.set("from", String(params.from));
    if (params.to != null) qs.set("to", String(params.to));
    if (params.assetClass) qs.set("asset_class", params.assetClass);
    if (params.direction) qs.set("direction", params.direction);
    if (params.asset) qs.set("asset", params.asset);
    if (params.limit != null) qs.set("limit", String(params.limit));
    return request<HistoryResponse>(`/api/positions/history?${qs}`);
  },

  // Audit
  audit: (limit = 100, eventType?: string) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (eventType) qs.set("event_type", eventType);
    return request<AuditEvent[]>(`/api/audit?${qs}`);
  },
  signals: (limit = 20) =>
    request<
      Array<{ ts: number; iso: string; event_type: string; payload: Record<string, unknown> }>
    >(`/api/signals?limit=${limit}`),

  // Mode
  getMode: () => request<ModeInfo>("/api/mode"),
  setMode: (mode: "live" | "paper", switchedBy?: string) =>
    request<{ mode: ModeInfo; note: string }>("/api/mode", {
      method: "POST",
      body: JSON.stringify({ mode, switched_by: switchedBy || "dashboard" }),
    }),

  // Position control
  closePosition: (id: string) =>
    request<{
      position_id: string;
      asset: string;
      direction: string;
      entry_price: number;
      close_price: number;
      realized_pnl_usd: number;
    }>(`/api/positions/${id}/close`, { method: "POST" }),

  // Sprint 46H: flatten every open position (Carlos's "clean session
  // before going live" use case).
  closeAllPositions: () =>
    request<CloseAllPositionsResponse>("/api/positions/close-all", { method: "POST" }),

  // Sprint 46H: Stop/Start toggle — pauses/resumes NEW entries only;
  // SL/TP protection on open positions is never affected.
  getTradingPause: () => request<TradingPauseState>("/api/trading-pause"),
  setTradingPause: (paused: boolean, updatedBy?: string) =>
    request<TradingPauseState>("/api/trading-pause", {
      method: "POST",
      body: JSON.stringify({ paused, updated_by: updatedBy || "dashboard" }),
    }),

  // Risk + allocation
  allocation: () => request<Allocation>("/api/allocation"),
  riskStress: () => request<StressResult>("/api/risk/stress"),
  riskCorrelation: () => request<CorrelationResult>("/api/risk/correlation"),
  riskCvar: () => request<CVaRResult>("/api/risk/cvar"),

  // Equity curve
  equity: (windowDays = 30) =>
    request<{ window_days: number; series: EquityPoint[] }>(
      `/api/equity?window_days=${windowDays}`,
    ),

  // Stats (compact KPIs)
  stats: () =>
    request<{
      mode: string;
      open_count: number;
      total_exposure_usd: number;
      total_unrealized_usd: number;
      total_unrealized_pct: number;
      daily_realized_pnl_usd: number;
      total_realized_pnl_usd: number;
      ts: number | null;
    }>("/api/stats"),

  // Trading config (Sprint 46C/D) — max simultaneous trades, risk %,
  // min order $, etc. Editable: updateConfig() saves a partial change
  // (only changed fields need to be sent); the bot picks it up on its
  // next restart (see `pending_restart` on the response) — call
  // restart() to apply immediately.
  config: () => request<TradingConfigResponse>("/api/config"),
  updateConfig: (updates: TradingConfigUpdate) =>
    request<TradingConfigResponse>("/api/config", {
      method: "POST",
      body: JSON.stringify(updates),
    }),

  // Risk/mandate config (Sprint 46F) — drawdown kill-switch
  // threshold/cooldown, portfolio-risk gate caps (concentration,
  // correlation, CVaR, stress), and the mandate's allowed-symbols
  // list. Same restart-required semantics as config()/updateConfig().
  riskConfig: () => request<RiskConfigResponse>("/api/risk-config"),
  updateRiskConfig: (updates: RiskConfigUpdate) =>
    request<RiskConfigResponse>("/api/risk-config", {
      method: "POST",
      body: JSON.stringify(updates),
    }),

  // Restart (Sprint 46D use case: apply saved config changes now).
  restart: () =>
    request<{ ok: boolean; pid: number; signal: string; note: string }>(
      "/api/restart",
      { method: "POST" },
    ),
};

export { ApiError };

// ----------------------------------------------------------------------
// Sprint 59: time-range → (interval, limit) mapping for the dashboard
// chart zoom buttons. Each range picks a yfinance interval that's
// granular enough to be useful at that zoom level without blowing up
// the response size or hitting yfinance's per-interval retention caps:
//
//   1D   -> 5m  interval, 100 bars (~8h of trading time)
//   5D   -> 15m interval, 200 bars (~2 trading days -- yfinance 15m
//                    caps at 60d so 5D is well within range)
//   1M   -> 1d  interval, 35 bars  (1 month, daily close)
//   3M   -> 1d  interval, 95 bars  (3 months, daily close)
//   1Y   -> 1d  interval, 370 bars (1 year, daily close)
//   ALL  -> 1wk interval, 520 bars (10 years of weekly closes)
//
// Reasoning per range:
//   - 1D/5D want intra-day detail (5m/15m) so you can see today's
//     volatility, but a 5D view shouldn't drown in noise so we bump
//     to 15m.
//   - 1M/3M/1Y use daily bars because that's the natural "zoom out"
//     level for "show me a month/quarter/year of price action".
//   - ALL uses weekly bars so 10+ years of history fits in a single
//     view (520 weekly bars = 10 years). Daily would mean 2000+
//     bars which is overkill for a chart card.
//
// The exact limit is generous (rounded up) so the chart line is
// always at the right edge of the selected range even with gaps
// (weekends, holidays, weekends for crypto = no gaps, etc.).
// ----------------------------------------------------------------------
export function rangeToParams(range: TimeRange): { interval: YfInterval; limit: number } {
  switch (range) {
    case "1D":  return { interval: "5m",  limit: 100 };
    case "5D":  return { interval: "15m", limit: 200 };
    case "1M":  return { interval: "1d",  limit: 35  };
    case "3M":  return { interval: "1d",  limit: 95  };
    case "1Y":  return { interval: "1d",  limit: 370 };
    case "ALL": return { interval: "1wk", limit: 520 };
  }
}
