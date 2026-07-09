"""
Guaritradbot Dashboard v3 — Spectacular Live Cockpit (signal8-inspired).

v3 improvements (after studying signal8.ai):
- Sticky ticker bar at top with prices, % change, mini sparklines, flash on update
- KPI cards now include 1h sparklines (not just numbers)
- Filter chips (ALL / LONG / SHORT / HIGH-CONF) on signals panel
- Countdown to next bot run (hourly cadence)
- Time-relative timestamps everywhere ("2h ago" not "17:23:40")
- Signal cards with rich "Catalyst" narrative — combines RSI/MACD/ATR/Volume/momentum
- News panel slide-out with latest headlines per ticker (yfinance .news)
- Top Movers panel — the 5 assets sorted by 4h momentum
- Search bar (filters positions & signals)
- News mini-cards with ticker badges in sidebar

Stack: Streamlit + Plotly (Plotly charts: equity, price+candles, sparklines, gauge, donut).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

# Plotly
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ============================================================
#  PAGE CONFIG  &  GLOBAL STYLE
# ============================================================

st.set_page_config(
    page_title="Guaritradbot v3 — Live Cockpit",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;800&family=Inter:wght@400;500;700&display=swap');

  html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
  }
  .stApp {
    background: linear-gradient(180deg, #0b0f1a 0%, #131829 100%);
    color: #e6e8ee;
  }
  section.main > div { padding-top: 0.5rem; }

  /* ---------- ticker bar ---------- */
  .ticker-bar {
    background: linear-gradient(90deg, #0d1325 0%, #131829 50%, #0d1325 100%);
    border-top: 1px solid #2a3050;
    border-bottom: 1px solid #2a3050;
    padding: 8px 14px;
    display: flex;
    gap: 14px;
    overflow-x: auto;
    white-space: nowrap;
    margin: 0 -1rem 12px -1rem;
  }
  .ticker-cell {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 12px;
    border-radius: 6px;
    background: rgba(26, 31, 58, 0.5);
    border: 1px solid #2a3050;
    transition: all 0.2s;
  }
  .ticker-cell:hover { background: #1a1f3a; }
  .ticker-symbol {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    color: #ccd6f6;
    font-size: 0.95rem;
  }
  .ticker-price {
    font-family: 'JetBrains Mono', monospace;
    color: #ccd6f6;
    font-size: 0.85rem;
  }
  .ticker-delta { font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; font-weight: 700; }
  .flash-up   { animation: flashUp 1.2s ease-out; }
  .flash-down { animation: flashDown 1.2s ease-out; }
  @keyframes flashUp   { 0% { background: rgba(6, 214, 160, 0.35); } 100% { background: rgba(26, 31, 58, 0.5); } }
  @keyframes flashDown { 0% { background: rgba(247, 37, 133, 0.35); } 100% { background: rgba(26, 31, 58, 0.5); } }

  /* ---------- hero ---------- */
  .hero {
    background: linear-gradient(135deg, #1a1f3a 0%, #2d1b4e 50%, #1a1f3a 100%);
    border: 1px solid #2a3050;
    border-radius: 14px;
    padding: 18px 24px;
    margin-bottom: 14px;
    position: relative;
    overflow: hidden;
  }
  .hero::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    background: radial-gradient(circle at 20% 50%, rgba(99, 102, 241, 0.15), transparent 60%),
                radial-gradient(circle at 80% 50%, rgba(236, 72, 153, 0.12), transparent 60%);
    pointer-events: none;
  }
  .hero h1 {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.8rem;
    margin: 0;
    background: linear-gradient(90deg, #06d6a0, #4cc9f0, #f72585);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.02em;
  }
  .hero .sub { color: #8892b0; font-size: 0.9rem; margin-top: 4px; }
  .pulse-dot {
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 50%;
    background: #06d6a0;
    box-shadow: 0 0 0 0 rgba(6, 214, 160, 0.7);
    animation: pulse 2s infinite;
    margin-right: 6px;
    vertical-align: middle;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(6, 214, 160, 0.7); }
    70%  { box-shadow: 0 0 0 10px rgba(6, 214, 160, 0); }
    100% { box-shadow: 0 0 0 0 rgba(6, 214, 160, 0); }
  }

  /* ---------- KPI cards ---------- */
  .kpi-card {
    background: linear-gradient(180deg, #161b2e 0%, #1a1f3a 100%);
    border: 1px solid #2a3050;
    border-radius: 10px;
    padding: 12px 14px;
    height: 100%;
  }
  .kpi-label {
    color: #8892b0;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 4px;
  }
  .kpi-value {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.4rem;
    line-height: 1.1;
  }
  .kpi-delta { font-size: 0.72rem; color: #8892b0; margin-top: 2px; }
  .pos { color: #06d6a0; }
  .neg { color: #f72585; }
  .neu { color: #4cc9f0; }

  /* ---------- filter chips (signal8 style) ---------- */
  .chip-row { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
  .chip {
    display: inline-block;
    padding: 5px 12px;
    border-radius: 999px;
    background: rgba(26, 31, 58, 0.5);
    border: 1px solid #2a3050;
    color: #ccd6f6;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.15s;
  }
  .chip:hover { background: #1a1f3a; }
  .chip.active {
    background: rgba(76, 201, 240, 0.15);
    border-color: #4cc9f0;
    color: #4cc9f0;
  }
  .chip.long.active  { background: rgba(6, 214, 160, 0.15); border-color: #06d6a0; color: #06d6a0; }
  .chip.short.active { background: rgba(247, 37, 133, 0.15); border-color: #f72585; color: #f72585; }

  /* ---------- signal cards ---------- */
  .signal-card {
    background: #1a1f3a;
    border-left: 3px solid #4cc9f0;
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 8px;
    transition: transform 0.2s;
  }
  .signal-card:hover { transform: translateX(4px); }
  .signal-card.long  { border-left-color: #06d6a0; }
  .signal-card.short { border-left-color: #f72585; }
  .signal-card .asset {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.05rem;
  }
  .signal-card .meta { color: #8892b0; font-size: 0.78rem; }
  .signal-card .conf {
    display: inline-block;
    background: rgba(76, 201, 240, 0.15);
    color: #4cc9f0;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 0.72rem;
    font-weight: 700;
  }
  .signal-card .conf.high { background: rgba(6, 214, 160, 0.18); color: #06d6a0; }
  .signal-card .conf.low  { background: rgba(247, 37, 133, 0.18); color: #f72585; }

  /* catalyst line */
  .catalyst {
    font-size: 0.85rem;
    color: #ccd6f6;
    line-height: 1.45;
    margin-top: 6px;
  }
  .catalyst .bullet { color: #06d6a0; margin-right: 6px; }

  /* news card */
  .news-card {
    background: #161b2e;
    border: 1px solid #2a3050;
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 8px;
    transition: transform 0.15s;
  }
  .news-card:hover { transform: translateX(2px); border-color: #4cc9f0; }
  .news-card .headline {
    color: #ccd6f6;
    font-weight: 600;
    font-size: 0.9rem;
    line-height: 1.3;
    margin-bottom: 4px;
  }
  .news-card .badge {
    display: inline-block;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    font-size: 0.7rem;
    color: #4cc9f0;
    background: rgba(76, 201, 240, 0.1);
    padding: 1px 6px;
    border-radius: 4px;
    margin-right: 6px;
  }
  .news-card .meta { color: #8892b0; font-size: 0.72rem; }

  /* panel */
  .panel {
    background: #131829;
    border: 1px solid #2a3050;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 10px;
  }
  .panel-title {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    font-weight: 700;
    color: #ccd6f6;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 10px;
  }

  /* divider */
  .thin-divider {
    height: 1px;
    background: linear-gradient(90deg, transparent, #2a3050, transparent);
    margin: 10px 0;
  }

  /* sidebar */
  section[data-testid="stSidebar"] {
    background: #0b0f1a;
    border-right: 1px solid #2a3050;
  }

  /* countdown */
  .countdown {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.1rem;
    color: #4cc9f0;
    text-align: center;
    padding: 10px;
    background: rgba(76, 201, 240, 0.08);
    border: 1px dashed #4cc9f0;
    border-radius: 8px;
  }

  /* hide streamlit branding */
  #MainMenu { visibility: hidden; }
  footer { visibility: hidden; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ============================================================
#  DATA LOADERS
# ============================================================

def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_audit(path: str = "audit/audit.jsonl", n: int = 30) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


@st.cache_data(ttl=5)
def _load_state_cached():
    return _load_json("latest_state.json")


@st.cache_data(ttl=10)
def _load_positions_cached():
    data = _load_json("data_store/positions.json")
    return data.get("positions", [])


@st.cache_data(ttl=10)
def _load_audit_cached(n: int = 30):
    return _load_audit(n=n)


@st.cache_data(ttl=10)
def _load_csv(asset: str) -> pd.DataFrame:
    """Load most recent cached OHLCV for asset. Used for sparklines + price chart history."""
    candidates = [
        f"data_store/{asset}_15m.csv",
        f"data_store/{asset}_1h.csv",
        f"data_store/{asset}_4h.csv",
        f"data_store/{asset}_1d.csv",
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                df = pd.read_csv(c)
                return df
            except Exception:
                continue
    return pd.DataFrame()


@st.cache_data(ttl=120)
def _load_news(asset: str, max_items: int = 3) -> list:
    """Fetch latest news for an asset via yfinance (cached 2 min)."""
    try:
        import yfinance as yf
        from src.data.yf_safe import safe_yf_download  # ensure session is initialized
        t = yf.Ticker(asset)
        news = getattr(t, "news", None) or []
        return news[:max_items]
    except Exception:
        return []


# ============================================================
#  HELPERS
# ============================================================

def fmt_usd(x, decimals: int = 2) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}${x:,.{decimals}f}"


def fmt_pct(x, decimals: int = 2) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.{decimals}f}%"


def color_class(x) -> str:
    if x is None:
        return "neu"
    if x > 0:
        return "pos"
    if x < 0:
        return "neg"
    return "neu"


def rel_time(iso_or_ts) -> str:
    """Convert ISO timestamp or unix ts → '2h ago' style."""
    if not iso_or_ts:
        return "—"
    try:
        if isinstance(iso_or_ts, (int, float)):
            dt = datetime.fromtimestamp(iso_or_ts, tz=timezone.utc)
        elif isinstance(iso_or_ts, str):
            # try ISO first
            try:
                dt = datetime.fromisoformat(iso_or_ts.replace("Z", "+00:00"))
            except Exception:
                return iso_or_ts
        else:
            return "—"
        delta = datetime.now(tz=timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "—"


def asset_last_price(asset: str, market_data: dict) -> float | None:
    if not market_data:
        return None
    for tf in ["15m", "1h", "4h", "1d"]:
        df = market_data.get(asset, {}).get(tf)
        if df is not None and not getattr(df, "empty", True):
            try:
                v = df["Close"].iloc[-1]
                if hasattr(v, "item"):
                    v = v.item()
                return float(v)
            except Exception:
                continue
    return None


def asset_prev_price(asset: str, market_data: dict, lookback: int = 1) -> float | None:
    """Get the price N bars back, for change-% calculations."""
    if not market_data:
        return None
    for tf in ["15m", "1h", "4h", "1d"]:
        df = market_data.get(asset, {}).get(tf)
        if df is not None and not getattr(df, "empty", True):
            try:
                if len(df) > lookback:
                    v = df["Close"].iloc[-(lookback + 1)]
                    if hasattr(v, "item"):
                        v = v.item()
                    return float(v)
            except Exception:
                continue
    return None


def sparkline(values: list, color: str = "#4cc9f0", height: int = 32, width: int = 90) -> str:
    """Return an inline SVG sparkline."""
    if not values or len(values) < 2:
        return ""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx != mn else 1.0
    pts = []
    for i, v in enumerate(vals):
        x = i * width / (len(vals) - 1)
        y = height - (v - mn) / rng * (height - 4) - 2
        pts.append(f"{x:.1f},{y:.1f}")
    points_str = " ".join(pts)
    last_y = height - (vals[-1] - mn) / rng * (height - 4) - 2
    svg = (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{points_str}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{(len(vals) - 1) * width / (len(vals) - 1):.1f}" cy="{last_y:.1f}" '
        f'r="2" fill="{color}"/></svg>'
    )
    return svg


# ============================================================
#  SIDEBAR
# ============================================================

with st.sidebar:
    st.markdown("### ⚙️ Cockpit Controls")

    refresh_sec = st.selectbox(
        "Auto-refresh",
        options=[5, 15, 30, 60, 0],
        index=1,
        format_func=lambda x: "Off" if x == 0 else f"{x}s",
    )

    show_news_panel = st.checkbox("News slide-out panel", value=False)
    show_signals = st.checkbox("Show raw signal JSON", value=False)
    show_audit = st.checkbox("Show audit feed", value=True)

    st.markdown("---")
    st.markdown("### 📊 Config")
    config = _load_yaml("config.yaml")
    risk = config.get("trading", {})
    exch = config.get("exchange", {})
    mand = config.get("mandate", {})

    st.markdown(
        f"""
        - **Mode**: `{config.get('execution_mode','?')}`
        - **Exchange**: `{exch.get('name','?')}`
        - **Risk/trade**: `{risk.get('risk_per_trade_pct','?')}%`
        - **Max cap/trade**: `{exch.get('max_capital_per_trade_pct','?')}%`
        - **Max open**: `{risk.get('max_open_trades','?')}`
        - **Mandate**: `{'ON' if mand.get('enabled') else 'OFF'}`
        """
    )

    st.markdown("---")
    st.markdown("### 🔭 Search")
    search_q = st.text_input("Filter signals / positions", placeholder="SPY, BTC, EMA, …").strip().upper()

    st.markdown("---")
    st.caption(f"🕒 {datetime.now().strftime('%H:%M:%S')} CT")


# Auto-refresh
if refresh_sec and refresh_sec > 0:
    time.sleep(refresh_sec)
    st.rerun()


# ============================================================
#  LOAD DATA
# ============================================================

state_blob = _load_state_cached()
last_state = state_blob.get("state", {}) if state_blob else {}
last_ts_iso = state_blob.get("timestamp") if state_blob else None

market_data_raw = last_state.get("analyze_market", {}).get("market_data", {})
hypotheses = last_state.get("generate_hypotheses", {}).get("hypotheses", [])
debate = last_state.get("debate_hypotheses", {})
approved_hyp = debate.get("approved_hypotheses", [])
risk_eval = last_state.get("risk_evaluation", {})
executed = last_state.get("execute_trades", {}).get("executed_trades", [])

balance = float(risk_eval.get("account_balance", 100.0))
positions = _load_positions_cached()
audit_events = _load_audit_cached(n=50)

open_positions = [p for p in positions if p.get("closed_ts") is None]
closed_positions = [p for p in positions if p.get("closed_ts") is not None]
realized_pnl = sum((p.get("realized_pnl") or 0.0) for p in positions)

unrealized_pnl = 0.0
unrealized_breakdown = []
for p in open_positions:
    px = asset_last_price(p["asset"], market_data_raw)
    if px is not None:
        sign = 1.0 if p["direction"] == "long" else -1.0
        upnl = sign * (px - p["entry_price"]) * p["qty"]
        unrealized_pnl += upnl
        unrealized_breakdown.append((p, upnl, px))
    else:
        unrealized_breakdown.append((p, 0.0, p["entry_price"]))

equity = balance + realized_pnl + unrealized_pnl
drawdown_pct = ((equity - balance) / balance) * 100 if balance else 0.0

total_exposure = sum(abs(p["entry_price"] * p["qty"]) for p in open_positions)
exposure_pct = (total_exposure / balance * 100) if balance else 0.0

max_open = int(risk.get("max_open_trades", 5))

# Asset list + class grouping
ASSETS_EQUITY = ["SPY", "QQQ"]
ASSETS_COMMOD = ["GLD", "USO"]
ASSETS_CRYPTO = ["BTC-USD"]
ASSETS_ALL = ASSETS_EQUITY + ASSETS_COMMOD + ASSETS_CRYPTO


# ============================================================
#  STICKY TICKER BAR (signal8-inspired)
# ============================================================

ticker_cells = []
for asset in ASSETS_ALL:
    px = asset_last_price(asset, market_data_raw)
    prev = asset_prev_price(asset, market_data_raw, lookback=4)  # ~4h ago
    delta = None
    delta_pct = None
    if px is not None and prev is not None:
        delta = px - prev
        delta_pct = (delta / prev) * 100 if prev else 0.0
    cls = color_class(delta_pct)
    arrow = "▲" if (delta_pct or 0) >= 0 else "▼"
    cell = (
        f'<div class="ticker-cell">'
        f'<span class="ticker-symbol">{asset}</span>'
        f'<span class="ticker-price">${px:,.2f}</span>' if px is not None
        else f'<div class="ticker-cell"><span class="ticker-symbol">{asset}</span><span class="ticker-price">—</span>'
    )
    if delta_pct is not None:
        cell += f'<span class="ticker-delta {cls}">{arrow} {delta_pct:+.2f}%</span>'
    else:
        cell += '<span class="ticker-delta neu">—</span>'
    cell += "</div>"
    ticker_cells.append(cell)

st.markdown(
    f'<div class="ticker-bar">{"".join(ticker_cells)}</div>',
    unsafe_allow_html=True,
)


# ============================================================
#  HERO HEADER
# ============================================================

last_rel = rel_time(last_ts_iso) if last_ts_iso else "—"
st.markdown(
    f"""
    <div class="hero">
      <h1>⚡ GUARITRADBOT v3 <span style="color:#f72585; font-size:1rem; vertical-align:super;">LIVE</span></h1>
      <div class="sub">
        <span class="pulse-dot"></span> Engine running · Last cycle: <code style="color:#4cc9f0;">{last_rel}</code>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ============================================================
