"""
Sprint 0+1 — ExecutionNode.

Sprint 0 fix: el viejo `input()` bloqueante rompía el daemon en Docker.
Sprint 1 añade:
- Kill Switch filesystem: si el archivo existe, NO se ejecuta nada.
- Cada fill (real o paper) se registra en el audit ledger.

Sprint 35 (B033) añade:
- Paper-mode gate: cuando `mandate_enabled=false` (paper mode), las
  órdenes se SIMULAN localmente y NO se envían al broker real. El bug
  previo era que `binanceus` no tiene testnet propio, así que
  cualquier `broker.create_market_order()` con credenciales reales
  ejecutaba dinero real aunque la intención fuera paper.
- Symbol validation: chequea que el par exista en el exchange antes
  de mandar la orden. binanceus solo soporta crypto, así que
  símbolos como `SPY`, `GLD`, `USO`, `QQQ` (que están en
  config.allowed_symbols) fallaban con un error genérico de
  ccxt. Ahora se rechazan antes con un mensaje claro.

Sprint 36 (multi-broker) añade:
- Routing por asset class: equities/ETFs (SPY/QQQ/GLD/USO) se enrutan
  a `AlpacaBroker` (con `notional_usd` para fractional shares), crypto
  (BTC/ETH/SOL) sigue yendo a `BrokerClient` (binanceus/ccxt).
- La tabla de routing vive en `config.brokers.<asset_class>.symbols`.
  Default: si el asset no está en la tabla, va a crypto (binanceus).
- Si el asset es equity pero `alpaca_broker` no está configurado, la
  orden se rechaza con `ALPACA_NOT_CONFIGURED` (no se simula en
  paper — esto es lo que el usuario quiere operar).
"""
import json
import os
import time

from src.data_store.positions import Position


# Sprint 43 H7 fix: known fill statuses from ccxt/Alpaca adapters.
# Anything not in this set is treated as not-yet-confirmed and the
# position is NOT added to the repo. The audit caught that the
# previous code treated any non-"failed" status as FILLED, which
# would silently add positions for pending/partial/accepted orders.
_FILLED_STATUSES = frozenset({
    "filled",      # ccxt standard (binance, bybit, etc.)
    "closed",      # Alpaca standard
    "FILLED",      # legacy uppercase
    "fill",        # some adapters
})
_PARTIAL_STATUSES = frozenset({
    "partially_filled",  # ccxt standard
    "partial",           # legacy / simplified
})
_FAILED_STATUSES = frozenset({
    "failed",
    "rejected",
    "expired",
    "canceled",
    "cancelled",
})
_PENDING_STATUSES = frozenset({
    "pending",     # ccxt standard (working)
    "new",         # just submitted
    "accepted",    # exchange acknowledged
    "open",        # still in the order book
})


def _classify_fill_status(broker_order) -> str:
    """
    Sprint 43 H7 helper: normalize a broker's order response into
    one of:
      - "filled": the full requested qty was filled
      - "partial": some qty was filled but less than requested
      - "failed": the order was rejected / expired / canceled
      - "pending": the order is still working (new, accepted, pending, etc.)
      - "unknown": broker returned something we don't recognize

    Caller should treat anything other than "filled" as
    not-yet-confirmed and avoid adding a position to the repo.
    """
    if not broker_order or not isinstance(broker_order, dict):
        return "unknown"
    status = broker_order.get("status", "")
    if not isinstance(status, str):
        return "unknown"
    status_lower = status.lower()
    if status_lower in _FILLED_STATUSES:
        # Even if status says "filled", check the filled qty if present.
        # If the broker reports filled=0, treat as unknown (defensive).
        filled = broker_order.get("filled")
        if filled is not None:
            try:
                if float(filled) <= 0:
                    return "unknown"
            except (TypeError, ValueError):
                pass
        return "filled"
    if status_lower in _PARTIAL_STATUSES:
        return "partial"
    if status_lower in _FAILED_STATUSES:
        return "failed"
    if status_lower in _PENDING_STATUSES:
        return "pending"
    # Unrecognized status — return "unknown" so the caller is
    # loud about it. Better to refuse to add a position than to
    # silently accept an exchange response we don't understand.
    return "unknown"


