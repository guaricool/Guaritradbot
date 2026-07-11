# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Guaritradbot: a multi-agent trading bot trading crypto (binance.us, via ccxt) and
equities/ETFs (Alpaca), driven by a YAML-defined workflow (`src/workflows/trading_loop.yaml`)
executed by a small custom `WorkflowEngine`. Ships as two Docker services behind
Coolify: the bot itself (`Dockerfile.bot`) and a separate Next.js dashboard
(`Dockerfile.dashboard`, in `dashboard/`) that reads the bot's state over a REST +
WebSocket API the bot exposes in-process (`src/api/`).

## Commands

```bash
# Run one full analysis cycle and exit (paper mode by default)
python main.py --once

# Run as a daemon (hourly analysis cycle + a faster position-monitor loop)
python main.py

# Send a one-shot Telegram test message and exit (verifies notification wiring)
python main.py --test-telegram

# Full test suite
python -m unittest discover tests -v

# Single test file / single test
python -m unittest tests.test_sprint_45_portfolio_gates -v
python -m unittest tests.test_sprint_45_portfolio_gates.SomeTestCase.test_something -v

# GP strategy-evolution CLI (standalone research tool — NOT wired into main.py/the
# live bot loop; only run via this entry point or the tests import it directly)
python -m src.analysis.genetic_programming

# Dashboard (separate Next.js app in dashboard/)
cd dashboard && npm install && npm run dev     # needs the bot's API running locally, see dashboard/README.md
cd dashboard && npm run build                   # next build (also type-checks)

# Full stack via Docker
docker compose up --build
```

Dependencies are pinned in `requirements.txt` (source of truth) and mirrored into
`requirements.lock` for the Docker build — when adding a dependency, update both
or the image build won't pick it up (this has bitten the project before).

## Architecture

### Workflow execution

`main.py` builds an `agents_registry` (`MarketAnalystAgent`, `StrategyAgent`,
`RiskManagerAgent`, `DebateAgent`, `ExecutionAgent`, `NotificationAgent`), loads
`src/workflows/trading_loop.yaml`, and hands both to `WorkflowEngine`
(`src/workflows/engine.py`), which executes the YAML's steps in order, respecting
each step's `depends_on`. Agents communicate both through direct step
input/output (workflow data) and through a shared `EventBus`
(`src/core/event_bus.py`) for cross-cutting notifications (`SYSTEM_ERROR`,
`TRADE_CLOSED`, `POSITION_UPDATE`, etc.) that `NotificationAgent` subscribes to
and forwards to Telegram.

### Two independent scheduling loops (important — don't conflate them)

`main.py` registers TWO jobs on the same global `schedule` library instance that
`EpochScheduler.start()` drives (`scheduler.run_pending()` in a single
`while True` loop — one scheduler instance, two independently-timed jobs):

- **`job_with_monitor`** (hourly, `schedule.run_interval_hours`): the full
  analysis cycle — drawdown kill-switch check, capital-aware asset routing,
  manual dashboard pause check, then (if none of those gate it) the full
  `WorkflowEngine` run (fetch data → generate hypotheses → risk-size → execute →
  debate).
- **`fast_monitor_tick`** (every few minutes, `schedule.fast_monitor_interval_minutes`,
  default 2): ONLY position protection for already-open positions — SL/TP
  polling, OCO reconciliation, smart profit-take on reversal signals, equity
  tracker updates, per-position P&L notifications. Never generates new entries.

This split exists because stop-loss/take-profit protection can't wait an hour
between checks, but the full analysis cycle is comparatively expensive and
doesn't need sub-hour freshness. When touching either job, check whether a
change belongs in the hourly cycle (new-entry logic) or the fast tick (existing-
position protection) — they intentionally do not share code paths for the SL/TP
check itself.

### Position protection: polling vs. native broker orders