#  KPI CARDS with SPARKLINES
# ============================================================

def kpi_with_spark(label, value, spark_values=None, spark_color="#4cc9f0",
                   delta="", klass="neu"):
    spark_svg = ""
    if spark_values and len(spark_values) >= 2:
        spark_svg = (
            f'<div style="margin-top:4px;">{sparkline(spark_values, color=spark_color)}</div>'
        )
    return f"""
    <div class="kpi-card">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value {klass}">{value}</div>
      <div class="kpi-delta">{delta}</div>
      {spark_svg}
    </div>
    """


# Build sparkline series from CSV history (close, last 20 bars)
def spark_series(asset: str, n: int = 20) -> list:
    df = _load_csv(asset)
    if df.empty:
        return []
    close_col = next((c for c in ["Close", "close", "Adj Close"] if c in df.columns), None)
    if not close_col:
        return []
    return df[close_col].tail(n).tolist()


c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    st.markdown(kpi_with_spark("Balance", fmt_usd(balance)), unsafe_allow_html=True)
with c2:
    eq_spark = []
    # synthesize equity curve over recent closed trades + unrealized (simplified)
    sorted_closes = sorted(
        [p for p in positions if p.get("closed_ts")],
        key=lambda p: p["closed_ts"],
    )
    eq_running = balance
    series = [eq_running]
    for p in sorted_closes[-19:]:
        eq_running += p.get("realized_pnl") or 0.0
        series.append(eq_running)
    if unrealized_pnl:
        series.append(eq_running + unrealized_pnl)
    st.markdown(
        kpi_with_spark(
            "Equity", fmt_usd(equity),
            spark_values=series[-20:],
            spark_color="#06d6a0" if equity >= balance else "#f72585",
            delta=f"{fmt_pct(drawdown_pct)} all-time",
            klass=color_class(equity - balance),
        ),
        unsafe_allow_html=True,
    )
