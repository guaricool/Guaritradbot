# Alpaca Setup — Multi-broker trading

> Sprint 62 / Sprint 63 documentation.
> Carlos's path: from "no broker besides crypto" → "Alpaca configured, paper + live ready".

## Why this doc exists

Before Sprint 62, the bot only traded crypto on Binance.US. Adding Alpaca
(stocks + ETFs) was on the roadmap but not configured. Sprint 62
formalized the multi-broker setup, Sprint 63 is when Carlos actually
wired it up. This doc captures the **exact steps** so future operators
(us, anyone) can reproduce the configuration.

## Architecture: how the bot talks to two brokers

The bot is **multi-broker** by design (`config.yaml::brokers:` section).
Each asset has an `asset_class` (`crypto` | `equity` | `forex`) and a
matching broker:

```yaml
brokers:
  crypto:
    name: "binanceus"
    symbols: [BTC-USD, ETH-USD, SOL-USD]
  equity:
    name: "alpaca"
    symbols: [SPY, QQQ, GLD, USO, AAPL, NVDA, TSLA]   # configurable
```

`ExecutionNode.execute()` dispatches each order to the broker matching
the asset's class. The same code path is used for entry, replacement,
and close — no per-broker branching in the strategy layer.

The current code reads **ONE pair of keys** for Alpaca:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

The endpoint URL (paper vs live) is selected at runtime by reading
`mode_override.json` (the same file the dashboard's PAPER/LIVE toggle
writes to). One key pair, two URL options. Cleaner than managing 4
keys.

## Prerequisites

1. **Alpaca account approved** (KYC takes 1-3 days)
   - https://app.alpaca.markets → "Get Started" → fill KYC
2. **binance.us account active** (Carlos already has this)

## Step 1: Generate the Alpaca paper trading API keys

1. Log in to https://app.alpaca.markets
2. Make sure you're in the **Paper** account (top-left dropdown
   shows `Paper · PA...` ID)
3. Sidebar → **"API Keys"** under "PERSONAL"
4. Click **"Generate New Key"** (or **"Regenerate"** if you have an
   old one)
5. **The modal shows BOTH the API Key ID AND the Secret Key.** The
   secret only appears ONCE — copy it now to your password manager.
   - API Key ID looks like `PKCL5DB2V6PEZWGREVY7PB4MVO`
   - Secret Key looks like `a1B2c3D4e5F6g7H8i9J0kLmNoPqRsT...` (~40 chars)
6. **Save the secret** in your password manager. If you lose it, you
   must regenerate (and the old one stops working).

> **Do NOT paste the secret in chat, DMs, git commits, or any log.**
> Coolify env vars are the only safe place.

## Step 2: Add the keys to Coolify

1. Open your Coolify dashboard: `http://13.140.181.29:8000`
2. Find the **guaritradbot** app (NOT the dashboard app)
3. Tab **"Environment Variables"**
4. Click **"+ Add"** for each of the two variables:

   | Key | Value |
   |-----|-------|
   | `ALPACA_API_KEY` | `PKCL5DB2V6PEZWGREVY7PB4MVO` (your paper key) |
   | `ALPACA_SECRET_KEY` | the secret you copied in Step 1 |

5. Click **"Save"** / **"Update"**
6. **DO NOT add `ALPACA_PAPER_*`** versions — the code reads only
   `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`. The URL switch is
   driven by `mode_override.json`, not by key naming.

> **Troubleshooting**: if Coolify doesn't pick up the keys after save
> (still shows `alpaca_balance_source: not_configured` in
> `/api/state`), try saving AGAIN — Coolify's first save often
> silently no-ops on docker-compose apps.

## Step 3: Wait for auto-redeploy (1-6 minutes)

Coolify redeploys automatically when env vars change. Webhook lag is
1-6 minutes. Watch the bot container:

```bash
ssh 13.140.181.29 "docker ps --filter name=guaritradbot --format '{{.Names}}|{{.Image}}'"
```

When the image SHA changes, the new container is up.

## Step 4: Verify the integration

Run from inside the bot container:

```bash
CID=$(docker ps --filter name=guaritradbot --format "{{.Names}}" | grep -v dash | head -1)
docker exec $CID env | grep ALPACA
# Should show: ALPACA_API_KEY=PK... and ALPACA_SECRET_KEY=...
```