def _is_mandate_enabled(override_path: str = "audit/mode_override.json") -> bool:
    """Read mode_override.json and return True if mandate_enabled is on.

    Same pattern as NotificationAgent._is_live_mode() — cheap file
    read on every call so a dashboard toggle takes effect immediately
    (the bot's main loop runs every ~60s, so there's no perf concern).
    """
    try:
        if not os.path.exists(override_path):
            return False
        with open(override_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("mandate_enabled", False))
    except Exception:
        # Safe default: if we can't read the override, behave conservatively
        # and stay in paper mode (no real orders).
        return False


class ExecutionNode:
    """
    Nodo de ejecución abstracto inspirado en NautilusTrader.
    Maneja el enrutamiento de órdenes aislando a los agentes
    de los detalles del broker (Simulado o en Vivo).
    """

    def __init__(
        self,
        event_bus,
        execution_mode="auto",
        broker_client=None,
        alpaca_broker=None,
        brokers_config=None,
        kill_switch=None,
        audit=None,
        mode_override_path="audit/mode_override.json",
        position_repo=None,
        use_native_crypto_stops: bool = False,
    ):
        self.event_bus = event_bus
        self.execution_mode = execution_mode
        self.broker = broker_client
        self.alpaca_broker = alpaca_broker
        self.brokers_config = brokers_config or {}
        self.kill_switch = kill_switch
        self.audit = audit
        self.mode_override_path = mode_override_path
        # Sprint 46I: place a real OCO stop-loss/take-profit order on
        # binance.us right after a confirmed crypto LONG entry fill,
        # instead of relying only on PositionMonitor's price polling.
        # Off by default (config.yaml's trading.use_native_crypto_stops)
        # — see src/execution/broker.py's OCO methods for the "not
        # exercised against the live API yet" caveat.
        self.use_native_crypto_stops = use_native_crypto_stops
        # Sprint 43 C5 fix: ExecutionNode now owns the position persistence
        # (was previously owned by RiskManagerAgent). Adding a position
        # to the repo is the LAST step of a successful fill, not part of
        # the risk-evaluation phase. This eliminates "ghost positions" —
        # entries that existed in the repo but never on the broker
        # (because the broker call failed after risk_eval but before
        # we had a chance to roll back).
        self.position_repo = position_repo
        # Cached list of supported symbols on the broker (populated lazily
        # so we don't hammer the exchange API at construction time).
        self._supported_symbols_cache: list | None = None
        # Sprint 36: build asset → asset_class map from brokers_config.
        # Default fallback is "crypto" for any asset not in the map.
        self._asset_to_class: dict[str, str] = {}
        for asset_class, cfg in self.brokers_config.items():
            if not isinstance(cfg, dict):
                continue
            for sym in cfg.get("symbols", []) or []:
                self._asset_to_class[sym] = asset_class
        self.event_bus.subscribe("ORDER_APPROVED", self.on_order_approved)

    # ----------------------------------------------------------------------
    # Sprint 43 C5 fix: position persistence on successful fill.
    # ----------------------------------------------------------------------
    def _persist_filled_position(
        self,
        order_data: dict,
        status: str,
        broker_oco_order_id: str = None,
        protection_mode: str = "polling",
    ):
        """
        Register a position in the repo AFTER the broker (or paper-mode
        simulation) confirms the fill. Emits POSITION_OPENED audit and
        TRADE_OPENED event for downstream consumers (NotificationAgent
        → Telegram, PositionMonitor → SL/TP tracking).

        CRITICAL: this is the ONLY place positions are added to the
        repo. The previous design added them in `RiskManagerAgent`
        (in the `risk_evaluation` step, BEFORE the broker call), which
        created ghost positions when the broker call failed. Now the
        repo is only updated when we have a confirmed fill (real or
        simulated), so exposure / max_open_trades / mandate caps
        always reflect ground truth.

        Sprint 46I: `broker_oco_order_id`/`protection_mode` let
        `_execute_crypto_order` record that a REAL exchange-side OCO
        order is protecting this position (see broker.py's
        `create_oco_sell_order`) — PositionMonitor uses this to
        reconcile against the exchange instead of polling price
        thresholds. Defaults preserve the original "polling" behavior
        for every other call site (equities, paper mode, OCO placement
        failures).
        """
        if self.position_repo is None:
            # Repos weren't injected (e.g. unit tests that only exercise
            # the execution node's order routing). Skip persistence
            # silently; tests that need persistence inject their own repo.
            return
        try:
            qty = float(order_data.get("position_size", 0) or 0)
            risk_usd = float(order_data.get("risk_usd", 0) or 0)
            entry_price = float(order_data.get("entry_price", 0) or 0)
            stop_loss = float(order_data.get("stop_loss", 0) or 0)
            take_profit = float(order_data.get("take_profit", 0) or 0)
            pos = Position(
                asset=order_data.get("asset", "?"),
                direction=order_data.get("direction", "long"),
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                qty=qty,
                risk_usd=risk_usd,
                entry_ts=time.time(),
                strategy=order_data.get("strategy", ""),
                protection_mode=protection_mode,
                broker_oco_order_id=broker_oco_order_id,
            )
            self.position_repo.add_open(pos)
            if self.audit:
                self.audit.append(
                    "POSITION_OPENED",
                    {
                        "position_id": pos.position_id,
                        "asset": pos.asset,
                        "qty": pos.qty,
                        "notional_usd": pos.notional_usd,
                        "status": status,
                    },
                )
            if self.event_bus is not None:
                self.event_bus.publish(
                    "TRADE_OPENED",
                    {
                        "position_id": pos.position_id,
                        "asset": pos.asset,
                        "direction": pos.direction,
                        "entry_price": pos.entry_price,
                        "qty": pos.qty,
                        "stop_loss": pos.stop_loss,
                        "take_profit": pos.take_profit,
                        "risk_usd": pos.risk_usd,
                        "notional_usd": pos.notional_usd,
                        "strategy": pos.strategy,
                        "entry_ts": pos.entry_ts,
                    },
                )
        except Exception as e:
            # Persistence failure must NOT take down the broker path —
            # the order has been filled, the position is real on the
            # exchange. Log to audit and continue.
            print(f"[ExecutionNode] ⚠️ _persist_filled_position failed: {e}")
            if self.audit:
                self.audit.append(
                    "POSITION_PERSIST_FAILED",
                    {
                        "asset": order_data.get("asset"),
                        "direction": order_data.get("direction"),
                        "qty": order_data.get("position_size"),
                        "status": status,
                        "error": str(e),
                    },
                )

    def _resolve_broker(self, asset: str) -> tuple:
        """Return ``(broker, asset_class, broker_cfg)`` for the given asset.

        ``asset_class`` is one of the keys in ``brokers_config`` (e.g.
        ``"crypto"``, ``"equity"``).

        Sprint 43 M9 fix: an asset that is NOT in the routing table
        no longer silently defaults to crypto. The audit flagged this
        as a fail-open risk: a typo or a stale config could send an
        order to binanceus for an asset that the user thought was
        on a different broker. Now the resolution surfaces the
        ambiguity with a warning, audits it, and (when the unknown
        class is ``"equity"``) refuses to send to crypto.

        Behavior:
          - Known asset: returns (broker, asset_class, cfg) as before.
          - Unknown asset, asset_class not in brokers_config at all:
            returns (None, "unknown", {}) — caller MUST handle None.
            Publishes SYSTEM_ERROR so Carlos sees the typo.
          - Unknown asset, asset_class inferred to "crypto" (the
            legacy fallback): logs a loud warning AND audits
            UNKNOWN_SYMBOL_ROUTED, but still routes to crypto for
            backward compat. Carlos can switch to strict mode by
            setting config.brokers.strict_unknown_routing=true (future).

        ``broker`` is ``None`` if the matched asset class has no broker
        configured (e.g. equity signal but no ``alpaca_broker``).
        """
        if asset in self._asset_to_class:
            asset_class = self._asset_to_class[asset]
            cfg = self.brokers_config.get(asset_class, {}) or {}
            if asset_class == "crypto":
                return self.broker, "crypto", cfg
            if asset_class == "equity":
                return self.alpaca_broker, "equity", cfg
            # Sprint 45 fix (N3): `asset_class` here is whatever key the
            # asset was found under in `brokers_config` (config.yaml's
            # `brokers:` section) — it is NOT necessarily "crypto" or
            # "equity". The previous code only special-cased "equity"
            # and silently routed EVERYTHING else (including any
            # future third asset class like "forex"/"commodity") to
            # the crypto broker, mislabeled as "crypto" in every audit
            # event — reintroducing exactly the fail-open-to-crypto
            # pattern the M9 fix was meant to eliminate, the moment a
            # new class gets added to config.yaml. Treat an unmapped
            # class the same way `_resolve_broker` already treats an
            # unmapped SYMBOL: reject loudly instead of guessing.
            warning_msg = (
                f"[ExecutionNode] ⚠️ asset_class '{asset_class}' (for '{asset}') "
                f"has no broker implementation wired up in _resolve_broker "
                f"(only 'crypto' and 'equity' are supported). Rejecting the "
                f"order instead of silently routing it to the crypto broker."
            )
            print(warning_msg)
            if self.audit is not None:
                self.audit.append(
                    "UNSUPPORTED_ASSET_CLASS_ROUTED",
                    {"asset": asset, "asset_class": asset_class, "action": "rejected_no_broker"},
                )
            if self.event_bus is not None:
                try:
                    self.event_bus.publish("SYSTEM_ERROR", {
                        "kind": "UNSUPPORTED_ASSET_CLASS",
                        "asset": asset,
                        "asset_class": asset_class,
                        "error": (f"❓ '{asset}' está configurado con asset_class "
                                  f"'{asset_class}', que no tiene broker implementado. "
                                  f"Orden RECHAZADA (no se enruta a crypto por defecto)."),
                    })
                except Exception:
                    pass
            return None, asset_class, cfg

        # === Unknown asset: audit + warn + return (None, "unknown", {}) ===
        warning_msg = (
            f"[ExecutionNode] ⚠️ UNKNOWN_SYMBOL '{asset}' is not in the "
            f"routing table. Available symbols: {sorted(self._asset_to_class.keys())[:10]}"
            f"{'...' if len(self._asset_to_class) > 10 else ''}. "
            f"Rejecting the order (no broker). Add it to config.yaml `brokers:` "
            f"section under the right asset_class to fix."
        )
        print(warning_msg)
        if self.audit is not None:
            self.audit.append(
                "UNKNOWN_SYMBOL_ROUTED",
                {
                    "asset": asset,
                    "known_symbols": sorted(self._asset_to_class.keys()),
                    "action": "rejected_no_broker",
                },
            )
        if self.event_bus is not None:
            try:
                self.event_bus.publish("SYSTEM_ERROR", {
                    "kind": "UNKNOWN_SYMBOL",
                    "asset": asset,
                    "known_symbols_sample": sorted(self._asset_to_class.keys())[:5],
                    "error": (f"❓ Símbolo '{asset}' NO está en la routing table. "
                              f"Orden RECHAZADA. Agrégalo a config.yaml → brokers: → "
                              f"symbols: para habilitarlo."),
                })
            except Exception:
                pass
        return None, "unknown", {}

    def on_order_approved(self, data: dict):
        # Sprint 1: Kill switch filesystem (defensa en profundidad)
        if self.kill_switch and self.kill_switch.is_triggered():
            print(
                f"[ExecutionNode] ⛔ Kill switch ARMED — orden {data.get('asset')} NO ejecutada."
            )
            if self.audit:
                self.audit.append(
                    "TRADE_BLOCKED_KILLSWITCH",
                    {"asset": data.get("asset")},
                )
            return

        if self.execution_mode == "human_in_the_loop":
            print(f"\n[ExecutionNode] 🛑 ORDEN PENDIENTE DE APROBACIÓN HUMANA")
            print(f"  Asset:       {data.get('asset')}")
            print(f"  Dirección:   {data.get('direction')}")
            print(f"  Tamaño:      {data.get('position_size', 0):.6f}")
            print(f"  Stop loss:   {data.get('stop_loss', 0):.4f}")

            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_PENDING_APPROVAL",
                    {"order": data, "timeout_s": 30},
                )

            try:
                decision = input("¿Aprobar? (Y/N, default=N en 30s): ").strip().upper()
                if decision != "Y":
                    print("[ExecutionNode] ❌ Orden rechazada por el humano.")
                    if self.audit:
                        self.audit.append(
                            "TRADE_REJECTED_HUMAN",
                            {"asset": data.get("asset")},
                        )
                    return
            except (EOFError, KeyboardInterrupt):
                print("[ExecutionNode] ⚠️ No hay TTY. SKIP seguro (cambia execution_mode a 'auto' para bypass).")
                if self.audit:
                    self.audit.append(
                        "TRADE_SKIPPED_NO_TTY",
                        {"asset": data.get("asset")},
                    )
                return

        self.execute_order(data)

    def _get_supported_symbols(self) -> list | None:
        """Return the broker's supported symbol list (e.g. ['BTC/USDT', ...]).

        Returns None if the broker is not configured, the call fails, or
        the exchange doesn't expose `symbols` (some ccxt exchanges don't).
        The caller treats None as "we can't validate, proceed but log
        a warning".
        """
        if self._supported_symbols_cache is not None:
            return self._supported_symbols_cache
        if self.broker is None or not hasattr(self.broker, "exchange"):
            return None
        try:
            symbols = self.broker.exchange.symbols
            if symbols:
                self._supported_symbols_cache = list(symbols)
                return self._supported_symbols_cache
        except Exception as e:
            print(f"[ExecutionNode] ⚠️ Could not fetch supported symbols: {e}")
        return None

    def execute_order(self, order_data: dict):
        # Doble check del kill switch
        if self.kill_switch and self.kill_switch.is_triggered():
            print("[ExecutionNode] ⛔ Kill switch ARMED — execute_order cancelado.")
            return

        asset = order_data.get("asset", "?")
        direction = order_data.get("direction", "?")
        qty = order_data.get("position_size", 0)
        entry_price = float(order_data.get("entry_price", 0) or 0)

        print(
            f"[ExecutionNode] 🚀 EJECUTANDO ORDEN: "
            f"{asset} {direction} qty={qty} @ ${entry_price:.2f}"
        )

        # === Sprint 36: Resolve broker by asset class ===
        broker_instance, asset_class, broker_cfg = self._resolve_broker(asset)
        # Sprint 43 M9 fix: an unknown symbol now resolves to
        # (None, "unknown", {}). Sprint 45 fix (N3/NEW-3): an asset
        # whose configured class has no broker implementation (not
        # "crypto"/"equity") now ALSO resolves with broker_instance
        # None, using its real class name instead of "unknown" — so
        # the two cases need distinct handling here, not the old
        # single condition (`asset_class == "unknown" or broker_instance
        # is None and asset_class not in ("equity", "crypto")`) whose
        # `and`-before-`or` branch was actually unreachable dead code
        # (it could only be true when asset_class == "unknown", which
        # the left side already covered).
        if asset_class == "unknown":
            status = f"FAILED (UNKNOWN_SYMBOL: {asset})"
            print(
                f"[ExecutionNode] ❌ UNKNOWN_SYMBOL: '{asset}' is not in the "
                f"routing table. Order REJECTED. Add it to config.yaml "
                f"`brokers:` section to enable trading."
            )
            if self.audit:
                self.audit.append(
                    "TRADE_FAILED",
                    {
                        "asset": asset,
                        "direction": direction,
                        "qty": qty,
                        "entry_price": entry_price,
                        "status": status,
                        "kind": "UNKNOWN_SYMBOL",
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {"status": status, "order": order_data, "kind": "UNKNOWN_SYMBOL"},
                )
            return
        if broker_instance is None and asset_class not in ("equity", "crypto"):
            status = f"FAILED (UNSUPPORTED_ASSET_CLASS: {asset_class})"
            print(
                f"[ExecutionNode] ❌ UNSUPPORTED_ASSET_CLASS: '{asset}' has "
                f"asset_class '{asset_class}', which has no broker wired up. "
                f"Order REJECTED (not silently routed to crypto)."
            )
            if self.audit:
                self.audit.append(
                    "TRADE_FAILED",
                    {
                        "asset": asset,
                        "direction": direction,
                        "qty": qty,
                        "entry_price": entry_price,
                        "status": status,
                        "kind": "UNSUPPORTED_ASSET_CLASS",
                        "asset_class": asset_class,
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {"status": status, "order": order_data, "kind": "UNSUPPORTED_ASSET_CLASS"},
                )
            return
        # If the asset is equity but Alpaca isn't configured, fail loudly.
        # We don't want a silent fallback to crypto for an SPY order.
        if asset_class == "equity" and broker_instance is None:
            print(
                f"[ExecutionNode] ❌ ALPACA_NOT_CONFIGURED: '{asset}' is an "
                f"equity/ETF but no AlpacaBroker is configured. Set "
                f"ALPACA_API_KEY + ALPACA_SECRET_KEY in Coolify Environment."
            )
            status = f"FAILED (ALPACA_NOT_CONFIGURED: {asset})"
            if self.audit:
                self.audit.append(
                    "TRADE_FAILED",
                    {
                        "asset": asset,
                        "direction": direction,
                        "qty": qty,
                        "entry_price": entry_price,
                        "status": status,
                        "asset_class": asset_class,
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {"status": status, "order": order_data},
                )
            return

        # === Sprint 36: No-broker case (preserves B033 default) ===
        # When no broker is configured for the asset class (e.g. running
        # without binanceus for crypto), simulate a fill at the requested
        # entry price. This is the original pre-B033 behavior: status
        # "FILLED (SIMULATED)" so dashboards and downstream agents keep
        # working in dev environments with no broker credentials.
        if broker_instance is None:
            print(
                f"[ExecutionNode] 🟡 NO BROKER — orden {asset} {direction} "
                f"simulada localmente (default dev behavior)."
            )
            status = "FILLED (SIMULATED)"
            if self.audit:
                self.audit.append(
                    "TRADE_FILLED",
                    {
                        "asset": asset,
                        "direction": direction,
                        "qty": qty,
                        "entry_price": entry_price,
                        "status": status,
                        "simulated": True,
                        "asset_class": asset_class,
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {
                        "status": status,
                        "order": order_data,
                        "simulated": True,
                        "asset_class": asset_class,
                    },
                )
            # Sprint 43 C5: persist position only after confirmed fill.
            self._persist_filled_position(order_data, status)
            return

        # === B033 fix: Paper-mode gate ===
        # binanceus/Alpaca-paper have no testnet guarantees — so any call
        # to a real broker uses real money. We MUST check mandate_enabled
        # before sending. If we're in paper mode (mandate_enabled=False),
        # simulate the fill locally and skip the broker entirely.
        is_paper_mode = not _is_mandate_enabled(self.mode_override_path)
        if is_paper_mode:
            print(
                f"[ExecutionNode] 🟡 PAPER MODE — orden {asset} {direction} "
                f"simulada @ ${entry_price:.2f} via {asset_class} broker "
                f"(NO enviada a broker real)"
            )
            status = "FILLED (PAPER)"
            if self.audit:
                self.audit.append(
                    "TRADE_FILLED",
                    {
                        "asset": asset,
                        "direction": direction,
                        "qty": qty,
                        "entry_price": entry_price,
                        "status": status,
                        "simulated": True,
                        "asset_class": asset_class,
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {
                        "status": status,
                        "order": order_data,
                        "simulated": True,
                        "asset_class": asset_class,
                    },
                )
            # Sprint 43 C5: persist position only after confirmed fill.
            self._persist_filled_position(order_data, status)
            return

        # === Dispatch to the right broker ===
        if asset_class == "equity":
            self._execute_equity_order(order_data, broker_instance)
            return

        # Default: crypto (binanceus via ccxt)
        self._execute_crypto_order(order_data, broker_instance)

    def _execute_crypto_order(self, order_data: dict, broker):
        """Route a crypto order to the binanceus broker (ccxt-based).

        Preserves the B033 symbol validation: the symbol must be in
        ``broker.exchange.symbols`` before we send.
        """
        asset = order_data.get("asset", "?")
        direction = order_data.get("direction", "?")
        qty = order_data.get("position_size", 0)
        entry_price = float(order_data.get("entry_price", 0) or 0)

        side = "buy" if direction == "long" else "sell"
        amount = qty
        symbol = asset
        if "-" in symbol:
            symbol = symbol.replace("-", "/")
        elif "/" not in symbol:
            symbol = f"{symbol}/USDT"

        # === B033 fix: Symbol validation ===
        # binanceus only supports crypto. Catching the ccxt error
        # AFTER sending is too late (we waste a network roundtrip
        # and confuse the audit log with generic 'failed' status).
        # Validate locally first.
        supported = self._get_supported_symbols()
        if supported is not None and symbol not in supported:
            print(
                f"[ExecutionNode] ❌ SYMBOL_NOT_SUPPORTED: '{symbol}' "
                f"no está en {broker.exchange.id if hasattr(broker, 'exchange') and hasattr(broker.exchange, 'id') else 'el broker'}. "
                f"binanceus solo soporta crypto. Si querés tradear "
                f"este activo, agregalo a un exchange que lo soporte."
            )
            status = f"FAILED (SYMBOL_NOT_SUPPORTED: {symbol})"
            if self.audit:
                self.audit.append(
                    "TRADE_FAILED",
                    {
                        "asset": asset,
                        "direction": direction,
                        "qty": qty,
                        "entry_price": entry_price,
                        "status": status,
                        "symbol": symbol,
                        "supported_count": len(supported),
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {"status": status, "order": order_data},
                )
            return

        broker_order = broker.create_market_order(symbol, side, amount)
        # Sprint 43 H7 fix: don't treat any non-"failed" status as FILLED.
        # The audit caught that "pending", "partially_filled", "new",
        # "accepted" would all be treated as FILLED, which would add
        # a position to the repo for a fill that hadn't actually
        # happened (or had only partially happened).
        # Now: only explicit "filled"/"closed" is treated as FILLED.
        # Anything else is logged with its real status and the order
        # does NOT enter the repo.
        fill_verdict = _classify_fill_status(broker_order)
        if fill_verdict == "filled":
            status = "FILLED (LIVE MARKET)"
        elif fill_verdict == "partial":
            status = f"PARTIAL_FILL (LIVE MARKET: {broker_order.get('filled', '?')}/{amount})"
        else:  # pending, unknown, missing
            status = f"NOT_FILLED (LIVE MARKET: {fill_verdict})"

        if self.audit:
            self.audit.append(
                "TRADE_FILLED" if status.startswith("FILLED") else "TRADE_FAILED",
                {
                    "asset": asset,
                    "direction": direction,
                    "qty": qty,
                    "entry_price": entry_price,
                    "status": status,
                    "asset_class": "crypto",
                },
            )
        if self.event_bus:
            self.event_bus.publish(
                "ORDER_EXECUTED",
                {"status": status, "order": order_data, "asset_class": "crypto"},
            )
        # Sprint 43 C5 + H7: only persist the position if the broker
        # confirmed a FULL fill. On FAILED / PARTIAL / PENDING status,
        # no Position is added to the repo, so the exposure /
        # max_open_trades / mandate caps remain consistent with
        # ground truth.
        if fill_verdict == "filled":
            # Sprint 46I: place a REAL protective OCO order on the
            # exchange right after the entry fills — only for LONG
            # positions (a sell-OCO protects a long; spot exchanges
            # don't support shorting an asset you don't hold, so
            # "short" crypto signals were never real exchange shorts
            # to begin with — out of scope here, pre-existing).
            # Best-effort: if this fails, the position is still
            # persisted normally with protection_mode="polling" (the
            # original behavior) — a failed OCO placement must never
            # block the entry itself, since the position is already
            # real on the exchange at this point.
            broker_oco_order_id = None
            protection_mode = "polling"
            if self.use_native_crypto_stops and direction == "long":
                stop_loss = float(order_data.get("stop_loss", 0) or 0)
                take_profit = float(order_data.get("take_profit", 0) or 0)
                if stop_loss > 0 and take_profit > 0 and take_profit > stop_loss:
                    try:
                        oco_response = broker.create_oco_sell_order(
                            symbol=symbol,
                            amount=amount,
                            take_profit_price=take_profit,
                            stop_price=stop_loss,
                        )
                        if isinstance(oco_response, dict) and oco_response.get("status") != "failed":
                            broker_oco_order_id = str(
                                oco_response.get("orderListId")
                                or oco_response.get("listClientOrderId", "")
                            ) or None
                            if broker_oco_order_id:
                                protection_mode = "native_oco"
                        if protection_mode != "native_oco":
                            print(
                                f"[ExecutionNode] ⚠️ OCO placement falló para {asset} "
                                f"(quedará en modo polling): {oco_response}"
                            )
                            if self.audit:
                                self.audit.append("OCO_PLACEMENT_FAILED", {
                                    "asset": asset, "response": str(oco_response)[:300],
                                })
                    except Exception as e:
                        print(f"[ExecutionNode] ⚠️ OCO placement exception para {asset}: {e} (modo polling)")
                        if self.audit:
                            self.audit.append("OCO_PLACEMENT_FAILED", {
                                "asset": asset, "error": str(e)[:300],
                            })
            self._persist_filled_position(
                order_data, status,
                broker_oco_order_id=broker_oco_order_id,
                protection_mode=protection_mode,
            )
        elif fill_verdict == "partial":
            # Sprint 43 H7: a partial fill is a real money event —
            # publish SYSTEM_ERROR so Carlos knows to investigate.
            # We don't add a position (the qty mismatch would corrupt
            # the audit). A human can decide whether to retry or
            # top-up manually.
            if self.event_bus:
                self.event_bus.publish("SYSTEM_ERROR", {
                    "kind": "PARTIAL_FILL",
                    "asset": asset,
                    "direction": direction,
                    "requested": amount,
                    "filled": broker_order.get("filled", "?"),
                    "broker_status": broker_order.get("status", "?"),
                    "error": (f"⚠️ Fill parcial: {asset} {direction} "
                              f"{broker_order.get('filled', '?')}/{amount}. "
                              f"Posición NO agregada al repo."),
                })

    def _execute_equity_order(self, order_data: dict, broker):
        """Route an equity/ETF order to the Alpaca broker.

        Alpaca's fractional shares are best driven by NOTIONAL USD
        amount. The bot's ``position_size`` is qty in the asset's unit
        (e.g. 0.0133 shares of SPY), so we convert to USD using
        ``entry_price`` and pass that to Alpaca as ``notional_usd``.
        Falls back to whole-share ``qty`` if ``entry_price`` is missing.
        """
        asset = order_data.get("asset", "?")
        direction = order_data.get("direction", "?")
        qty = order_data.get("position_size", 0)
        entry_price = float(order_data.get("entry_price", 0) or 0)

        side = "buy" if direction == "long" else "sell"

        # Pre-flight: confirm symbol is tradeable before sending.
        # We don't fetch the full ~10k symbol list every order; just
        # query this one symbol's status (one network call, cheap).
        if not broker.is_symbol_tradeable(asset):
            print(
                f"[ExecutionNode] ❌ SYMBOL_NOT_TRADEABLE: '{asset}' is "
                f"not active/tradeable on Alpaca."
            )
            status = f"FAILED (SYMBOL_NOT_TRADEABLE: {asset})"
            if self.audit:
                self.audit.append(
                    "TRADE_FAILED",
                    {
                        "asset": asset,
                        "direction": direction,
                        "qty": qty,
                        "entry_price": entry_price,
                        "status": status,
                        "asset_class": "equity",
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {"status": status, "order": order_data},
                )
            return

        # Decide: notional_usd (fractional) vs amount (whole shares).
        if entry_price > 0 and qty > 0:
            notional_usd = float(qty) * entry_price
            broker_order = broker.create_market_order(
                asset, side, notional_usd=notional_usd
            )
            order_kind = "notional"
        else:
            # No price → must use whole shares.
            broker_order = broker.create_market_order(asset, side, amount=qty)
            order_kind = "qty"

        # Sprint 43 H7 fix: same fill-status classification as the
        # crypto path. Only "filled"/"closed" is treated as a full
        # fill. Pending/partial/unknown are surfaced and the order
        # does NOT enter the repo.
        fill_verdict = _classify_fill_status(broker_order)
        if fill_verdict == "filled":
            status = "FILLED (LIVE MARKET — ALPACA)"
        elif fill_verdict == "partial":
            status = f"PARTIAL_FILL (LIVE MARKET — ALPACA: {broker_order.get('filled', '?')}/{qty})"
        else:
            status = f"NOT_FILLED (LIVE MARKET — ALPACA: {fill_verdict})"

        if self.audit:
            self.audit.append(
                "TRADE_FILLED" if status.startswith("FILLED") else "TRADE_FAILED",
                {
                    "asset": asset,
                    "direction": direction,
                    "qty": qty,
                    "entry_price": entry_price,
                    "status": status,
                    "asset_class": "equity",
                    "broker": "alpaca",
                    "order_kind": order_kind,
                    "broker_order_id": broker_order.get("id") if broker_order else None,
                },
            )
        if self.event_bus:
            self.event_bus.publish(
                "ORDER_EXECUTED",
                {
                    "status": status,
                    "order": order_data,
                    "asset_class": "equity",
                    "broker": "alpaca",
                },
            )
        # Sprint 43 C5 + H7: only persist on a full fill. See
        # _classify_fill_status for the exact rule.
        if fill_verdict == "filled":
            self._persist_filled_position(order_data, status)
        elif fill_verdict == "partial":
            if self.event_bus:
                self.event_bus.publish("SYSTEM_ERROR", {
                    "kind": "PARTIAL_FILL",
                    "asset": asset,
                    "direction": direction,
                    "requested": qty,
                    "filled": broker_order.get("filled", "?"),
                    "broker_status": broker_order.get("status", "?"),
                    "error": (f"⚠️ Fill parcial Alpaca: {asset} {direction} "
                              f"{broker_order.get('filled', '?')}/{qty}. "
                              f"Posición NO agregada al repo."),
                })