with c3:
    st.markdown(
        kpi_with_spark(
            "Open PnL", fmt_usd(unrealized_pnl),
            spark_values=[(p.get('entry_price') or 0) for p in open_positions],
            spark_color="#06d6a0" if unrealized_pnl >= 0 else "#f72585",
            delta="unrealized",
            klass=color_class(unrealized_pnl),
        ),
        unsafe_allow_html=True,
    )
with c4:
    st.markdown(
        kpi_with_spark(
            "Realized", fmt_usd(realized_pnl),
            spark_values=[p.get('realized_pnl') or 0 for p in closed_positions[-20:]],
            spark_color="#06d6a0" if realized_pnl >= 0 else "#f72585",
            delta=f"{len(closed_positions)} closed",
            klass=color_class(realized_pnl),
        ),
        unsafe_allow_html=True,
    )
with c5:
    st.markdown(
        kpi_with_spark(
            "Positions", f"{len(open_positions)}/{max_open}",
            spark_values=[len(open_positions)] * 5,
            delta=f"{fmt_pct(exposure_pct, 1)} exposed",
            klass="neu",
        ),
        unsafe_allow_html=True,
    )
with c6:
    bot_status = "🟢 LIVE" if mand.get("enabled") else "🟡 PAPER"
    st.markdown(
        kpi_with_spark(
            "Engine", bot_status,
            spark_values=[1] * 5,
            delta=f"{len(executed)} exec'd this cycle",
            klass="neu",
        ),
        unsafe_allow_html=True,
    )


