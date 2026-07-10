"""Sprint 46A — Bot HTTP API + WebSocket layer.

Replaces the Streamlit dashboard with a real REST/WS backend that
the new Next.js dashboard (Sprint 46B) can consume. Also keeps the
data access pattern pure: the bot owns the state on disk, the API
layer only reads + exposes it.

Module layout:
  - auth.py    : token-based auth (DASHBOARD_PASSWORD -> bearer token)
  - state.py   : snapshot builder (positions, P&L, audit, mode)
  - server.py  : FastAPI app + routes + WebSocket
"""
