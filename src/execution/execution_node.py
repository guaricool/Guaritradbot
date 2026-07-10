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
    ):
        self.event_bus = event_bus
        self.execution_mode = execution_mode
        self.broker = broker_client
        self.alpaca_broker = alpaca_broker
        self.brokers_config = brokers_config or {}
        self.kill_switch = kill_switch
        self.audit = audit
        self.mode_override_path = mode_override_path
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

    def _resolve_broker(self, asset: str) -> tuple:
        """Return ``(broker, asset_class, broker_cfg)`` for the given asset.

        ``asset_class`` is one of the keys in ``brokers_config`` (e.g.
        ``"crypto"``, ``"equity"``). If the asset is not in the routing
        table, defaults to ``"crypto"`` (binanceus).

        ``broker`` is ``None`` if the matched asset class has no broker
        configured (e.g. equity signal but no ``alpaca_broker``).
        """
        asset_class = self._asset_to_class.get(asset, "crypto")
        cfg = self.brokers_config.get(asset_class, {}) or {}
        if asset_class == "equity":
            return self.alpaca_broker, "equity", cfg
        # default: crypto
        return self.broker, "crypto", cfg

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
        if broker_order and broker_order.get("status") != "failed":
            status = "FILLED (LIVE MARKET)"
        else:
            status = "FAILED (LIVE MARKET)"

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

        if broker_order and broker_order.get("status") != "failed":
            status = "FILLED (LIVE MARKET — ALPACA)"
        else:
            status = f"FAILED (LIVE MARKET — ALPACA: {broker_order.get('error', '?')})"

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