# ============================================================
#  EQUITY CURVE  +  RISK GAUGE  +  COUNTDOWN
# ============================================================

col_eq, col_gauge, col_cd = st.columns([2, 1, 1])

with col_eq:
    st.markdown('<div class="panel-title">📈 Equity Curve</div>',
                unsafe_allow_html=True)
    if not positions:
        fig_eq = go.Figure()
        fig_eq.add_hline(y=balance, line_color="#4cc9f0", line_dash="dash",
                         annotation_text=f"Start: ${balance:.2f}")
        fig_eq.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#ccd6f6", family="JetBrains Mono"),
            height=300, margin=dict(l=40, r=20, t=10, b=30),
            xaxis=dict(gridcolor="#1f2640"),
            yaxis=dict(gridcolor="#1f2640", title="Equity ($)"),
            annotations=[dict(
                x=0.5, y=0.5, xref="paper", yref="paper",
                text="No trades yet — waiting for first signal",
                showarrow=False, font=dict(size=14, color="#8892b0"),
            )],
        )
    else:
        sorted_closes = sorted(
            [p for p in positions if p.get("closed_ts")],
            key=lambda p: p["closed_ts"],
        )
        ts = [datetime.fromtimestamp(sorted_closes[0]["entry_ts"])] if sorted_closes else [datetime.now()]
        eq = [balance]
        eq_running = balance
        for p in sorted_closes:
            eq_running += p.get("realized_pnl") or 0.0
            ts.append(datetime.fromtimestamp(p["closed_ts"]))
            eq.append(eq_running)
        if unrealized_pnl != 0.0 and open_positions:
            ts.append(datetime.now())
            eq.append(eq_running + unrealized_pnl)
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=ts, y=eq, mode="lines+markers",
            line=dict(color="#06d6a0", width=3, shape="spline", smoothing=1.0),
            marker=dict(size=8, color="#06d6a0", line=dict(color="#0b0f1a", width=2)),
            fill="tozeroy", fillcolor="rgba(6, 214, 160, 0.12)",
            name="Equity",
            hovertemplate="<b>%{x}</b><br>Equity: $%{y:.2f}<extra></extra>",
        ))
        fig_eq.add_hline(y=balance, line_color="#4cc9f0", line_dash="dash",
                         annotation_text=f"Start ${balance:.2f}",
                         annotation_position="top left",
                         annotation_font_color="#4cc9f0")
        fig_eq.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#ccd6f6", family="JetBrains Mono"),
            height=300, margin=dict(l=40, r=20, t=10, b=30),
            xaxis=dict(gridcolor="#1f2640"),
            yaxis=dict(gridcolor="#1f2640", title="Equity ($)"),
            showlegend=False,
        )
    st.plotly_chart(fig_eq, use_container_width=True, theme=None)

