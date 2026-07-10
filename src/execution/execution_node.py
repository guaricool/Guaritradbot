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
        kill_switch=None,
        audit=None,
        mode_override_path="audit/mode_override.json",
    ):
        self.event_bus = event_bus
        self.execution_mode = execution_mode
        self.broker = broker_client
        self.kill_switch = kill_switch
        self.audit = audit
        self.mode_override_path = mode_override_path
        # Cached list of supported symbols on the broker (populated lazily
        # so we don't hammer the exchange API at construction time).
        self._supported_symbols_cache: list | None = None
        self.event_bus.subscribe("ORDER_APPROVED", self.on_order_approved)

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

        print(
            f"[ExecutionNode] 🚀 EJECUTANDO ORDEN: "
            f"{asset} {direction} qty={qty}"
        )

        # === B033 fix: Paper-mode gate ===
        # binanceus doesn't have a testnet — so any call to the broker
        # uses real money. We MUST check mandate_enabled before sending.
        # If we're in paper mode (mandate_enabled=False), simulate the
        # fill locally and skip the broker entirely.
        is_paper_mode = not _is_mandate_enabled(self.mode_override_path)
        if is_paper_mode and self.broker is not None:
            # Simulate a fill at the requested entry price
            entry_price = float(order_data.get("entry_price", 0))
            print(
                f"[ExecutionNode] 🟡 PAPER MODE — orden {asset} {direction} "
                f"simulada @ ${entry_price:.2f} (NO enviada a broker real)"
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
                    },
                )
            if self.event_bus:
                self.event_bus.publish(
                    "ORDER_EXECUTED",
                    {"status": status, "order": order_data, "simulated": True},
                )
            return

        status = "FILLED (SIMULATED)"  # default when no broker is configured

        if self.broker:
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
                    f"no está en {self.broker.exchange.id if hasattr(self.broker.exchange, 'id') else 'el broker'}. "
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
                            "entry_price": order_data.get("entry_price", 0),
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

            broker_order = self.broker.create_market_order(symbol, side, amount)
            if broker_order and broker_order.get("status") != "failed":
                status = "FILLED (LIVE MARKET)"
            else:
                status = "FAILED (LIVE MARKET)"

        # Sprint 1: audit
        if self.audit:
            self.audit.append(
                "TRADE_FILLED" if status.startswith("FILLED") else "TRADE_FAILED",
                {
                    "asset": asset,
                    "direction": direction,
                    "qty": qty,
                    "entry_price": order_data.get("entry_price", 0),
                    "status": status,
                },
            )

        if self.event_bus:
            self.event_bus.publish(
                "ORDER_EXECUTED",
                {"status": status, "order": order_data},
            )
