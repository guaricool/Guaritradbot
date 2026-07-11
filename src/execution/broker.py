import ccxt
import os
try:
    from dotenv import load_dotenv
except ImportError:
    # Sprint 31 hardening: dotenv missing shouldn't crash the bot at startup.
    # Fall back to a no-op loader so the bot can still try to read env vars
    # already exported in the container (Coolify env_file).
    def load_dotenv(*args, **kwargs):
        return False

class BrokerClient:
    """
    Cliente genérico para conectarse a un exchange (Binance, Bybit, etc.)
    utilizando CCXT.
    """
    def __init__(self, exchange_name="binance", use_testnet=True):
        load_dotenv()

        # Sprint 0 fix: align with .env.example (which already declares BINANCE_*)
        api_key = os.getenv("BINANCE_API_KEY")
        secret = os.getenv("BINANCE_API_SECRET")

        # Instanciar el exchange dinámicamente desde ccxt
        exchange_class = getattr(ccxt, exchange_name)

        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
        })

        if use_testnet:
            self.exchange.set_sandbox_mode(True)
            print(f"[BrokerClient] Conectado a {exchange_name.upper()} en modo TESTNET (Sandbox).")
        else:
            print(f"[BrokerClient] ⚠️ Conectado a {exchange_name.upper()} en modo LIVE (Dinero Real).")

    def get_usdt_balance(self) -> float:
        """
        Obtiene el balance disponible. binance global usa USDT,
        binance.us usa USD. Aceptamos ambos.

        Sprint 43 H1 fix: on any error, RAISE the exception instead
        of silently returning 100.0. The audit flagged the old
        "fallback to 100" behavior as a fail-open vulnerability:
        if the broker call fails (network timeout, wrong API keys,
        exchange down, etc.), the caller would size orders based
        on imaginary money. A user with a $0 real balance could
        see orders sized as if they had $100.

        Callers that want a simulated fallback (e.g. for paper
        mode or local dev) should catch the exception and decide
        based on `GUARICO_ALLOW_SIMULATED_BALANCE`:
          - True  → return a simulated value (caller's choice)
          - False → re-raise or return None (production safe)

        A genuine balance of $0 is returned as `0.0` (not raised)
        — that's a valid state, not an error.

        Returns:
            float: the free USD-equivalent balance. May be 0.0 if
                the account has no USD/USDT/BUSD/USDC free.
        Raises:
            ccxt.NetworkError, ccxt.ExchangeError, or any
            underlying broker error.
        """
        balance = self.exchange.fetch_balance()
        for sym in ("USD", "USDT", "BUSD", "USDC"):
            if sym in balance:
                info = balance[sym]
                free = info.get("free") if isinstance(info, dict) else None
                if free is not None and float(free) > 0:
                    return float(free)
                total = info.get("total") if isinstance(info, dict) else None
                if total is not None and float(total) > 0:
                    return float(total)
        # Try raw structure (some exchanges nest balances differently)
        raw = balance.get("info", {}).get("balances", []) if isinstance(balance.get("info"), dict) else []
        for entry in raw:
            asset = entry.get("asset", "").upper()
            if asset in ("USD", "USDT", "BUSD", "USDC"):
                free = float(entry.get("free", 0) or 0)
                if free > 0:
                    return free
        return 0.0

    def create_market_order(self, symbol: str, side: str, amount: float):
        """
        Ejecuta una orden de mercado en el exchange.

        Sprint 46N (audit A3): quantize `amount` to the exchange's real
        lot step-size via `amount_to_precision` before sending — the
        native-OCO path (`create_oco_sell_order` below) already does
        this, but this entry/close path never did, so a caller could
        send a raw float quantity the exchange truncates on its own
        side, silently changing the executed size from what the caller
        (and the audit trail) believes was sent. RiskManagerAgent
        (Sprint 46N audit A3) already quantizes+re-verifies notional at
        SIZING time using this same `amount_to_precision` call, so this
        is defense-in-depth for every OTHER caller of this method
        (position closes, replacements) that pass an already-quantized
        qty through unchanged — it should be a no-op for those, and a
        safety net for anything that isn't.

        Best-effort: if quantization itself fails for any reason
        (markets not loaded, symbol not recognized, network hiccup),
        falls back to the original raw `amount` rather than blocking
        the order — a quantization problem must not be worse than the
        truncation problem it's meant to prevent.
        """
        try:
            if not getattr(self.exchange, "markets", None):
                self.exchange.load_markets()
            quantized = float(self.exchange.amount_to_precision(symbol, amount))
            if quantized > 0:
                amount = quantized
        except Exception as e:
            print(f"[BrokerClient] ⚠️ amount_to_precision falló para {symbol} ({e}); usando cantidad sin cuantizar.")
        try:
            print(f"[BrokerClient] Enviando orden {side.upper()} {amount} {symbol}...")
            # En un entorno de simulación sin API Keys válidas, esto fallará.
            order = self.exchange.create_market_order(symbol, side, amount)
            print(f"[BrokerClient] -> Orden ejecutada: {order['id']}")
            return order
        except Exception as e:
            print(f"[BrokerClient] -> Error ejecutando orden: {e}")
            return {"status": "failed", "error": str(e)}

    def get_ticker_price(self, symbol: str) -> float | None:
        """Sprint 46N (audit A7): live price for SL/TP trigger comparisons.

        Before this fix, `main.py`'s `_fetch_prices_for_open_positions`
        used yfinance's DAILY-CANDLE CLOSE (`interval="1d"`) as a stand-in
        for "the current price" when comparing against stop_loss/
        take_profit — up to a full trading day stale, and sourced from
        Yahoo's composite BTC-USD index, which is NOT binance.us's own
        order book. A position could be closed (or NOT closed) based on
        a price that never actually existed on the exchange that will
        execute the close. This method fetches the price from the SAME
        exchange (binance.us via ccxt) that executes the order.

        Prefers `last` (last traded price). Falls back to a bid/ask
        midpoint if `last` isn't populated (some exchanges omit it on a
        thin book), then to `close` (previous close) as a final resort.

        Returns None (never raises) on any failure — callers treat a
        missing price as "skip this asset this tick", the same fail-open
        philosophy as every other best-effort price fetch in this bot.
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            last = ticker.get("last")
            if last is not None and float(last) > 0:
                return float(last)
            bid, ask = ticker.get("bid"), ticker.get("ask")
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                return (float(bid) + float(ask)) / 2.0
            close = ticker.get("close")
            if close is not None and float(close) > 0:
                return float(close)
            return None
        except Exception as e:
            print(f"[BrokerClient] ⚠️ fetch_ticker falló para {symbol} ({e}).")
            return None

    # ------------------------------------------------------------------
    # Sprint 46I — native OCO stop-loss/take-profit (binance.us)
    # ------------------------------------------------------------------
    #
    # Carlos: "vamos a lo grande... quiero que sea super ultra robusto"
    # — worried that polling stops/TPs once per hour could miss a real
    # exit opportunity. The fix for CRYPTO specifically: place a real
    # OCO (One-Cancels-Other) order on the exchange itself at entry
    # time. The exchange enforces the stop/take-profit with ZERO
    # dependency on the bot process even running — a crash, a slow
    # cycle, a deploy in progress, none of it matters once this order
    # is resting on binance.us's books.
    #
    # (Equities/ETFs via Alpaca do NOT get this: Alpaca does not allow
    # combining bracket/OCO orders with fractional/notional shares,
    # which is how this bot buys SPY/QQQ/GLD/USO with a small account
    # — see src/execution/alpaca_broker.py. Carlos explicitly chose to
    # keep equities on the fast polling loop instead of requiring
    # whole-share minimums — see main.py's fast monitor loop.)
    #
    # IMPORTANT — testing caveat: this session had NO shell/API access
    # to place a real test order and confirm the exact response shape
    # binance.us returns, or that binanceus's ccxt build exposes these
    # exact implicit method names. It's built directly from ccxt's own
    # published example (examples/py/binance-create-oco-order-with-
    # implicit-methods.py) and Binance.US's documented POST /api/v3/
    # order/oco endpoint, and `binanceus` extends ccxt's `binance`
    # class (same implicit-method generation) — but it has NOT been
    # exercised against the live API. Test with the exchange's minimum
    # order size before trusting this at real position sizes, and
    # watch the first few live positions closely.
    #
    # binance.us OCO order requirements (same as binance.com):
    #   - `price` (the take-profit LIMIT leg) must be ABOVE current
    #     price for a sell OCO (protecting a long).
    #   - `stopPrice` (the stop-loss trigger) must be BELOW current
    #     price for a sell OCO.
    #   - `stopLimitPrice` is the actual limit price submitted once
    #     `stopPrice` is touched — set slightly below `stopPrice` (a
    #     small buffer) so the stop leg can still fill during a fast
    #     drop instead of sitting unfilled above the market.

    def create_oco_sell_order(
        self,
        symbol: str,
        amount: float,
        take_profit_price: float,
        stop_price: float,
        stop_limit_price: float = None,
        stop_limit_buffer_pct: float = 0.5,
    ) -> dict:
        """Place a real OCO sell order protecting a LONG crypto position.

        Args:
            symbol: unified ccxt symbol, e.g. "BTC/USDT" (same format
                `_execute_crypto_order` already builds).
            amount: quantity to protect (same qty as the filled entry).
            take_profit_price: the LIMIT leg — must be above current price.
            stop_price: the STOP trigger — must be below current price.
            stop_limit_price: the actual sell-limit price once stopPrice
                triggers. If not given, defaults to `stop_price` minus
                `stop_limit_buffer_pct`% — a small buffer so the order
                can still fill during a fast drop instead of resting
                above a falling market.
            stop_limit_buffer_pct: used only when `stop_limit_price`
                isn't given (see above).

        Returns the raw exchange response dict on success, or
        `{"status": "failed", "error": ...}` on any failure — same
        shape as `create_market_order`, so callers can check
        `.get("status") == "failed"` uniformly. Never raises.
        """
        try:
            market = self.exchange.market(symbol)
            if stop_limit_price is None:
                stop_limit_price = stop_price * (1.0 - stop_limit_buffer_pct / 100.0)
            params = {
                "symbol": market["id"],
                "side": "SELL",
                "quantity": self.exchange.amount_to_precision(symbol, amount),
                "price": self.exchange.price_to_precision(symbol, take_profit_price),
                "stopPrice": self.exchange.price_to_precision(symbol, stop_price),
                "stopLimitPrice": self.exchange.price_to_precision(symbol, stop_limit_price),
                "stopLimitTimeInForce": "GTC",
            }
            print(
                f"[BrokerClient] Colocando OCO real: {symbol} qty={amount} "
                f"TP={take_profit_price} SL={stop_price} (stopLimit={stop_limit_price})"
            )
            response = self.exchange.private_post_order_oco(params)
            print(f"[BrokerClient] -> OCO colocada: orderListId={response.get('orderListId', '?')}")
            return response
        except Exception as e:
            print(f"[BrokerClient] -> Error colocando OCO: {e}")
            return {"status": "failed", "error": str(e)}

    def get_oco_order_status(self, symbol: str, order_list_id: str) -> dict:
        """Query the status of a resting OCO order (for reconciliation:
        has the exchange already closed this position via the stop or
        take-profit leg?). Returns `{"status": "failed", "error": ...}`
        on any failure — never raises. Expected keys on success include
        `listOrderStatus` ("EXECUTING" / "ALL_DONE" / "REJECT") per
        Binance's OCO order-list schema.
        """
        try:
            market = self.exchange.market(symbol)
            params = {"symbol": market["id"], "orderListId": order_list_id}
            return self.exchange.private_get_order_list(params)
        except Exception as e:
            print(f"[BrokerClient] -> Error consultando OCO {order_list_id}: {e}")
            return {"status": "failed", "error": str(e)}

    def cancel_oco_order(self, symbol: str, order_list_id: str) -> dict:
        """Cancel a resting OCO order — MUST be called before manually
        closing a position that has one (e.g. dashboard "Close" /
        "Close all"), otherwise the exchange still has a live sell
        order that could fill later against a position the bot no
        longer thinks is open. Returns `{"status": "failed", ...}` on
        any failure (including "already filled/canceled", which the
        caller should treat as fine to proceed) — never raises.
        """
        try:
            market = self.exchange.market(symbol)
            params = {"symbol": market["id"], "orderListId": order_list_id}
            return self.exchange.private_delete_order_list(params)
        except Exception as e:
            print(f"[BrokerClient] -> Error cancelando OCO {order_list_id}: {e}")
            return {"status": "failed", "error": str(e)}
