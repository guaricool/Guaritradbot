"""
Sprint 36 — Alpaca Broker Wrapper.

Wrapper for Alpaca Markets REST API that mirrors BrokerClient's interface
(``create_market_order(symbol, side, amount)``) so ExecutionNode can route
orders to either binanceus (crypto) or Alpaca (stocks/ETFs) transparently.

Differences from the ccxt-based crypto broker (BrokerClient):

* **Symbol format**: equities are bare tickers (``SPY``), not ``BTC/USDT``.
  No ``-`` / ``/`` normalization needed at the broker level.
* **Balance**: USD cash, not USDT. We expose ``get_usd_balance()`` (NOT
  ``get_usdt_balance``) to make the asset class explicit at the call site.
* **Fractional shares**: Alpaca supports buying fractional shares by
  *notional* USD amount (not by qty). For example, ``$10 of SPY`` →
  ``0.0133 shares``. We expose a ``notional_usd`` parameter on
  ``create_market_order``; callers that want fractional pass it instead
  of ``amount``.
* **Asset class**: only US equities + ETFs + crypto on Alpaca's side.
  Crypto through Alpaca uses a different product API; for now we only
  route US equities/ETFs here. Crypto stays on binanceus.

Uses the modern alpaca-py SDK (``from alpaca.trading.client import
TradingClient``). The old ``alpaca_trade_api.REST`` import is
deprecated and was removed in 0.40+.

The same paper-mode safety pattern as B033 applies: this class is
constructed at startup if ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY``
are set in the environment. If either is missing, ``AlpacaBroker(...)``
raises ``ValueError`` and the bot falls back to a single-broker
(binanceus) config without breaking.

Reference:
- https://alpaca.markets/docs/api-references/trading-api/
- https://github.com/alpacahq/alpaca-py
"""
import logging
import os

logger = logging.getLogger(__name__)


class AlpacaBroker:
    """Minimal Alpaca Markets broker client for the trading bot.

    Designed to be a drop-in alternative to ``BrokerClient`` for the
    equity/ETF half of the portfolio. Does NOT support the full ccxt
    surface — only the methods ExecutionNode needs.

    Methods
    -------
    get_usd_balance() -> float
        Cash balance in USD (Alpaca account.cash).
    is_symbol_tradeable(symbol) -> bool
        Cheap pre-flight check before submitting an order.
    create_market_order(symbol, side, amount=None, notional_usd=None) -> dict
        Submit a market order. Pass ``amount`` (whole shares) OR
        ``notional_usd`` (fractional by USD). For the bot's typical
        micro-account use case, ``notional_usd`` is the right choice.
    get_supported_symbols() -> list
        Returns the active, tradeable US equity/ETF list. Cached
        after first call.
    """

    PAPER_URL = "https://paper-api.alpaca.markets"
    LIVE_URL = "https://api.alpaca.markets"

    def __init__(self, api_key=None, secret_key=None, paper=True):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        self.paper = paper
        if not self.api_key or not self.secret_key:
            raise ValueError(
                "AlpacaBroker requires ALPACA_API_KEY and ALPACA_SECRET_KEY "
                "in the environment (or passed explicitly)."
            )
        try:
            from alpaca.trading.client import TradingClient  # noqa: WPS433
        except ImportError as exc:
            raise ImportError(
                "alpaca-py is not installed. Run `pip install alpaca-py`."
            ) from exc
        self.trading_client = TradingClient(
            self.api_key, self.secret_key, paper=paper
        )
        mode = "PAPER" if paper else "LIVE ⚠️"
        logger.info(
            f"[AlpacaBroker] Conectado a Alpaca en modo {mode}"
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def get_usd_balance(self) -> float:
        """Return the cash balance in USD.

        Returns 0.0 on any error (and logs). The bot's risk model treats
        0 balance as a hard stop, so this is the safe default.
        """
        try:
            account = self.trading_client.get_account()
            return float(account.cash)
        except Exception as exc:
            logger.error(f"[AlpacaBroker] Error getting balance: {exc}")
            return 0.0

    # ------------------------------------------------------------------
    # Symbol validation
    # ------------------------------------------------------------------
    def is_symbol_tradeable(self, symbol: str) -> bool:
        """Check if Alpaca will accept an order for ``symbol``.

        Returns False on any error (network, unknown symbol, halted asset).
        """
        try:
            asset = self.trading_client.get_asset(symbol)
            return bool(asset.tradable) and asset.status == "active"
        except Exception as exc:
            logger.debug(f"[AlpacaBroker] is_symbol_tradeable({symbol}) → False: {exc}")
            return False

    def get_supported_symbols(self) -> list | None:
        """Return a list of active tradeable US equities/ETFs.

        Returns None if the call fails (the caller treats None as
        "skip pre-flight validation, attempt the order anyway").

        This is a LARGE list (~10k symbols) — we cache it after the
        first call to avoid hammering the API.
        """
        if getattr(self, "_supported_symbols_cache", None) is not None:
            return self._supported_symbols_cache
        try:
            # New alpaca-py: list_assets is sync, returns Asset objects
            request_params = getattr(self.trading_client, "get_all_assets", None)
            if request_params is None:
                # Older alpaca-py exposed list_assets
                assets = self.trading_client.list_assets()
            else:
                assets = self.trading_client.get_all_assets()
            symbols = [a.symbol for a in assets if a.tradable and a.status == "active"]
            self._supported_symbols_cache = symbols
            logger.info(
                f"[AlpacaBroker] cached {len(symbols)} supported symbols"
            )
            return symbols
        except Exception as exc:
            logger.error(f"[AlpacaBroker] list_assets failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------
    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float | None = None,
        notional_usd: float | None = None,
    ) -> dict:
        """Submit a market order to Alpaca.

        Exactly ONE of ``amount`` (whole shares) or ``notional_usd`` (USD)
        must be provided.

        Returns
        -------
        dict with at minimum ``status`` ("filled" / "accepted" / "failed")
        and the order id when successful. On failure, ``status="failed"``
        and ``error`` contains the message.
        """
        if (amount is None) == (notional_usd is None):
            return {
                "status": "failed",
                "error": "create_market_order requires exactly one of "
                "`amount` (qty) or `notional_usd` (USD).",
            }
        side_norm = side.lower()
        if side_norm not in ("buy", "sell"):
            return {"status": "failed", "error": f"invalid side: {side}"}

        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
        except ImportError as exc:
            return {
                "status": "failed",
                "error": f"alpaca-py import failed: {exc}",
            }

        if notional_usd is not None:
            # Alpaca requires notional >= 1.00 for fractional orders.
            if notional_usd < 1.0:
                return {
                    "status": "failed",
                    "error": f"notional_usd=${notional_usd:.2f} below Alpaca minimum $1.00",
                }
            req = MarketOrderRequest(
                symbol=symbol,
                notional=round(float(notional_usd), 2),
                side=OrderSide.BUY if side_norm == "buy" else OrderSide.SELL,
                type="market",
                time_in_force=TimeInForce.DAY,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=float(amount),
                side=OrderSide.BUY if side_norm == "buy" else OrderSide.SELL,
                type="market",
                time_in_force=TimeInForce.DAY,
            )

        try:
            order = self.trading_client.submit_order(req)
            return {
                "id": str(order.id),
                "status": str(order.status),
                "symbol": order.symbol,
                "side": str(order.side),
                "qty": str(order.qty) if order.qty is not None else None,
                "notional": str(order.notional) if order.notional is not None else None,
                "submitted_at": str(order.submitted_at),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "symbol": symbol,
            }
