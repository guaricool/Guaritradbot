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

Sprint 36.1 — Runtime paper/live switching:

The ``paper`` flag is NOT a constructor argument anymore. It's read on
every call from ``mode_override.json`` (the same file the B033 paper
gate reads ``mandate_enabled`` from). The dashboard's Paper/Live
toggle writes BOTH keys, so one click switches both the "send orders
at all" gate AND the Alpaca endpoint in lockstep.

We hold TWO TradingClient instances (paper + live) in memory — cheap,
no re-construction. Each method picks the right one based on the
runtime flag. The user is responsible for putting the right keys in
the env vars; the broker will raise a clear auth error if the keys
don't match the chosen endpoint (e.g. live keys sent to paper endpoint).

Reference:
- https://alpaca.markets/docs/api-references/trading-api/
- https://github.com/alpacahq/alpaca-py
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def _read_alpaca_paper_flag(mode_override_path: str) -> bool:
    """Read ``alpaca_paper`` from mode_override.json. Default: True (paper).

    Defensive: any error (file missing, malformed JSON) returns True.
    The bot defaults to paper mode for safety — a failed read should
    never accidentally route to live.
    """
    try:
        if not os.path.exists(mode_override_path):
            return True
        with open(mode_override_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("alpaca_paper", True))
    except Exception:
        return True


class AlpacaBroker:
    """Minimal Alpaca Markets broker client for the trading bot.

    Holds two TradingClient instances (paper + live) and dispatches
    each call to the one matching the current ``alpaca_paper`` flag
    in ``mode_override.json``.

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

    def __init__(
        self,
        api_key=None,
        secret_key=None,
        paper=True,
        mode_override_path="audit/mode_override.json",
    ):
        """Initialize the broker.

        The ``paper`` parameter is now LEGACY and ignored. Kept for
        backwards-compat with Sprint 36 callers. The runtime flag
        ``alpaca_paper`` from mode_override.json is the source of
        truth. Defaults to paper if the file is missing/malformed.
        """
        self.api_key = api_key or os.getenv("ALPACA_API_KEY")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        self.mode_override_path = mode_override_path
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
        # Build BOTH clients at startup. The SDK constructor doesn't
        # make any API calls — it just stores credentials and creates
        # an HTTP session. Cheap, no state to worry about.
        self._paper_client = TradingClient(
            self.api_key, self.secret_key, paper=True
        )
        self._live_client = TradingClient(
            self.api_key, self.secret_key, paper=False
        )
        # Log which mode we're starting in (helps catch deploy misconfigs)
        initial_paper = _read_alpaca_paper_flag(self.mode_override_path)
        mode = "PAPER" if initial_paper else "LIVE ⚠️"
        logger.info(
            f"[AlpacaBroker] inicializado con clients paper+live. "
            f"Modo runtime actual: {mode} (leído de {self.mode_override_path})"
        )

    # ------------------------------------------------------------------
    # Internal: pick the right client based on the runtime flag
    # ------------------------------------------------------------------
    def _alpaca_paper_mode(self) -> bool:
        """Read the current ``alpaca_paper`` flag from mode_override.json.

        Returns True (paper) on any error — defensive default.
        Cheap file read, called on every public method.
        """
        return _read_alpaca_paper_flag(self.mode_override_path)

    def _client(self):
        """Return the TradingClient matching the current runtime mode.

        If the runtime says paper, return self._paper_client.
        If the runtime says live, return self._live_client.
        The dashboard's toggle changes the file → this method picks
        the right one on the next call, no restart needed.
        """
        if self._alpaca_paper_mode():
            return self._paper_client
        return self._live_client

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def get_usd_balance(self) -> float:
        """Return the cash balance in USD.

        Returns 0.0 on any error (and logs). The bot's risk model treats
        0 balance as a hard stop, so this is the safe default.
        """
        try:
            account = self._client().get_account()
            return float(account.cash)
        except Exception as exc:
            logger.error(
                f"[AlpacaBroker] Error getting balance "
                f"(paper={self._alpaca_paper_mode()}): {exc}"
            )
            return 0.0

    # ------------------------------------------------------------------
    # Symbol validation
    # ------------------------------------------------------------------
    def is_symbol_tradeable(self, symbol: str) -> bool:
        """Check if Alpaca will accept an order for ``symbol``.

        Returns False on any error (network, unknown symbol, halted asset).
        """
        try:
            asset = self._client().get_asset(symbol)
            return bool(asset.tradable) and asset.status == "active"
        except Exception as exc:
            logger.debug(
                f"[AlpacaBroker] is_symbol_tradeable({symbol}) → False: {exc}"
            )
            return False

    def get_supported_symbols(self) -> list | None:
        """Return a list of active tradeable US equities/ETFs.

        Returns None if the call fails (the caller treats None as
        "skip pre-flight validation, attempt the order anyway").

        This is a LARGE list (~10k symbols) — we cache it after the
        first call to avoid hammering the API. The cache is per-endpoint
        (paper vs live) so the two clients don't poison each other.
        """
        is_paper = self._alpaca_paper_mode()
        cache_attr = "_supported_symbols_cache_paper" if is_paper else "_supported_symbols_cache_live"
        cached = getattr(self, cache_attr, None)
        if cached is not None:
            return cached
        try:
            tc = self._client()
            request_params = getattr(tc, "get_all_assets", None)
            if request_params is None:
                assets = tc.list_assets()
            else:
                assets = tc.get_all_assets()
            symbols = [
                a.symbol for a in assets if a.tradable and a.status == "active"
            ]
            setattr(self, cache_attr, symbols)
            logger.info(
                f"[AlpacaBroker] cached {len(symbols)} supported symbols "
                f"({'paper' if is_paper else 'live'} endpoint)"
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
        and ``error`` contains the message. The ``endpoint`` field
        indicates which Alpaca environment was used (paper/live) for
        audit clarity.
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

        is_paper = self._alpaca_paper_mode()
        endpoint = "paper" if is_paper else "live"
        try:
            order = self._client().submit_order(req)
            return {
                "id": str(order.id),
                "status": str(order.status),
                "symbol": order.symbol,
                "side": str(order.side),
                "qty": str(order.qty) if order.qty is not None else None,
                "notional": str(order.notional) if order.notional is not None else None,
                "submitted_at": str(order.submitted_at),
                "endpoint": endpoint,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "symbol": symbol,
                "endpoint": endpoint,
            }