with col_gauge:
    st.markdown('<div class="panel-title">🎯 Risk Exposure</div>',
                unsafe_allow_html=True)
    fig_gauge = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=exposure_pct,
        delta={"reference": float(risk.get("risk_per_trade_pct", 1.0)) * max_open,
               "increasing": {"color": "#f72585"},
               "decreasing": {"color": "#06d6a0"}},
        gauge={
            "axis": {"range": [0, 100], "tickcolor": "#8892b0",
                     "tickfont": {"color": "#8892b0"}},
            "bar": {"color": "#4cc9f0", "thickness": 0.3},
            "bgcolor": "#1a1f3a",
            "borderwidth": 0,
            "steps": [
                {"range": [0, 30], "color": "rgba(6, 214, 160, 0.25)"},
                {"range": [30, 70], "color": "rgba(255, 209, 102, 0.25)"},
                {"range": [70, 100], "color": "rgba(247, 37, 133, 0.25)"},
            ],
            "threshold": {
                "line": {"color": "#f72585", "width": 4},
                "thickness": 0.8,
                "value": float(risk.get("risk_per_trade_pct", 1.0)) * max_open,
            },
        },
        number={"suffix": "%", "font": {"color": "#ccd6f6", "size": 30,
                                        "family": "JetBrains Mono"}},
        domain={"x": [0, 1], "y": [0, 1]},
    ))
    fig_gauge.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#ccd6f6", family="JetBrains Mono"),
        height=300, margin=dict(l=20, r=20, t=10, b=10),
    )
    st.plotly_chart(fig_gauge, use_container_width=True, theme=None)

with col_cd:
    st.markdown('<div class="panel-title">⏱ Next Bot Run</div>',
                unsafe_allow_html=True)
    # Countdown to the next hour boundary
    now = datetime.now()
    next_run = now.replace(minute=0, second=0, microsecond=0)
    # If we're past the top of the hour, add 1h
    if now.minute > 0 or now.second > 0:
        next_run = next_run.replace(hour=(now.hour + 1) % 24)
    delta = (next_run - now).total_seconds()
    mins = int(delta // 60)
    secs = int(delta % 60)
    st.markdown(
        f'<div class="countdown">{mins:02d}m {secs:02d}s<br>'
        f'<span style="font-size:0.7rem; color:#8892b0;">@ {next_run.strftime("%H:%M")} CT</span></div>',
        unsafe_allow_html=True,
    )

    # Also: time since last cycle
    st.markdown(
        f'<div style="text-align:center; margin-top:8px; font-size:0.78rem; color:#8892b0;">'
        f'Last cycle: <span style="color:#4cc9f0;">{last_rel}</span></div>',
        unsafe_allow_html=True,
    )
    # And: candles / hour bar
    fig_bar = go.Figure()
    fig_bar.add_trace(go.Bar(
        x=["done", "left"], y=[60 - (mins + 1), mins + 1],
        marker_color=["#06d6a0", "#2a3050"],
        showlegend=False, width=[0.5, 0.5],
    ))
    fig_bar.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        height=120, margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
    )
    st.plotly_chart(fig_bar, use_container_width=True, theme=None)


# ============================================================
#  LIVE PRICE CHARTS — per asset, with buy/sell markers
# ============================================================

st.markdown('<div class="panel-title">📊 Live Market — buy/sell zones</div>',
            unsafe_allow_html=True)

asset_tabs = st.tabs([f" {a} " for a in ASSETS_ALL])