Then hit `/api/state` (with the bot's password):

```bash
docker exec $CID python -c '
import urllib.request, json
body = json.dumps({"password": "**YOUR_DASHBOARD_PASSWORD**"}).encode()
req = urllib.request.Request("http://localhost:8080/api/auth/login", data=body, method="POST",
    headers={"Content-Type": "application/json"})
token = json.loads(urllib.request.urlopen(req).read())["token"]
req = urllib.request.Request("http://localhost:8080/api/state",
    headers={"Authorization": f"Bearer {token}"})
data = json.loads(urllib.request.urlopen(req).read())
print("alpaca_balance_usd:", data.get("alpaca_balance_usd"))   # should be 100000.0
print("alpaca_balance_source:", data.get("alpaca_balance_source"))  # should be "live"
'
```

If `alpaca_balance_usd` is `None`, the keys didn't make it to the
container. Re-check Step 2.

## Step 5: Watch the bot take equity positions

After Alpaca is configured:

1. **Hard refresh the dashboard** — the "Alpaca" balance card now
   shows `$100,000.00` (paper) or your live balance.
2. **The bot's next cycle** (within 30 min) will include SPY, QQQ, GLD,
   USO, AAPL, NVDA, TSLA in its asset universe.
3. Signals will be generated; longs go through, shorts are rejected
   by the `equity_short_not_supported` gate (Alpaca fractional shares
   can't combine with bracket/short orders — see `allow_equity_short`
   in `config.yaml`).

## Paper vs live mode

The bot's mode (paper / live) is controlled by the dashboard's
**"Mode" toggle** (top-right of every page), which writes to
`audit/mode_override.json`:

- **Paper mode** (`mandate_enabled: false`): `AlpacaBroker` uses
  `https://paper-api.alpaca.markets/v2` — orders are simulated on
  Alpaca's $100k paper account. No real money moves.
- **Live mode** (`mandate_enabled: true`): `AlpacaBroker` uses
  `https://api.alpaca.markets/v2` — orders are real. **ONLY do this
  with real keys (not paper keys), and ONLY after you've validated
  the bot's behavior in paper for at least a week.**

To switch to live, you need to:

1. Generate **LIVE** keys (not paper) in the Alpaca dashboard
2. Replace `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in Coolify with
   the live values
3. Flip the dashboard toggle to "LIVE"
4. Monitor closely for the first hour

## Loss-streak filter (Sprint 63)

After Alpaca is wired, you may notice the bot **not taking new positions**
even with paper money available. That's the **loss_streak_suppress=3**
filter (`src/agents/strategy_agent.py`): if the last 3 trades for an
asset+direction all lost, the StrategyAgent vetoes the next signal
at the source.

To disable (Carlos did this on 2026-07-14 to see more activity):

Edit `main.py` line ~1029, find the `StrategyAgent(...)` block, add
`loss_streak_suppress=0` as the last argument. The
HypothesisScorer's softer weighting (`recent_lessons_for`) is still
active, so defeated strategies still lose the debate — this just
prevents the source-side veto.

To re-enable: remove the line (or set to 3).

## What's NOT in this setup yet

- **Alpaca paper + live separate keys** (currently 1 pair, URL is
  runtime-switched). Adding 4-key support is a Sprint 64+ TODO.
- **Forex** (IBKR / OANDA / FXCM). Sprint 65+ — needs a new broker
  adapter. The dashboard's `/charts` already shows forex pairs as
  read-only reference.
- **OCO orders for equities** (Alpaca bracket orders are blocked for
  fractional/notional sizing). Sprint 64+ TODO.

## Reference files

- `src/execution/alpaca_broker.py` — broker adapter (handles paper/live
  URL switch on every call)
- `src/execution/broker_routing.py` — the B033 paper-mode gate
- `main.py:1010` — where StrategyAgent is instantiated
- `main.py:580-597` — where the Alpaca broker is constructed
- `src/api/state.py` — `_get_alpaca_balance()` populates
  `alpaca_balance_usd` / `alpaca_balance_source`
- `config.yaml::brokers` — asset → class map (edit here to add more
  equities)

## Quick troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `alpaca_balance_usd: null` | Keys not in container | Re-save in Coolify, wait for redeploy |
| `alpaca_balance_source: "not_configured"` | Same as above | Same |
| `alpaca_balance_source: "unavailable"` | Keys present but API call failed | Check key/secret are correct, no extra spaces |
| Bot not taking equity positions | Loss streak filter | Set `loss_streak_suppress=0` (Sprint 63) |
| Shorts rejected with "equity_short_not_supported" | Expected — Alpaca fractional | This is correct, no fix needed |
| `BROKER_NOT_CONFIGURED` in audit log | Mandate enabled but no broker for that asset class | Add the asset to `config.yaml::brokers.equity.symbols` |
