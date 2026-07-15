# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Guaritradbot: a multi-agent trading bot trading crypto (binance.us, via ccxt) and
equities/ETFs (Alpaca), driven by a YAML-defined workflow (`src/workflows/trading_loop.yaml`)
executed by a small custom `WorkflowEngine`. Ships as two Docker services behind
Coolify: the bot itself (`Dockerfile.bot`) and a separate Next.js dashboard
(`Dockerfile.dashboard`, in `dashboard/`) that reads the bot's state over a REST +
Server-Sent Events (SSE) stream API the bot exposes in-process (`src/api/`).

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

`main.py` builds an `agents_registry` containing:
- `MarketAnalystAgent`: fetches market data and computes indicators.
- `NewsAnalyst`: scans RSS news headlines (Sprint 49).
- `SentimentAnalyst`: scans social sentiment (Sprint 50).
- `LLMAnalyst`: generates shadow LLM validation votes (Sprint 55).
- `StrategyAgent`: evaluates technical strategies and generates trade hypotheses.
- `HypothesisScorer` (formerly `DebateAgent`, renamed in Sprint 47B): scores hypotheses sequentially (Bull/Bear/Risk) and applies news/sentiment adjustments.
- `RiskManagerAgent`: sizes positions using Kelly criterion (fractional sizing added in Sprint 46S) and stress-tests portfolio limits.
- `ExecutionAgent`: executes orders via broker.
- `NotificationAgent` (independent): listens to the shared `EventBus` and sends Telegram alerts.

`main.py` instantiates these agents and hands them to the `WorkflowEngine` (`src/workflows/engine.py`), which executes the YAML-defined workflow (`src/workflows/trading_loop.yaml`) steps in order, respecting each step's `depends_on`.

### Two independent scheduling loops managed by BotRuntime (Sprint 46T / audit M6)

`main.py` builds dependencies and instantiates a `BotRuntime` (`src/runtime/bot_runtime.py`) which owns and drives the two execution loops:

- **`job_with_monitor`** (every 30 mins by default, `schedule.run_interval_hours` set to 0.5 since Sprint 46S): runs on the main thread via `EpochScheduler`. It checks the Drawdown Kill Switch (derived from `EquityTracker` since Sprint 46N), applies capital-aware asset routing, checks the dashboard manual pause (`trading_pause.json`), and executes the full workflow engine analysis cycle (`NewsAnalyst` -> `SentimentAnalyst` -> `MarketAnalyst` -> `LLMAnalyst` -> `StrategyAgent` -> `HypothesisScorer` -> `RiskManagerAgent` -> `ExecutionAgent`).
- **`fast_monitor_tick`** (every 2 minutes, `schedule.fast_monitor_interval_minutes`): runs on its own dedicated daemon thread (`fast-monitor-thread`) to prevent starvation during long analysis cycles (Sprint 46N/A5). It only performs position protection: SL/TP polling, native OCO reconciliation (for crypto), smart profit-taking on fresh reversal signals (under 5 min old, Sprint 46R), updating the `EquityTracker` (persisted to `data_store/equity_state.json`), sending hourly P&L Telegram updates, and pinging the dead-man's switch (`HEALTHCHECKS_PING_URL`, Sprint 46R/M11.4).

This split exists because stop-loss/take-profit protection can't wait between long cycles, and separating them into threads prevents yfinance rate-limiting delays from leaving positions unprotected.

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

`src/api/server.py` (FastAPI) runs in a background thread inside the same process as the bot (`_start_api_server` in main.py). It provides a read/write layer over the bot's state (mirrored in `audit/` for the Docker container volumes).
Instead of WebSockets, it primarily broadcasts live state updates (audit log, position lists, and stats) over a Server-Sent Events (SSE) stream at `/api/events` to bypass Traefik proxy HTTP/1.1 upgrade blocks on the VPS (Sprint 57), though `/ws/live` remains for fallback.
Auth is a bearer token derived from `DASHBOARD_PASSWORD` via HMAC-SHA256 (Sprint 46N/A9) using a key stored in `audit/token_secret.key`. Auth is required on ALL endpoints (both reading and mutating) except `/api/health` and `/api/auth/login` (Sprint 46N/C5). Rate limiting and cooldowns guard login and restart endpoints.

### Strategy research tools (not in the live loop)

`src/analysis/genetic_programming.py` (GP strategy evolution) and
`src/optimization/hyperopt.py`/`backtester.py` are standalone tools invoked via
their own CLI entry points or tests — as of this writing neither is called from
`main.py`'s live scheduling. Don't assume changes there affect production
trading unless you also wire them in.

## Recent Sprints & Changes

- **Sprint 62 (Simulated Paper Balance)**:
  - Added `paper.starting_balance_usd` to `config.yaml` to allow starting paper trading with a custom virtual balance (default $1000) instead of the real live balance.
  - Skip broker balance reconciliation in paper mode to avoid fake deposit/withdrawal events.
- **Sprint 63 (Alpaca Integration & Filter Tuning)**:
  - Added `docs/ALPACA_SETUP.md` for multi-broker setup.
  - Disabled StrategyAgent loss-streak suppression in `main.py` (`loss_streak_suppress=0`) to allow more trade opportunities.
- **Sprint 64 (Notification & Execution Realignment)**:
  - Enriched `TRADE_CLOSED` event payload in `src/data_store/position_monitor.py` with `direction`, `qty`, `entry_price`, `close_price`, and `duration_s` for detailed Telegram notifications.
  - Dynamically align Stop Loss and Take Profit in `src/execution/execution_node.py` when the actual filled entry price differs from the requested signal entry price (preventing immediate stop-outs due to feed lag).

