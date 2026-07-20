"""
Sprint 46N — shared broker-routing + paper-mode helpers for CLOSING an
already-open position, extracted out of `ExecutionNode` so
`PositionMonitor` and `RiskManagerAgent._try_replace_position` can
route closes correctly instead of always hitting whichever single
`broker_client` (the crypto ccxt client) they happened to be
constructed with.

Fixes two findings from the 2026-07-11 third-party audit
(`AUDITORIA_COMPLETA_2026-07-11.md`):

- C1: equity closes (SPY/QQQ/GLD/USO) were sent to the CRYPTO broker
  (`BrokerClient.create_market_order`, a ccxt/binance.us call) because
  `PositionMonitor`/`RiskManagerAgent` only ever held one `self.broker`.
  A ccxt client obviously rejects an equity symbol, so every equity
  close/replacement failed forever (`CLOSE_FAILED`/`REPLACEMENT_FAILED`,
  position stuck open) — the SL/TP protection this bot exists to
  provide never actually worked for equities.
- C2: paper mode still called the real broker on close/replace — there
  was no check at all (only "is a broker object configured"), unlike
  the ENTRY side (`ExecutionNode`) which already gates on
  `_is_mandate_enabled()`. In paper mode this meant closes/replacements
  were placing real orders against the live/paper Alpaca or binance.us
  sandbox account instead of being simulated locally like entries are.

Why a new module instead of importing `ExecutionNode`'s private
`_is_mandate_enabled`/`_resolve_broker`: those are tested, live-tested
code paths for ORDER ENTRY; duplicating this small amount of read-only
logic here avoids touching `execution_node.py` at all while fixing the
close-side bug, minimizing regression risk in the entry path. Keep the
semantics identical to `execution_node.py`'s versions if either changes.
"""
import json
import os


def is_mandate_enabled(override_path: str = "audit/mode_override.json") -> bool:
    """True = LIVE mode (mandate enabled), False = paper.

    Same file + same semantics as `ExecutionNode._is_mandate_enabled`
    and `NotificationAgent`'s mode check: a cheap read on every call
    (not cached) so a dashboard LIVE/PAPER toggle takes effect on the
    very next close attempt, not just the next restart.
    """
    try:
        if not override_path or not os.path.exists(override_path):
            return False
        with open(override_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return bool(data.get("mandate_enabled", False))
    except Exception:
        # Fail safe toward PAPER (i.e. do NOT assume live) — matches
        # ExecutionNode._is_mandate_enabled's own fail-closed behavior.
        return False


def is_scalp_mode_enabled(override_path: str = "audit/scalp_mode_override.json") -> bool:
    """True = scalp mode (many small-profit entries, tighter TP), False
    = the normal swing profile. Same read-every-call pattern as
    `is_mandate_enabled` so the dashboard toggle takes effect on the
    very next cycle, no restart needed. Scalp mode is paper-only —
    callers are expected to also check `not is_mandate_enabled(...)`
    before applying its overrides.
    """
    try:
        if not override_path or not os.path.exists(override_path):
            return False
        with open(override_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return bool(data.get("scalp_mode_enabled", False))
    except Exception:
        return False


def build_asset_to_class_map(brokers_config: dict) -> dict:
    """{"BTC-USD": "crypto", "SPY": "equity", ...} built from
    config.yaml's `brokers:` section — the same source
    `ExecutionNode._resolve_broker` reads — so entries and closes agree
    on where an asset actually trades.
    """
    mapping: dict = {}
    if not brokers_config:
        return mapping
    for asset_class, cfg in brokers_config.items():
        if not isinstance(cfg, dict):
            continue
        for sym in cfg.get("symbols", []) or []:
            mapping[sym] = asset_class
    return mapping


def resolve_broker_for_close(asset: str, asset_to_class: dict, crypto_broker, alpaca_broker, oanda_broker=None):
    """Return (broker_or_None, asset_class_str) for closing `asset`.

    Unlike `ExecutionNode._resolve_broker` (which is choosing where to
    OPEN a brand-new position and can reasonably refuse an unmapped
    asset), a close/replace caller already has an OPEN position on its
    hands. If the asset isn't in `asset_to_class` at all (e.g.
    `brokers_config` wasn't passed at all — every close/replace call
    site before Sprint 46N only ever had ONE broker anyway — or a
    symbol was removed from config.yaml after a position was opened on
    it), we fall back to `crypto_broker`, i.e. the SAME broker every
    caller used unconditionally before this module existed. This is a
    deliberate backward-compatibility choice: `brokers_config` only
    ever reclassifies an asset as "equity"/"forex" (routing it to
    `alpaca_broker`/`oanda_broker` instead — the actual C1 fix), it
    never needs to turn a previously-reachable crypto/unknown asset
    into "no broker at all". Returns `asset_class="unknown"` in this
    case so callers can still log/audit it distinctly from a confirmed
    "crypto" match.

    ``oanda_broker`` defaults to None (optional third broker, like
    Alpaca) — callers that don't pass it just never resolve "forex".
    """
    asset_class = asset_to_class.get(asset)
    if asset_class == "crypto":
        return crypto_broker, "crypto"
    if asset_class == "equity":
        return alpaca_broker, "equity"
    if asset_class == "forex":
        return oanda_broker, "forex"
    return crypto_broker, (asset_class or "unknown")


def send_close_order(broker, asset_class: str, symbol: str, side: str, qty: float) -> dict:
    """Place a market order to close/reduce a position, using the
    calling convention that matches `asset_class`.

    - crypto (`BrokerClient`, ccxt-backed): positional
      `create_market_order(symbol, side, qty)`.
    - equity (`AlpacaBroker`): `create_market_order(symbol, side,
      amount=qty)` — Alpaca's client also accepts `notional_usd` as an
      alternative and REJECTS both/neither being set, so `amount` must
      be passed by keyword.

    Returns whatever the broker returns (expected: a dict with a
    `status` key set to `"failed"` on failure — both broker clients
    follow this convention; see their docstrings).
    """
    if asset_class == "equity":
        return broker.create_market_order(symbol, side, amount=qty)
    return broker.create_market_order(symbol, side, qty)