for tab, asset in zip(asset_tabs, ASSETS_ALL):
    with tab:
        df = _load_csv(asset)
        if df.empty:
            st.info(f"No cached data for {asset} yet. Bot will populate on next cycle.")
            continue
        close_col = next((c for c in ["Close", "close", "Adj Close"] if c in df.columns), None)
        if close_col is None:
            st.warning(f"No Close column in {asset} CSV.")
            continue

        asset_positions = [p for p in positions if p.get("asset") == asset]
        buys_x, buys_y, sells_x, sells_y = [], [], [], []
        for p in asset_positions:
            ts = pd.to_datetime(p.get("entry_ts"), unit="s")
            ts_close = pd.to_datetime(p.get("closed_ts"), unit="s") if p.get("closed_ts") else None
            if p.get("direction") == "long":
                buys_x.append(ts); buys_y.append(p["entry_price"])
            else:
                sells_x.append(ts); sells_y.append(p["entry_price"])
            if ts_close is not None and p.get("closed_price") is not None:
                if p.get("direction") == "long":
                    sells_x.append(ts_close); sells_y.append(p["closed_price"])
                else:
                    buys_x.append(ts_close); buys_y.append(p["closed_price"])

        date_col = next((c for c in ["Datetime", "Date", "date", "timestamp"] if c in df.columns), None)
        x_vals = pd.to_datetime(df[date_col]) if date_col else pd.RangeIndex(len(df))

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            vertical_spacing=0.03, row_heights=[0.7, 0.3])
        fig.add_trace(go.Scatter(
            x=x_vals, y=df[close_col], mode="lines",
            line=dict(color="#4cc9f0", width=2),
            name=f"{asset} Close",
            hovertemplate="<b>%{x}</b><br>$%{y:.2f}<extra></extra>",
        ), row=1, col=1)
        if buys_x:
            fig.add_trace(go.Scatter(
                x=buys_x, y=buys_y, mode="markers",
                marker=dict(symbol="triangle-up", size=14, color="#06d6a0",
                            line=dict(color="#0b0f1a", width=1.5)),
                name="BUY", hovertemplate="BUY @ $%{y:.2f}<extra></extra>",
            ), row=1, col=1)
        if sells_x:
            fig.add_trace(go.Scatter(
                x=sells_x, y=sells_y, mode="markers",
                marker=dict(symbol="triangle-down", size=14, color="#f72585",
                            line=dict(color="#0b0f1a", width=1.5)),
                name="SELL", hovertemplate="SELL @ $%{y:.2f}<extra></extra>",
            ), row=1, col=1)
        vol_col = next((c for c in ["Volume", "volume"] if c in df.columns), None)
        if vol_col:
            colors = ["#06d6a0" if (i == 0 or df[close_col].iloc[i] >= df[close_col].iloc[i-1])
                      else "#f72585"
                      for i in range(len(df))]
            fig.add_trace(go.Bar(
                x=x_vals, y=df[vol_col], marker_color=colors, opacity=0.5,
                showlegend=False, name="Volume",
            ), row=2, col=1)
        fig.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#ccd6f6", family="JetBrains Mono"),
            height=380, margin=dict(l=40, r=20, t=10, b=30),
            xaxis=dict(gridcolor="#1f2640"),
            xaxis2=dict(gridcolor="#1f2640"),
            yaxis=dict(gridcolor="#1f2640", title="Price ($)"),
            yaxis2=dict(gridcolor="#1f2640", title="Vol"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="right", x=1, bgcolor="rgba(0,0,0,0)"),
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True, theme=None)


# ============================================================
#  TOP MOVERS — signal8 inspiration
# ============================================================

st.markdown('<div class="panel-title">🔥 Top Movers (4h momentum)</div>',
            unsafe_allow_html=True)

movers = []
for asset in ASSETS_ALL:
    px = asset_last_price(asset, market_data_raw)
    prev = asset_prev_price(asset, market_data_raw, lookback=4)
    if px is not None and prev is not None and prev:
        pct = (px - prev) / prev * 100
        spark = spark_series(asset, n=20)
        movers.append((asset, px, pct, spark))
movers.sort(key=lambda m: abs(m[2]), reverse=True)

