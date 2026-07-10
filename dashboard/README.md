# Sprint 46B — Guaritradbot Dashboard

A Next.js 14 dashboard for the **Guaritradbot** trading system. Consumes the
REST + WebSocket API exposed by the bot's HTTP layer
(`src/api/server.py` in the bot repo, see Sprint 46A commit `f805014`).

## Stack

- **Next.js 14** (App Router) + **TypeScript** + **Tailwind CSS**
- **SWR** for REST polling with revalidation
- **recharts** for the price + equity charts
- Native **WebSocket** for the live positions/audit stream
- Bearer-token auth stored in `localStorage` (12h TTL)

## Pages

- `/` — overview KPIs, open positions, equity curve
- `/positions` — full positions table with manual close
- `/positions/[id]` — single-position detail with price chart + SL/TP/Entry overlays
- `/audit` — filterable audit log
- `/allocation` — asset-class drift, recession stress test, CVaR, correlation matrix
- `/login` — password gate (same `DASHBOARD_PASSWORD` env on the bot)

## Local dev

```bash
# 1. In the bot repo, start the API:
DASHBOARD_PASSWORD=changeme python -m uvicorn src.api.server:app --host 0.0.0.0 --port 8080

# 2. In this repo:
cp .env.example .env.local
# .env.local: NEXT_PUBLIC_API_URL=http://localhost:8080
npm install
npm run dev   # http://localhost:3000
```

## Build

```bash
npm run build
```

`next build` runs `tsc` (and catches deep type errors that `tsc --noEmit`
might miss). Production output goes to `.next/`.

## Deploy

- **Vercel** (recommended) — `vercel link` once, then `vercel --prod`.
  Set `NEXT_PUBLIC_API_URL` in the project env vars to the public API URL.
- **Coolify / self-hosted** — `npm run build && npm start` behind a reverse
  proxy; set `DASHBOARD_CORS_ORIGINS` on the bot to allow this dashboard's
  origin.

## CORS (bot side)

The bot's CORS is open by default (`DASHBOARD_CORS_ORIGINS=*`). In prod,
narrow it:

```bash
DASHBOARD_CORS_ORIGINS=https://dashboard.guaritradbot.com
```

## Design

Dark, warm, trading-desk aesthetic — `ink` for surfaces, `gold` for primary
actions, `gain`/`loss` for P&L. Numbers in JetBrains Mono with tabular-nums
so they line up across rows.
