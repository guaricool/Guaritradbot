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

# Non-blocking auto-refresh (replaces time.sleep + st.rerun that delayed
# the first render by N seconds).
from streamlit_autorefresh import st_autorefresh

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
  .hero .sub { color: #d4dbe9; font-size: 0.95rem; font-weight: 500; margin-top: 6px; }
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
    color: #c5cce0;
    font-size: 0.74rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 6px;
  }
  .kpi-value {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.4rem;
    line-height: 1.1;
  }
  .kpi-delta { font-size: 0.74rem; color: #c5cce0; font-weight: 500; margin-top: 3px; }
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
  .signal-card .meta { color: #c5cce0; font-size: 0.8rem; font-weight: 500; }
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
  .news-card .meta { color: #c5cce0; font-size: 0.74rem; font-weight: 500; }

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
    font-size: 0.95rem;
    font-weight: 800;
    color: #e8edf7;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid #2a3050;
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

  /* =====================================================
     v4 DESIGN OVERHAUL — better contrast & legibility
     ===================================================== */

  /* Better base text contrast (was #e6e8ee → #e8edf7) */
  html, body, [data-testid="stAppViewContainer"], [class*="css"] {
    color: #e8edf7;
  }
  p, li, span, div, label { color: inherit; }

  /* Main app container — subtle vignette so cards pop */
  .stApp {
    background:
      radial-gradient(ellipse at top, rgba(76, 201, 240, 0.06) 0%, transparent 50%),
      linear-gradient(180deg, #0a0e1c 0%, #131829 100%);
  }

  /* ---------- SIDEBAR ---------- */
  section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0a0e1c 0%, #0d1224 100%);
    border-right: 1px solid #2a3050;
  }
  section[data-testid="stSidebar"] > div:first-child {
    padding-top: 1rem;
  }
  /* Sidebar section titles (Cockpit Controls / Config / Search) */
  section[data-testid="stSidebar"] .stMarkdown h3 {
    color: #4cc9f0 !important;
    font-weight: 800 !important;
    font-size: 0.85rem !important;
    letter-spacing: 0.12em !important;
    text-transform: uppercase !important;
    padding-bottom: 6px;
    margin-top: 0.4rem !important;
    margin-bottom: 0.6rem !important;
    border-bottom: 1px solid rgba(76, 201, 240, 0.25);
  }
  /* Sidebar widget labels (Auto-refresh, checkbox text, etc.) */
  section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
  section[data-testid="stSidebar"] label,
  section[data-testid="stSidebar"] label p,
  section[data-testid="stSidebar"] .stMarkdown p,
  section[data-testid="stSidebar"] .stMarkdown li,
  section[data-testid="stSidebar"] .stCaption,
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    color: #d4dbe9 !important;
    font-weight: 500 !important;
    font-size: 0.88rem !important;
  }
  /* The bold "Mode / Exchange / Risk/trade / …" labels stay bright */
  section[data-testid="stSidebar"] .stMarkdown ul li strong {
    color: #e8edf7 !important;
    font-weight: 700 !important;
  }
  /* The badge values like `auto`, `binance.us`, `1.0%`, `OFF` */
  section[data-testid="stSidebar"] .stMarkdown code {
    background: rgba(76, 201, 240, 0.18) !important;
    color: #4cc9f0 !important;
    font-weight: 700 !important;
    font-size: 0.82rem !important;
    padding: 2px 8px;
    border-radius: 5px;
    border: 1px solid rgba(76, 201, 240, 0.3);
  }
  /* Make sidebar bullets spacing nicer */
  section[data-testid="stSidebar"] .stMarkdown ul {
    padding-left: 1.1rem;
    margin-bottom: 0.5rem;
  }
  section[data-testid="stSidebar"] .stMarkdown ul li {
    padding: 3px 0;
    line-height: 1.5;
  }
  /* Checkbox / radio boxes — ensure visible check */
  section[data-testid="stSidebar"] [data-baseweb="checkbox"] span,
  section[data-testid="stSidebar"] [data-baseweb="checkbox"] div {
    border-color: #4cc9f0 !important;
  }

  /* Sidebar selectbox + textinput fields */
  section[data-testid="stSidebar"] [data-baseweb="select"] > div,
  section[data-testid="stSidebar"] [data-baseweb="input"] > div,
  section[data-testid="stSidebar"] input {
    background: #161b2e !important;
    color: #e8edf7 !important;
    border: 1px solid #353f6a !important;
    font-weight: 500 !important;
  }
  /* Brighten the placeholder text in sidebar inputs (was Streamlit's default dim gray) */
  section[data-testid="stSidebar"] input::placeholder,
  section[data-testid="stSidebar"] textarea::placeholder,
  section[data-testid="stSidebar"] [data-baseweb="input"] input::placeholder {
    color: #b8c1d6 !important;
    opacity: 1 !important;
    font-weight: 600 !important;
  }
  /* Focus ring on inputs (cyan glow) */
  section[data-testid="stSidebar"] input:focus,
  section[data-testid="stSidebar"] [data-baseweb="input"] input:focus {
    border-color: #4cc9f0 !important;
    box-shadow: 0 0 0 3px rgba(76, 201, 240, 0.25) !important;
    outline: none !important;
  }

  /* ---------- MAIN AREA inputs + text inputs (search bar, etc.) ---------- */
  [data-baseweb="input"] input,
  [data-baseweb="textarea"] textarea {
    background: #161b2e !important;
    color: #e8edf7 !important;
    border: 1px solid #353f6a !important;
  }
  [data-baseweb="input"] input::placeholder,
  [data-baseweb="textarea"] textarea::placeholder {
    color: #b8c1d6 !important;
    opacity: 1 !important;
    font-weight: 500 !important;
  }

  /* ---------- MAIN AREA widget labels ---------- */
  .stSlider [data-testid="stWidgetLabel"] p,
  .stCheckbox [data-testid="stWidgetLabel"] p {
    color: #d4dbe9 !important;
    font-weight: 600 !important;
    font-size: 0.88rem !important;
  }

  /* ---------- TABS (signal selectors) ---------- */
  .stTabs [data-baseweb="tab-list"] {
    background: transparent;
    gap: 4px;
    border-bottom: 1px solid #2a3050;
  }
  .stTabs [data-baseweb="tab"] {
    color: #b8c1d6 !important;
    font-weight: 700 !important;
    background: transparent !important;
    padding: 8px 16px;
    border-radius: 6px 6px 0 0;
    font-size: 0.92rem;
  }
  .stTabs [data-baseweb="tab"]:hover {
    color: #e8edf7 !important;
    background: rgba(76, 201, 240, 0.06) !important;
  }
  .stTabs [aria-selected="true"][data-baseweb="tab"] {
    color: #4cc9f0 !important;
    background: rgba(76, 201, 240, 0.12) !important;
    border-bottom: 2px solid #4cc9f0 !important;
  }
  .stTabs [data-baseweb="tab-highlight"] {
    background-color: #4cc9f0 !important;
  }
  .stTabs [data-baseweb="tab-border"] { display: none !important; }

  /* ---------- BUTTONS (Save risk settings, filter chips, mode toggle) ---------- */
  .stButton > button {
    background: linear-gradient(135deg, #1d2342 0%, #2a3258 100%) !important;
    color: #ffffff !important;
    border: 1px solid #353f6a !important;
    font-weight: 800 !important;
    font-family: 'JetBrains Mono', monospace !important;
    letter-spacing: 0.04em;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
    transition: all 0.15s;
    opacity: 1 !important;
  }
  .stButton > button:hover {
    background: linear-gradient(135deg, #2a3258 0%, #353f6a 100%) !important;
    border-color: #4cc9f0 !important;
    color: #ffffff !important;
    transform: translateY(-1px);
  }
  .stButton > button:focus {
    border-color: #4cc9f0 !important;
    box-shadow: 0 0 0 3px rgba(76, 201, 240, 0.3) !important;
  }
  /* Disabled buttons — dark bg + dim text (NOT faded opacity, which kills
     contrast). Ensures "Stay PAPER" / "Stay LIVE" remain readable when
     one of the two is disabled. */
  .stButton > button:disabled,
  .stButton > button[disabled] {
    background: #0d1224 !important;
    color: #6b7390 !important;
    border: 1px solid #2a3050 !important;
    opacity: 1 !important;
    text-shadow: none !important;
    cursor: not-allowed !important;
  }

  /* The "Save risk settings" primary button — make it pop more */
  .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #06d6a0 0%, #05b386 100%) !important;
    color: #0a0e1c !important;
    border: none !important;
    font-weight: 800 !important;
    text-shadow: none !important;
  }
  .stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #05b386 0%, #06d6a0 100%) !important;
    box-shadow: 0 4px 14px rgba(6, 214, 160, 0.35) !important;
  }

  /* ---------- DATAFRAMES (Open Positions) ---------- */
  .stDataFrame {
    border: 1px solid #353f6a;
    border-radius: 8px;
    overflow: hidden;
  }

  /* ---------- EXPANDER (Raw signal JSON) ---------- */
  details summary,
  .streamlit-expanderHeader {
    background: #1a1f3a !important;
    color: #d4dbe9 !important;
    border: 1px solid #2a3050 !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
  }
  .streamlit-expanderHeader:hover {
    background: #1d2342 !important;
    color: #4cc9f0 !important;
  }

  /* ---------- ALERTS / INFO ---------- */
  .stAlert, [data-testid="stAlert"] {
    background: rgba(76, 201, 240, 0.08) !important;
    border: 1px solid rgba(76, 201, 240, 0.3) !important;
    color: #d4dbe9 !important;
  }

  /* ---------- KPI CARD REFINEMENTS ---------- */
  .kpi-card {
    background: linear-gradient(180deg, #161b2e 0%, #1a1f3a 100%);
    border: 1px solid #353f6a;
    border-radius: 12px;
    padding: 14px 16px;
    height: 100%;
    box-shadow: 0 1px 0 rgba(255, 255, 255, 0.04) inset,
                0 6px 18px rgba(0, 0, 0, 0.25);
    transition: border-color 0.15s, transform 0.15s;
  }
  .kpi-card:hover {
    border-color: #4cc9f0;
    transform: translateY(-2px);
  }
  .kpi-value {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.55rem;
    line-height: 1.1;
    letter-spacing: -0.02em;
  }

  /* ---------- COUNTDOWN ---------- */
  .countdown {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.4rem;
    color: #4cc9f0;
    text-align: center;
    padding: 14px;
    background: linear-gradient(135deg, rgba(76, 201, 240, 0.12) 0%, rgba(76, 201, 240, 0.04) 100%);
    border: 1px solid rgba(76, 201, 240, 0.35);
    border-radius: 10px;
    letter-spacing: 0.04em;
  }
  .countdown small, .countdown span {
    color: #c5cce0 !important;
    font-weight: 600;
  }

  /* ---------- TICKER BAR UPGRADE ---------- */
  .ticker-bar {
    background: linear-gradient(90deg, #0a0e1c 0%, #131829 50%, #0a0e1c 100%);
    border-top: 1px solid #353f6a;
    border-bottom: 1px solid #353f6a;
    padding: 10px 14px;
    display: flex;
    gap: 12px;
    overflow-x: auto;
    white-space: nowrap;
    margin: 0 -1rem 14px -1rem;
  }
  .ticker-cell {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 6px 14px;
    border-radius: 8px;
    background: rgba(26, 31, 58, 0.6);
    border: 1px solid #353f6a;
    transition: all 0.2s;
  }
  .ticker-cell:hover {
    background: #1d2342;
    border-color: #4cc9f0;
  }
  .ticker-symbol {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    color: #e8edf7;
    font-size: 0.95rem;
    letter-spacing: 0.03em;
  }
  .ticker-price {
    font-family: 'JetBrains Mono', monospace;
    color: #e8edf7;
    font-size: 0.88rem;
    font-weight: 700;
  }
  .ticker-delta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.82rem;
    font-weight: 800;
  }

  /* ---------- SIGNAL CARDS REFINEMENT ---------- */
  .signal-card {
    background: #1a1f3a;
    border: 1px solid #353f6a;
    border-left: 4px solid #4cc9f0;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 10px;
    transition: transform 0.2s, border-color 0.2s;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
  }
  .signal-card:hover {
    transform: translateX(4px);
    border-color: #4cc9f0;
  }
  .signal-card.long  { border-left-color: #06d6a0; }
  .signal-card.short { border-left-color: #f72585; }
  .signal-card .asset {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.08rem;
    color: #e8edf7;
  }
  .signal-card .conf {
    display: inline-block;
    background: rgba(76, 201, 240, 0.18);
    color: #4cc9f0;
    border: 1px solid rgba(76, 201, 240, 0.4);
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 0.74rem;
    font-weight: 800;
  }
  .signal-card .conf.high { background: rgba(6, 214, 160, 0.2); color: #06d6a0; border-color: rgba(6, 214, 160, 0.5); }
  .signal-card .conf.low  { background: rgba(247, 37, 133, 0.2); color: #f72585; border-color: rgba(247, 37, 133, 0.5); }

  .catalyst {
    font-size: 0.86rem;
    color: #d4dbe9;
    line-height: 1.55;
    margin-top: 8px;
  }
  .catalyst .bullet { color: #06d6a0; margin-right: 6px; font-weight: 800; }

  /* ---------- NEWS CARD ---------- */
  .news-card {
    background: #161b2e;
    border: 1px solid #353f6a;
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 10px;
    transition: transform 0.15s, border-color 0.15s;
  }
  .news-card:hover {
    transform: translateX(2px);
    border-color: #4cc9f0;
  }
  .news-card .headline {
    color: #e8edf7;
    font-weight: 700;
    font-size: 0.92rem;
    line-height: 1.35;
    margin-bottom: 6px;
  }
  .news-card .badge {
    display: inline-block;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 0.7rem;
    color: #4cc9f0;
    background: rgba(76, 201, 240, 0.18);
    border: 1px solid rgba(76, 201, 240, 0.4);
    padding: 2px 7px;
    border-radius: 4px;
    margin-right: 6px;
    letter-spacing: 0.05em;
  }

  /* ---------- PANEL REFINEMENT ---------- */
  .panel {
    background: #131829;
    border: 1px solid #353f6a;
    border-radius: 10px;
    padding: 16px 18px;
    margin-bottom: 12px;
    color: #d4dbe9;
  }

  /* ---------- HERO REFINEMENT ---------- */
  .hero {
    background: linear-gradient(135deg, #1a1f3a 0%, #2d1b4e 50%, #1a1f3a 100%);
    border: 1px solid #353f6a;
    border-radius: 14px;
    padding: 22px 26px;
    margin-bottom: 16px;
    position: relative;
    overflow: hidden;
  }
  .hero::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    background: radial-gradient(circle at 20% 50%, rgba(76, 201, 240, 0.18), transparent 60%),
                radial-gradient(circle at 80% 50%, rgba(247, 37, 133, 0.14), transparent 60%);
    pointer-events: none;
  }
  .hero h1 {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 800;
    font-size: 1.95rem;
    margin: 0;
    background: linear-gradient(90deg, #06d6a0, #4cc9f0, #f72585);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.02em;
  }

  /* ---------- SCROLLBAR ---------- */
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: #0a0e1c; }
  ::-webkit-scrollbar-thumb { background: #353f6a; border-radius: 5px; }
  ::-webkit-scrollbar-thumb:hover { background: #4cc9f0; }

  /* ---------- DIVIDER (--- in markdown) ---------- */
  hr {
    border-color: #2a3050 !important;
    margin: 14px 0 !important;
  }
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
    """Load latest bot state.

    Sprint 9 fix: bot writes to `audit/latest_state.json` so the dashboard
    container (which only shares the `bot_audit` volume, not `/app/`) can
    see the live state. Falls back to top-level `latest_state.json` for
    backwards compatibility.
    """
    return _load_json("audit/latest_state.json") or _load_json("latest_state.json")


@st.cache_data(ttl=10)
def _load_positions_cached():
    """Load open/closed positions.

    Sprint 11 fix: try audit/positions.json first (shared via bot_audit
    volume), then fall back to legacy data_store/positions.json which
    lives only in the bot container.
    """
    data = _load_json("audit/positions.json")
    if data:
        return data.get("positions", [])
    return _load_json("data_store/positions.json").get("positions", [])


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


@st.cache_data(ttl=30)
def _live_prices(assets: tuple) -> dict:
    """Fetch live last prices + prev-close via yfinance fast_info.

    Cached 30s so we don't hammer the API on every refresh. Falls back to a
    1h history download if fast_info is missing the field. Used by the
    ticker bar AND the Top Movers panel so prices are always live
    regardless of when the bot last ran a cycle.
    """
    out: dict = {}
    try:
        import yfinance as yf
        from src.data.yf_safe import safe_yf_download  # warm up curl_cffi session
        for asset in assets:
            try:
                t = yf.Ticker(asset)
                fi = getattr(t, "fast_info", None) or {}
                price = fi.get("lastPrice") or fi.get("last_price")
                prev = fi.get("previousClose") or fi.get("previous_close")
                if price is None:
                    # fallback: 2d/1h history
                    hist = t.history(period="2d", interval="1h")
                    if not hist.empty:
                        price = float(hist["Close"].iloc[-1])
                        if prev is None and len(hist) >= 2:
                            prev = float(hist["Close"].iloc[-2])
                if price is not None:
                    out[asset] = {
                        "price": float(price),
                        "prev": float(prev) if prev else None,
                    }
            except Exception:
                continue
    except Exception:
        pass
    return out


@st.cache_data(ttl=15)
def _live_binance_balance() -> dict | None:
    """Fetch LIVE USD/USDT balance from binance.us via ccxt.

    Used as a fallback when the bot hasn't written a fresh risk_evaluation
    yet (or its value is 0 / missing). Cached 15s so we don't spam the API.
    Returns dict {free, total, source} or None on failure.
    """
    api_key = os.getenv("BINANCE_API_KEY")
    secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not secret:
        return None
    try:
        import ccxt
        exch = ccxt.binanceus({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
        })
        bal = exch.fetch_balance()
        out = {"free": 0.0, "total": 0.0, "source": "binance.us (live)"}
        for sym in ("USD", "USDT", "BUSD", "USDC"):
            info = bal.get(sym)
            if isinstance(info, dict):
                free = float(info.get("free") or 0)
                total = float(info.get("total") or 0)
                if total > 0:
                    out["free"] = free
                    out["total"] = total
                    break
        return out
    except Exception:
        return None


@st.cache_data(ttl=5)
def _live_ohlcv(asset: str, interval: str = "15m", period: str = "5d") -> pd.DataFrame:
    """Fetch live OHLCV candles via yfinance.

    Sprint 13: Used by the live tick stream + the per-asset chart to show
    REAL-TIME price action, not the stale bot-cache CSVs (which only
    update on each hourly cycle).

    interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h
    period:   1d, 5d, 1mo, etc. (must match interval)

    Cached 5s — same as default auto-refresh. Returns empty DataFrame on
    failure.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(asset)
        df = t.history(period=period, interval=interval)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(how="all")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3)
def _live_tick(asset: str) -> dict | None:
    """Fetch a single real-time tick (last price + intraday change).

    Sprint 13: Used for the live tick stream panel that updates every
    auto-refresh. Cached 3s so multiple widgets in the same refresh
    cycle share one API call.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(asset)
        fi = getattr(t, "fast_info", None) or {}
        price = fi.get("lastPrice") or fi.get("last_price")
        prev = fi.get("previousClose") or fi.get("previous_close")
        day_high = fi.get("dayHigh") or fi.get("day_high")
        day_low = fi.get("dayLow") or fi.get("day_low")
        year_high = fi.get("yearHigh") or fi.get("year_high")
        year_low = fi.get("yearLow") or fi.get("year_low")
        volume = fi.get("lastVolume") or fi.get("last_volume") or fi.get("threeMonthAverageVolume")
        if price is None:
            hist = t.history(period="1d", interval="1m")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                if prev is None and len(hist) >= 2:
                    prev = float(hist["Close"].iloc[0])
        if price is None:
            return None
        return {
            "price": float(price),
            "prev_close": float(prev) if prev else None,
            "change": float(price - prev) if prev else 0.0,
            "change_pct": ((price - prev) / prev * 100) if prev else 0.0,
            "day_high": float(day_high) if day_high else None,
            "day_low": float(day_low) if day_low else None,
            "year_high": float(year_high) if year_high else None,
            "year_low": float(year_low) if year_low else None,
            "volume": float(volume) if volume else None,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception:
        return None


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
    # ==========================================================
    # ⚡ MODE TOGGLE — PAPER / LIVE (Sprint 12)
    # ==========================================================
    # Load mandate status from config + last override
    config = _load_yaml("config.yaml")
    mand_from_config = config.get("mandate", {}).get("enabled", False)
    mode_override = _load_json("audit/mode_override.json")
    effective_mandate = bool(mand_from_config) or bool(mode_override.get("mandate_enabled", False))
    is_live = effective_mandate
    is_paper = not is_live

    # Big visual badge for current mode
    if is_live:
        mode_color = "#06d6a0"   # green
        mode_icon = "🟢"
        mode_label = "LIVE TRADING"
        mode_subtitle = "Real money on binance.us"
    else:
        mode_color = "#ffd166"   # yellow
        mode_icon = "🟡"
        mode_label = "PAPER MODE"
        mode_subtitle = "Fake money — signals execute but no real orders"

    st.markdown(
        f'<div style="background:linear-gradient(135deg, {mode_color}33 0%, {mode_color}11 100%); '
        f'border:2px solid {mode_color}; border-radius:10px; padding:14px 16px; '
        f'text-align:center; margin-bottom:12px;">'
        f'<div style="font-size:1.6rem; font-weight:800; color:{mode_color}; letter-spacing:0.05em;">'
        f'{mode_icon} {mode_label}</div>'
        f'<div style="font-size:0.78rem; color:#c5cce0; margin-top:4px;">{mode_subtitle}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # The toggle itself — single big button
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "🟡 PAPER" if is_live else "✓ Stay PAPER",
            use_container_width=True,
            disabled=is_paper,
            help="Switch to paper mode (no real orders)",
            key="toggle_paper",
        ):
            # Write override + trigger bot restart
            os.makedirs("audit", exist_ok=True)
            with open("audit/mode_override.json", "w", encoding="utf-8") as f:
                json.dump({
                    "mandate_enabled": False,
                    "switched_at": datetime.now().isoformat(),
                    "switched_by": "dashboard",
                }, f, indent=2)
            st.success("✓ Switched to PAPER. Bot restart triggered.")
            st.info("ℹ️ Restarting bot in 3s...")
            time.sleep(2)
            st.rerun()

    with col_b:
        if st.button(
            "🟢 LIVE" if is_paper else "✓ Stay LIVE",
            use_container_width=True,
            disabled=is_live,
            help="⚠️ Switch to live trading — real orders on binance.us",
            key="toggle_live",
        ):
            # Write override + trigger bot restart
            os.makedirs("audit", exist_ok=True)
            with open("audit/mode_override.json", "w", encoding="utf-8") as f:
                json.dump({
                    "mandate_enabled": True,
                    "switched_at": datetime.now().isoformat(),
                    "switched_by": "dashboard",
                }, f, indent=2)
            st.warning("⚠️ Switching to LIVE — real money will be at risk.")
            st.info("ℹ️ Restarting bot in 3s...")
            time.sleep(2)
            st.rerun()

    st.caption("Toggle takes effect on next bot cycle (≤60s).")

    st.markdown("---")
    st.markdown("### ⚙️ Cockpit Controls")

    refresh_sec = st.selectbox(
        "Auto-refresh (live ticks)",
        options=[2, 5, 10, 15, 30, 60, 0],
        index=1,
        format_func=lambda x: "Off" if x == 0 else f"{x}s",
        help="How often the dashboard refreshes. 5s default = real-time feel.",
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
    st.markdown("### 🎚️ Quick Risk")
    st.caption("Adjust risk parameters without scrolling. "
               "Click **Save** to persist; bot restarts to apply.")
    sidebar_risk_pct = st.slider(
        "Risk per trade (%)",
        0.1, 5.0,
        float(risk.get("risk_per_trade_pct", 1.0)), 0.1,
        key="sidebar_risk_pct",
    )
    sidebar_cap_pct = st.slider(
        "Cap per trade (%)",
        1, 50,
        int(exch.get("max_capital_per_trade_pct", 10)), 1,
        key="sidebar_cap_pct",
    )
    sidebar_max_open = st.slider(
        "Max open trades",
        1, 10,
        int(risk.get("max_open_trades", 5)), 1,
        key="sidebar_max_open",
    )
    sidebar_mandate = st.checkbox(
        "🟢 Mandate gate (LIVE trading)",
        value=bool(mand.get("enabled", False)),
        key="sidebar_mandate",
        help="When ON, the bot submits real orders to binance.us. "
             "⚠️ Real money at risk.",
    )

    if st.button("💾 Save Quick Risk", use_container_width=True, key="sidebar_save"):
        overrides = {
            "trading": {
                "risk_per_trade_pct": sidebar_risk_pct,
                "max_open_trades": sidebar_max_open,
            },
            "exchange": {
                "max_capital_per_trade_pct": sidebar_cap_pct,
            },
            "mandate": {
                "enabled": sidebar_mandate,
            },
            "saved_at": datetime.now().isoformat(),
        }
        os.makedirs("audit", exist_ok=True)
        with open("audit/risk_overrides.json", "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)
        st.success("✓ Saved → audit/risk_overrides.json")
        st.info("ℹ️ Restart the bot to apply. Click *Restart bot* below.")

    st.markdown("---")
    st.markdown("### 🔭 Search")
    search_q = st.text_input(
        "Filter signals / positions",
        placeholder="SPY, BTC, EMA, …",
    ).strip().upper()

    with st.expander("🔌 Connect Binance.us (live trading)", expanded=False):
        st.markdown(
            """
**To trade real money, the bot needs your binance.us API key.**

1. Go to **binance.us → Account → API Management**
2. Create a new API key
   - ✅ Enable **Spot & Margin Trading**
   - ❌ Don't enable Withdrawals
   - Add your VPS IP `13.140.181.29` to the whitelist
3. Copy the **API Key** and **Secret** into the VPS env:
   ```bash
   ssh root@13.140.181.29
   # Edit the Coolify app env vars:
   #   BINANCE_API_KEY=...
   #   BINANCE_API_SECRET=...
   ```
   Or in the Coolify UI: *Resources → guaritradbot → Environment*
4. Toggle **🟢 Mandate gate (LIVE trading)** above → **Save**
5. **Restart the bot** (Coolify → *Restart* on the resource)
6. Verify in the audit log:
   ```bash
   tail -f /data/coolify/applications/wyn2ah6rflg6ufwzpvzk436f/audit/audit.jsonl
   ```
   You should see `BROKER_CONNECTED` events with your balance.

> ⚠️ Start with **max $20** on the account (see `max_position_usd`
> in config.yaml). The mandate gate blocks anything bigger.
"""
        )

    st.markdown("---")
    st.caption(f"🕒 {datetime.now().strftime('%H:%M:%S')} CT")


# Auto-refresh — non-blocking. Triggers a soft rerun every N seconds WITHOUT
# delaying the first render with time.sleep. The interval is in milliseconds.
if refresh_sec and refresh_sec > 0:
    st_autorefresh(interval=refresh_sec * 1000, key="guaritradbot_autorefresh")


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

# Account balance: prefer the bot's last risk_evaluation. If that's
# missing/zero (e.g. bot hasn't run yet, or was restarted), fall back to
# a live fetch from binance.us so the dashboard always shows the real
# number. Avoid the misleading hardcoded $100 default.
_bot_balance = float(risk_eval.get("account_balance") or 0.0)
_bot_balance_source = risk_eval.get("balance_source", "")
if _bot_balance > 0:
    balance = _bot_balance
    balance_source = _bot_balance_source or "bot"
else:
    live = _live_binance_balance()
    if live and live["total"] > 0:
        balance = live["total"]
        balance_source = live["source"]
    elif live and live["free"] > 0:
        balance = live["free"]
        balance_source = live["source"]
    else:
        balance = 0.0
        balance_source = "unavailable"
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

# Live prices from yfinance (refreshes every 30s).
# Used as a fallback so the ticker bar + Top Movers always show prices,
# even when the bot hasn't run its latest cycle yet.
live_prices = _live_prices(tuple(ASSETS_ALL))


def _get_price(asset: str) -> float | None:
    """Prefer bot's market_data (close of last bar); fall back to live yfinance."""
    px = asset_last_price(asset, market_data_raw)
    if px is not None:
        return px
    lp = live_prices.get(asset)
    return lp["price"] if lp else None


def _get_prev_close(asset: str) -> float | None:
    """Prefer 4h-back from bot data; fall back to yfinance previousClose."""
    prev = asset_prev_price(asset, market_data_raw, lookback=4)
    if prev is not None:
        return prev
    lp = live_prices.get(asset)
    return lp["prev"] if lp else None


# ============================================================
#  STICKY TICKER BAR (signal8-inspired)
# ============================================================

ticker_cells = []
for asset in ASSETS_ALL:
    px = _get_price(asset)
    prev = _get_prev_close(asset)
    delta = None
    delta_pct = None
    if px is not None and prev is not None:
        delta = px - prev
        delta_pct = (delta / prev) * 100 if prev else 0.0
    cls = color_class(delta_pct)
    arrow = "▲" if (delta_pct or 0) >= 0 else "▼"
    if px is not None:
        cell = (
            f'<div class="ticker-cell">'
            f'<span class="ticker-symbol">{asset}</span>'
            f'<span class="ticker-price">${px:,.2f}</span>'
        )
    else:
        cell = (
            f'<div class="ticker-cell">'
            f'<span class="ticker-symbol">{asset}</span>'
            f'<span class="ticker-price" style="color:#c5cce0;">—</span>'
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
    st.markdown(
        kpi_with_spark(
            "Balance", fmt_usd(balance),
            delta=f"<span style='color:#4cc9f0;'>src: {balance_source}</span>",
        ),
        unsafe_allow_html=True,
    )
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
                showarrow=False, font=dict(size=14, color="#c5cce0"),
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
    threshold_val = float(risk.get("risk_per_trade_pct", 1.0)) * max_open
    fig_gauge = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=exposure_pct,
        delta={
            "reference": threshold_val,
            "increasing": {"color": "#f72585", "symbol": "▲"},
            "decreasing": {"color": "#06d6a0", "symbol": "▼"},
            "font": {"color": "#f72585", "size": 18, "family": "JetBrains Mono"},
        },
        gauge={
            "axis": {
                "range": [0, 100],
                "tickcolor": "#e8edf7",
                "tickfont": {"color": "#e8edf7", "size": 13,
                             "family": "JetBrains Mono"},
                "tickwidth": 2,
            },
            "bar": {"color": "#4cc9f0", "thickness": 0.3},
            "bgcolor": "#161b2e",
            "borderwidth": 0,
            "steps": [
                {"range": [0, 30], "color": "rgba(6, 214, 160, 0.30)"},
                {"range": [30, 70], "color": "rgba(255, 209, 102, 0.30)"},
                {"range": [70, 100], "color": "rgba(247, 37, 133, 0.30)"},
            ],
            "threshold": {
                "line": {"color": "#f72585", "width": 5},
                "thickness": 0.85,
                "value": threshold_val,
            },
        },
        number={
            "suffix": "%",
            "font": {"color": "#e8edf7", "size": 38,
                     "family": "JetBrains Mono"},
        },
        domain={"x": [0, 1], "y": [0, 1]},
    ))
    # Threshold label rendered below the gauge for clarity
    fig_gauge.add_annotation(
        x=0.5, y=-0.02, xref="paper", yref="paper",
        text=f"<b>Max allowed: {threshold_val:.1f}%</b> "
             f"(risk/trade × max open)",
        showarrow=False,
        font=dict(color="#c5cce0", size=11, family="JetBrains Mono"),
        xanchor="center",
    )
    fig_gauge.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e8edf7", family="JetBrains Mono"),
        height=320, margin=dict(l=20, r=20, t=10, b=40),
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
        f'<span style="font-size:0.7rem; color:#c5cce0;">@ {next_run.strftime("%H:%M")} CT</span></div>',
        unsafe_allow_html=True,
    )

    # Also: time since last cycle
    st.markdown(
        f'<div style="text-align:center; margin-top:8px; font-size:0.78rem; color:#c5cce0;">'
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

# ============================================================
# Live Tick Stream panel (Sprint 13)
# Sprint 13: real-time tick-by-tick view of all 5 assets. Updates every
# auto-refresh (5s default). Each card shows: last price, $ change,
# % change, day high/low, year high/low, volume.
# ============================================================
tick_cols = st.columns(len(ASSETS_ALL))
for col, asset in zip(tick_cols, ASSETS_ALL):
    with col:
        tick = _live_tick(asset)
        if tick is None:
            st.markdown(
                f'<div class="panel" style="text-align:center; padding:12px;">'
                f'<div style="color:#8892b0; font-size:0.75rem;">{asset}</div>'
                f'<div style="color:#6b7390; font-size:0.9rem;">— loading —</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            continue
        cls = "pos" if tick["change"] > 0 else ("neg" if tick["change"] < 0 else "neu")
        arrow = "▲" if tick["change"] > 0 else ("▼" if tick["change"] < 0 else "●")
        # Day high/low bar
        dh = tick.get("day_high") or tick["price"]
        dl = tick.get("day_low") or tick["price"]
        day_range_pct = 0.0
        if dh and dl and dh > dl:
            day_range_pct = ((tick["price"] - dl) / (dh - dl)) * 100
            day_range_pct = max(0, min(100, day_range_pct))
        # 52-week position
        yh = tick.get("year_high") or tick["price"]
        yl = tick.get("year_low") or tick["price"]
        ytd_range_pct = 50.0
        if yh and yl and yh > yl:
            ytd_range_pct = ((tick["price"] - yl) / (yh - yl)) * 100
            ytd_range_pct = max(0, min(100, ytd_range_pct))

        st.markdown(
            f'<div class="panel" style="padding:10px 12px;">'
            f'<div style="display:flex; justify-content:space-between; align-items:baseline;">'
            f'<span style="font-family:JetBrains Mono; font-weight:800; color:#c5cce0; font-size:0.85rem;">{asset}</span>'
            f'<span style="font-family:JetBrains Mono; font-size:0.65rem; color:#6b7390;">live</span>'
            f'</div>'
            f'<div style="font-family:JetBrains Mono; font-weight:800; font-size:1.4rem; color:#ffffff; margin:4px 0 2px;">'
            f'${tick["price"]:,.2f}'
            f'</div>'
            f'<div style="font-family:JetBrains Mono; font-size:0.8rem;" class="{cls}">'
            f'{arrow} {tick["change"]:+.2f} ({tick["change_pct"]:+.2f}%)'
            f'</div>'
            f'<div style="margin-top:6px;">'
            f'<div style="display:flex; justify-content:space-between; font-size:0.62rem; color:#8892b0; margin-bottom:1px;">'
            f'<span>L ${dl:,.2f}</span><span>Day H ${dh:,.2f}</span>'
            f'</div>'
            f'<div style="background:#1a1f3a; height:3px; border-radius:2px; overflow:hidden;">'
            f'<div style="background:linear-gradient(90deg, #f72585, #ffd166, #06d6a0); height:100%; '
            f'width:{day_range_pct:.0f}%; border-radius:2px;"></div>'
            f'</div>'
            f'</div>'
            f'<div style="margin-top:6px; font-size:0.62rem; color:#6b7390;">'
            f'52w: ${yl:,.2f} – ${yh:,.2f} ({ytd_range_pct:.0f}%)'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

# Per-asset chart tabs — use LIVE OHLCV (15m, 5d) for tight time-range detail
asset_tabs = st.tabs([f" {a} " for a in ASSETS_ALL])

for tab, asset in zip(asset_tabs, ASSETS_ALL):
    with tab:
        # Sprint 13: live 15m candles from yfinance (5d window) for tight
        # detail. Falls back to bot CSV cache if yfinance fails.
        df = _live_ohlcv(asset, interval="15m", period="5d")
        live_source = True
        if df.empty or len(df) < 5:
            df = _load_csv(asset)
            live_source = False
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
        # Sprint 13: render real candlesticks (not just line) so user sees
        # the actual OHLC of each 15m bar. OHLC columns may be capitalised
        # differently between yfinance and the bot CSVs.
        ohlc_map = {
            "Open": next((c for c in ["Open"] if c in df.columns), None),
            "High": next((c for c in ["High"] if c in df.columns), None),
            "Low":  next((c for c in ["Low"]  if c in df.columns), None),
            "Close": close_col,
        }
        if all(ohlc_map.values()):
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                vertical_spacing=0.03, row_heights=[0.72, 0.28])
            fig.add_trace(go.Candlestick(
                x=x_vals,
                open=df[ohlc_map["Open"]],
                high=df[ohlc_map["High"]],
                low=df[ohlc_map["Low"]],
                close=df[ohlc_map["Close"]],
                increasing=dict(line=dict(color="#06d6a0", width=1),
                                fillcolor="rgba(6, 214, 160, 0.6)"),
                decreasing=dict(line=dict(color="#f72585", width=1),
                                fillcolor="rgba(247, 37, 133, 0.6)"),
                name=f"{asset}",
                showlegend=False,
                hovertemplate=f"<b>%{{x}}</b><br>"
                              f"O: %{{open:.2f}} H: %{{high:.2f}} "
                              f"L: %{{low:.2f}} C: %{{close:.2f}}<extra></extra>",
            ), row=1, col=1)
            # Overlays: BUY/SELL markers
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
            # Source badge so user knows if this is live or cached
            source_badge = "🟢 LIVE 15m" if live_source else "🟡 cached"
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccd6f6", family="JetBrains Mono"),
                height=420, margin=dict(l=40, r=20, t=30, b=30),
                xaxis=dict(gridcolor="#1f2640", rangeslider=dict(visible=False)),
                xaxis2=dict(gridcolor="#1f2640"),
                yaxis=dict(gridcolor="#1f2640", title="Price ($)"),
                yaxis2=dict(gridcolor="#1f2640", title="Vol"),
                legend=dict(orientation="h", yanchor="bottom", y=1.02,
                            xanchor="right", x=1, bgcolor="rgba(0,0,0,0)"),
                hovermode="x unified",
                title=dict(
                    text=f"{asset} · {source_badge}",
                    font=dict(size=11, color="#8892b0"),
                    x=0.01, xanchor="left",
                ),
            )
            st.plotly_chart(fig, use_container_width=True, theme=None)
        else:
            # Fallback: line only (no OHLC columns available)
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                vertical_spacing=0.03, row_heights=[0.7, 0.3])
            fig.add_trace(go.Scatter(
                x=x_vals, y=df[close_col], mode="lines",
                line=dict(color="#4cc9f0", width=2),
                name=f"{asset} Close",
                hovertemplate="<b>%{x}</b><br>$%{y:.2f}<extra></extra>",
            ), row=1, col=1)
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#ccd6f6", family="JetBrains Mono"),
                height=380, margin=dict(l=40, r=20, t=10, b=30),
                xaxis=dict(gridcolor="#1f2640"),
                yaxis=dict(gridcolor="#1f2640", title="Price ($)"),
            )
            st.plotly_chart(fig, use_container_width=True, theme=None)


# ============================================================
#  TOP MOVERS — signal8 inspiration
# ============================================================

st.markdown('<div class="panel-title">🔥 Top Movers (4h momentum)</div>',
            unsafe_allow_html=True)

movers = []
for asset in ASSETS_ALL:
    px = _get_price(asset)
    prev = _get_prev_close(asset)
    if px is not None and prev is not None and prev:
        pct = (px - prev) / prev * 100
        spark = spark_series(asset, n=20)
        movers.append((asset, px, pct, spark))
movers.sort(key=lambda m: abs(m[2]), reverse=True)

# Note: latest_state.json omits DataFrames from JSON serialization, so
# market_data_raw is mostly placeholders. movers can legitimately be empty
# on first load (or while the bot is between cycles). Show a graceful
# fallback instead of crashing on st.columns(0).
if not movers:
    st.markdown(
        '<div class="panel" style="text-align:center; padding:18px; color:#c5cce0;">'
        'Top Movers se actualizará cuando el bot termine el próximo ciclo '
        '(cada hora). Mientras tanto, los precios live están en el <b>ticker bar</b> '
        'de arriba y los charts por asset abajo.</div>',
        unsafe_allow_html=True,
    )
else:
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
            """<div class="panel" style="text-align:center; padding:24px; color:#c5cce0;">
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
            """<div class="panel" style="text-align:center; padding:18px; color:#c5cce0;">
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
                f'<div class="panel" style="text-align:center; padding:18px; color:#c5cce0;">'
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