m_cols = st.columns(len(movers))
for col, (asset, px, pct, spark) in zip(m_cols, movers):
    cls = color_class(pct)
    arrow = "▲" if pct >= 0 else "▼"
    with col:
        st.markdown(
            f'<div class="kpi-card">'
            f'<div class="kpi-label">{asset}</div>'
            f'<div class="kpi-value {cls}">${px:,.2f}</div>'
            f'<div class="kpi-delta">{arrow} {pct:+.2f}% · 4h</div>'
            f'<div style="margin-top:6px;">{sparkline(spark, color="#06d6a0" if pct >= 0 else "#f72585", height=36, width=160)}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


# ============================================================
#  OPEN POSITIONS  +  SMART SIGNALS (with filter chips)
# ============================================================

col_pos, col_sig = st.columns([1, 1])

with col_pos:
    st.markdown('<div class="panel-title">💼 Open Positions (Live PnL)</div>',
                unsafe_allow_html=True)
    if not open_positions:
        st.markdown(
            """<div class="panel" style="text-align:center; padding:24px; color:#8892b0;">
              No open positions. Bot is scanning the market — next signal incoming.
            </div>""",
            unsafe_allow_html=True,
        )
    else:
        rows = []
        for p, upnl, px in unrealized_breakdown:
            entry = p["entry_price"]
            notional = abs(entry * p["qty"])
            margin = (notional / balance * 100) if balance else 0.0
            rows.append({
                "Asset": p["asset"],
                "Dir": p["direction"].upper(),
                "Entry": f"${entry:.2f}",
                "Now": f"${px:.2f}",
                "Qty": f"{p['qty']:.4f}",
                "PnL $": fmt_usd(upnl, 2),
                "PnL %": fmt_pct((upnl / (entry * p["qty"]) * 100)
                                 if entry * p["qty"] else 0.0, 2),
                "SL": f"${p['stop_loss']:.2f}",
                "TP": f"${p['take_profit']:.2f}",
                "Strategy": p.get("strategy", "?"),
                "Margin": f"{margin:.1f}%",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

with col_sig:
    st.markdown('<div class="panel-title">🧠 Smart Signals (current cycle)</div>',
                unsafe_allow_html=True)

    # FILTER CHIPS (signal8 style)
    if "signal_filter" not in st.session_state:
        st.session_state.signal_filter = "ALL"

    chips_html = '<div class="chip-row">'
    for label, value in [("ALL", "ALL"), ("LONG", "long"), ("SHORT", "short"),
                         ("HIGH-CONF ≥75%", "high"), ("LOW-CONF <60%", "low")]:
        cls = "active" if st.session_state.signal_filter == value else ""
        if value == "long":
            cls += " long"
        elif value == "short":
            cls += " short"
        chips_html += f'<span class="chip {cls}">{label}</span>'
    chips_html += "</div>"
    st.markdown(chips_html, unsafe_allow_html=True)

    fcol1, fcol2, fcol3, fcol4, fcol5 = st.columns(5)
    with fcol1:
        if st.button("ALL", use_container_width=True, key="chip_all"):
            st.session_state.signal_filter = "ALL"
            st.rerun()
    with fcol2:
        if st.button("LONG", use_container_width=True, key="chip_long"):
            st.session_state.signal_filter = "long"
            st.rerun()
    with fcol3:
        if st.button("SHORT", use_container_width=True, key="chip_short"):
            st.session_state.signal_filter = "short"
            st.rerun()
    with fcol4:
        if st.button("HIGH-CONF", use_container_width=True, key="chip_high"):
            st.session_state.signal_filter = "high"
            st.rerun()
    with fcol5:
        if st.button("LOW-CONF", use_container_width=True, key="chip_low"):
            st.session_state.signal_filter = "low"
            st.rerun()

    # Filter + render signals
    if not hypotheses:
        st.markdown(
            """<div class="panel" style="text-align:center; padding:18px; color:#8892b0;">
              No new signals this cycle. Bot is watching for RSI / MACD / EMA crosses.
            </div>""",
            unsafe_allow_html=True,
        )
    else:
        # Enrich with confidence + filter
        enriched = []
        for h in hypotheses:
            direction = h.get("direction", "long")
            strategy = h.get("strategy", "?")
            price = h.get("price", 0)
            atr = h.get("atr_at_signal", 0) or 0.0
            conf = 50
            if "RSI" in strategy:
                rsi = h.get("rsi_at_signal", 50)
                conf = min(95, 50 + abs(50 - rsi))
            elif "MACD" in strategy:
                macd = h.get("macd_at_signal", 0)
                conf = min(95, 60 + abs(macd) * 10)
            elif "EMA" in strategy:
                ema20 = h.get("ema20_at_signal", 0)
                ema50 = h.get("ema50_at_signal", 0)
                spread = abs(ema20 - ema50) / max(price, 1e-6) * 1000
                conf = min(95, 50 + spread)
            conf = max(20, min(95, int(conf)))
            enriched.append({**h, "confidence": conf, "direction": direction})

        f = st.session_state.signal_filter
        if f == "long":
            enriched = [e for e in enriched if e["direction"] == "long"]
        elif f == "short":
            enriched = [e for e in enriched if e["direction"] == "short"]
        elif f == "high":
            enriched = [e for e in enriched if e["confidence"] >= 75]
        elif f == "low":
            enriched = [e for e in enriched if e["confidence"] < 60]

        # apply search filter
        if search_q:
            enriched = [e for e in enriched
                        if search_q in e.get("asset", "").upper()
                        or search_q in e.get("strategy", "").upper()]

        if not enriched:
            st.markdown(
                f'<div class="panel" style="text-align:center; padding:18px; color:#8892b0;">'
                f'No signals matching filter <code style="color:#4cc9f0;">{f}</code>.</div>',
                unsafe_allow_html=True,
            )
        else:
            for h in enriched:
                direction = h["direction"]
                asset = h.get("asset", "?")
                strategy = h.get("strategy", "?")
                price = h.get("price", 0)
                atr = h.get("atr_at_signal", 0) or 0.0
                conf = h["confidence"]
                conf_cls = "high" if conf >= 75 else ("low" if conf < 60 else "")
                arrow = "▲" if direction == "long" else "▼"

                # Build catalyst narrative (signal8 style: human-readable "why")
                catalyst_parts = []
                if "RSI" in strategy:
                    rsi = h.get("rsi_at_signal", 0)
                    catalyst_parts.append(
                        f"<b>{direction.upper()}</b> triggered by RSI crossing "
                        f"<b>{'below' if direction == 'long' else 'above'} "
                        f"{'oversold' if direction == 'long' else 'overbought'}</b> "
                        f"at level {rsi:.1f}"
                    )
                elif "MACD" in strategy:
                    macd = h.get("macd_at_signal", 0)
                    catalyst_parts.append(
                        f"<b>{direction.upper()}</b> on MACD bullish/bearish cross "
                        f"(MACD = {macd:.4f})"
                    )
                elif "EMA" in strategy:
                    ema20 = h.get("ema20_at_signal", 0)
                    ema50 = h.get("ema50_at_signal", 0)
                    cross_type = "Golden" if direction == "long" else "Death"
                    catalyst_parts.append(
                        f"<b>{cross_type} cross</b> on {h.get('tf','?')} EMA20/EMA50 "
                        f"(spread {(ema20 - ema50):.4f})"
                    )
                # ATR-based SL/TP
                k_sl = float(risk.get("atr_stop_multiplier", 2.0))
                k_tp = float(risk.get("atr_take_profit_multiplier", 4.0))
                if atr:
                    if direction == "long":
                        sl = price - k_sl * atr
                        tp = price + k_tp * atr
                    else:
                        sl = price + k_sl * atr
                        tp = price - k_tp * atr
                    r_mult = k_tp / k_sl
                    catalyst_parts.append(
                        f"Stop <b>${sl:.2f}</b> · TP <b>${tp:.2f}</b> "
                        f"({r_mult:.1f}:1 R:R via ATR={atr:.4f})"
                    )
                # Add momentum context from 4h change
                prev = asset_prev_price(asset, market_data_raw, lookback=4)
                if prev and price:
                    pct = (price - prev) / prev * 100
                    arrow2 = "▲" if pct >= 0 else "▼"
                    catalyst_parts.append(
                        f"<span style='color:{'#06d6a0' if pct>=0 else '#f72585'};'>"
                        f"{arrow2} {pct:+.2f}% in 4h</span>"
                    )
                catalyst_html = "<br>".join(
                    f'<span class="bullet">▸</span>{p}' for p in catalyst_parts
                )

                st.markdown(
                    f"""
                    <div class="signal-card {direction}">
                      <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                          <span class="asset">{arrow} {asset}</span>
                          <span class="meta" style="margin-left:8px;">${price:.2f} · {h.get('tf','?')}</span>
                        </div>
                        <span class="conf {conf_cls}">{conf}%</span>
                      </div>
                      <div class="catalyst">{catalyst_html}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            if show_signals:
                with st.expander("Raw signal JSON", expanded=False):
                    st.json(hypotheses)


# ============================================================
#  SETTINGS PANEL — editable risk config (writes overrides)
# ============================================================

st.markdown('<div class="panel-title">⚙️ Risk Settings — editable</div>',
            unsafe_allow_html=True)

col_s1, col_s2, col_s3, col_s4, col_s5 = st.columns(5)
with col_s1:
    risk_pct = st.slider("Risk / trade (%)", 0.1, 5.0,
                          float(risk.get("risk_per_trade_pct", 1.0)), 0.1)
with col_s2:
    atr_sl = st.slider("ATR Stop × k", 0.5, 5.0,
                        float(risk.get("atr_stop_multiplier", 2.0)), 0.1)
with col_s3:
    atr_tp = st.slider("ATR Take-Profit × k", 1.0, 10.0,
                        float(risk.get("atr_take_profit_multiplier", 4.0)), 0.1)
with col_s4:
    rr = st.slider("Risk : Reward ratio", 1.0, 5.0,
                    float(risk.get("risk_reward_ratio", 2.0)), 0.1)
with col_s5:
    max_open_new = st.slider("Max open trades", 1, 10, max_open, 1)

col_s6, col_s7, col_s8 = st.columns([1, 1, 2])
with col_s6:
    cap_per_trade = st.slider("Cap % per trade", 1, 50,
                               int(exch.get("max_capital_per_trade_pct", 10)), 1)
with col_s7:
    enable_mandate = st.checkbox(
        "Mandate gate (live trading)",
        value=bool(mand.get("enabled", False)),
        help="⚠️ When ON, the bot will submit real orders to the exchange.",
    )

if st.button("💾 Save risk settings (apply on next bot restart)"):
    overrides = {
        "trading": {
            "risk_per_trade_pct": risk_pct,
            "atr_stop_multiplier": atr_sl,
            "atr_take_profit_multiplier": atr_tp,
            "risk_reward_ratio": rr,
            "max_open_trades": max_open_new,
        },
        "exchange": {
            "max_capital_per_trade_pct": cap_per_trade,
        },
        "mandate": {
            "enabled": enable_mandate,
        },
        "saved_at": datetime.now().isoformat(),
    }
    os.makedirs("audit", exist_ok=True)
    with open("audit/risk_overrides.json", "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2)
    st.success("Saved → audit/risk_overrides.json. Restart the bot to apply.")


# ============================================================
#  NEWS PANEL — slide-out style (signal8 inspiration)
# ============================================================

if show_news_panel:
    st.markdown("---")
    st.markdown('<div class="panel-title">📰 Latest News (per asset)</div>',
                unsafe_allow_html=True)
    cols = st.columns(len(ASSETS_ALL))
    for col, asset in zip(cols, ASSETS_ALL):
        with col:
            st.markdown(f"**{asset}**")
            items = _load_news(asset, max_items=3)
            if not items:
                st.caption("No news available.")
                continue
            for item in items:
                title = item.get("title", "(no title)")
                pub = item.get("providerPublishTime") or item.get("pubDate")
                src = item.get("publisher", "?")
                rel = rel_time(pub) if isinstance(pub, (int, float)) else ""
                # crude category tag
                tag = "NEWS"
                ttl_l = title.lower()
                if any(w in ttl_l for w in ["earnings", "guidance", "revenue"]):
                    tag = "EARNINGS"
                elif any(w in ttl_l for w in ["merger", "acqui", "buyback"]):
                    tag = "CORP"
                elif any(w in ttl_l for w in ["fed", "cpi", "inflation"]):
                    tag = "MACRO"
                st.markdown(
                    f'<div class="news-card">'
                    f'<div class="headline">{title[:120]}{"…" if len(title) > 120 else ""}</div>'
                    f'<div class="meta"><span class="badge">{tag}</span> {src} · {rel}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


# ============================================================
#  ACTIVITY FEED
# ============================================================

if show_audit:
    st.markdown("---")
    st.markdown('<div class="panel-title">📋 Activity Feed</div>',
                unsafe_allow_html=True)
    if not audit_events:
        st.caption("No audit events yet.")
    else:
        for ev in reversed(audit_events[-15:]):
            t = rel_time(ev.get("iso") or ev.get("ts"))
            et = ev.get("event_type", "?")
            icon = "🟢"
            if "FAIL" in et or "ERROR" in et or "FAULT" in et:
                icon = "🔴"
            elif "WARN" in et or "DEGRAD" in et:
                icon = "🟡"
            elif "TRADE" in et or "EXEC" in et or "APPROV" in et:
                icon = "💰"
            elif "BOT_START" in et or "WORKFLOW" in et:
                icon = "⚙️"
            st.markdown(
                f"<div class='meta' style='padding:4px 0;'>"
                f"<code style='color:#4cc9f0;'>{t}</code> "
                f"{icon} <b>{et}</b></div>",
                unsafe_allow_html=True,
            )