`Position.protection_mode` (`src/data_store/positions.py`) is either `"polling"`
(default — `PositionMonitor` compares live price against `stop_loss`/`take_profit`
each tick and sends a fresh market order to close) or `"native_oco"` (crypto
only, opt-in via `trading.use_native_crypto_stops` in config.yaml — a real OCO
order rests on binance.us itself; `PositionMonitor._reconcile_native_oco`
just asks the exchange whether it already closed the position, it never sends
its own close order for these). Alpaca equities can never use `native_oco`:
Alpaca doesn't allow combining bracket/OCO orders with fractional/notional
shares, which is how this bot buys SPY/QQQ/GLD/USO on a small account.

### Multi-broker routing

`config.yaml`'s `brokers:` section maps each asset to a class (`crypto` →
binance.us, `equity` → Alpaca) via `symbols` lists. `ExecutionNode`
(`src/execution/execution_node.py`) dispatches orders to the matching broker
client; `main.py`'s `_asset_class_for`/`_get_active_asset_classes` do the same
lookup before signal generation to decide which asset classes are even worth
analyzing this cycle, based on live balances (fail-open: a broker that can't be
reached stays "active" rather than silently going dark).

### Safety layers (all independent, all can block/pause trading for different reasons)

- **`MandateGate`** (`src/safety/mandate_gate.py`): per-trade validation —
  symbol allow-list, max position size, rolling-24h realized daily-loss cap,
  rolling-24h max-total-exposure cap, rolling-24h max-new-entries cap. Reads
  `PositionRepository` as source of truth, not the audit log, for exposure/loss
  (a prior bug summed audit events without subtracting closes).
- **`KillSwitch`** (`src/safety/kill_switch.py`): a filesystem flag file; blocks
  bot STARTUP entirely if present.
- **`DrawdownKillSwitch`** (`src/safety/kelly_drawdown.py`): pauses NEW entries
  (not existing-position protection) once account equity has dropped more than
  a configured % from its peak, for a cooldown period.
- **Dashboard "Stop trading" toggle** (`audit/trading_pause.json`, read by
  `_is_trading_paused` in main.py): pauses NEW entries only, checked every
  cycle. Deliberately separate from both the LIVE/PAPER mode toggle and
  `KillSwitch` — this is the "soft pause," those are harder stops.
- **`PaperToLiveChecklist`** (`src/safety/paper_to_live.py`): runs automatically
  at startup when transitioning into live mode with open paper positions;
  defaults to aborting the transition unless configured otherwise.

### Config override pattern (dashboard-editable settings)

`config.yaml` is never rewritten by the running bot or the dashboard (PyYAML's
`dump()` isn't comment-safe and would wipe the file's documentation). Instead,
dashboard saves write small JSON files under `audit/` — `trading_config_override.json`,
`risk_config_override.json`, `trading_pause.json`, `mode_override.json` — which
`main.py` merges on top of `config.yaml`'s values at startup, before agent
construction. This means **most dashboard-saved changes require a bot restart**
to take effect (the dashboard's Settings page surfaces `pending_restart` and a
"Restart now" button that hits `POST /api/restart`); only the mode toggle and
the trading-pause toggle are re-read live, every cycle. `src/api/state.py`'s
module docstring has the full read/write helper inventory for each override
file.

### Dashboard API

`src/api/server.py` (FastAPI) runs in a background thread inside the SAME
process as the bot (`_start_api_server` in main.py) — it's a read-mostly layer
over the same on-disk state the bot already writes (`audit/audit.jsonl`,
`data_store/positions.json`, mirrored to `audit/positions.json` for the
dashboard container to see, since the two Docker services don't share
`data_store/`). Background tasks poll and broadcast over `/ws/live` every 1-2s
for the dashboard's live audit feed and position P&L. Auth is a single shared
bearer token (`DASHBOARD_PASSWORD`), required on all mutating endpoints.

### Strategy research tools (not in the live loop)

`src/analysis/genetic_programming.py` (GP strategy evolution) and
`src/optimization/hyperopt.py`/`backtester.py` are standalone tools invoked via
their own CLI entry points or tests — as of this writing neither is called from
`main.py`'s live scheduling. Don't assume changes there affect production
trading unless you also wire them in.
