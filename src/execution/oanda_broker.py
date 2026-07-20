"""
OANDA v20 Broker Wrapper — forex (EUR/USD, GBP/USD, etc.).

Carlos: forex trades ~24/5 with tighter, more frequent moves than
equities/crypto's usual signal cadence, and OANDA is commission-free on
spread-only pricing (like Alpaca), which matters for the scalp-mode
math (see risk_agent.py's scalp_overrides docstring on fee sensitivity).

Mirrors AlpacaBroker's interface shape so ExecutionNode/RiskManagerAgent/
PositionMonitor can treat forex as a third asset class the same way they
already treat equity vs crypto — see broker_routing.py.

Differences from AlpacaBroker:
* **Symbol format**: OANDA uses underscore instrument names (``EUR_USD``),
  not yfinance's forex suffix format (``EURUSD=X``) used elsewhere in this
  bot for HISTORICAL data/indicators. `to_oanda_instrument`/
  `from_oanda_instrument` convert between the two.
* **Sizing**: OANDA orders are placed in whole ``units`` of the base
  currency (positive = buy, negative = sell) — NOT notional USD like
  Alpaca's fractional-share orders. Since price ~ 1.0-1.5 for majors,
  this bot's existing ``qty = notional_usd / price`` sizing math already
  produces a sensible unit count; we just round to the nearest whole
  unit here (OANDA doesn't support fractional units).
* **create_market_order(instrument, side, units) signature is
  POSITIONAL and side-based** (like the crypto ccxt broker), NOT
  Alpaca's ``amount=``/``notional_usd=`` kwarg style — so
  ``broker_routing.send_close_order``'s crypto-style fallback call
  works for forex without a new branch.
* **Single environment per instance**: unlike AlpacaBroker's dual
  paper+live client (both sets of Alpaca keys are normally available),
  this bot only has an OANDA *practice* (demo) account configured so
  far. One client, built from ``OANDA_ENV`` (``practice`` or ``live``)
  at construction time. The bot-wide paper/live gate in
  ``execution_node.py`` (``B033``) still decides whether a REAL order is
  ever sent — this broker will happily place a real demo-account order
  any time it's called, exactly like Alpaca's paper client does.

Reference: https://developer.oanda.com/rest-live-v20/introduction/
"""
import logging
import os

logger = logging.getLogger(__name__)

PRACTICE_HOST = "api-fxpractice.oanda.com"
LIVE_HOST = "api-fxtrade.oanda.com"


def to_oanda_instrument(symbol: str) -> str:
    """``EURUSD=X`` (yfinance forex format, used elsewhere in this bot
    for historical data) -> ``EUR_USD`` (OANDA instrument format).
    Already-underscored input passes through unchanged."""
    s = symbol.replace("=X", "").upper()
    if "_" in s:
        return s
    if len(s) == 6:
        return f"{s[:3]}_{s[3:]}"
    return s


def from_oanda_instrument(instrument: str) -> str:
    """``EUR_USD`` -> ``EURUSD=X``, the reverse of `to_oanda_instrument`,
    so audit events/config can key off the same symbol string
    MarketAnalystAgent already uses for forex indicator data."""
    return f"{instrument.replace('_', '')}=X"


class OandaBroker:
    """Minimal OANDA v20 broker client for the trading bot.

    Methods
    -------
    get_usd_balance() -> float
        Account NAV in the account's home currency (USD for this bot's
        demo account).
    is_market_open() -> bool
        Whether OANDA currently considers pricing "tradeable" for a
        reference instrument (forex closes ~Fri 17:00 ET to Sun 17:00 ET).
    get_latest_trade_price(instrument) -> float | None
        Latest mid price (accepts either symbol format, see
        `to_oanda_instrument`).
    create_market_order(instrument, side, units) -> dict
        Submit a market order. ``units`` is a POSITIVE unit count;
        ``side`` ("buy"/"sell") determines the sign sent to OANDA.
    """

    def __init__(
        self,
        api_token: str = None,
        account_id: str = None,
        environment: str = None,
    ):
        self.api_token = api_token or os.getenv("OANDA_API_TOKEN")
        self.account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
        self.environment = (environment or os.getenv("OANDA_ENV", "practice")).strip().lower()
        if self.environment not in ("practice", "live"):
            logger.warning(
                f"[OandaBroker] OANDA_ENV={self.environment!r} not in "
                f"('practice', 'live') — defaulting to 'practice' for safety."
            )
            self.environment = "practice"
        if not self.api_token or not self.account_id:
            raise ValueError(
                "OandaBroker requires OANDA_API_TOKEN and OANDA_ACCOUNT_ID "
                "in the environment (or passed explicitly)."
            )
        try:
            from oandapyV20 import API  # noqa: WPS433
        except ImportError as exc:
            raise ImportError(
                "oandapyV20 is not installed. Run `pip install oandapyV20`."
            ) from exc
        self._client = API(access_token=self.api_token, environment=self.environment)
        logger.info(
            f"[OandaBroker] inicializado — environment={self.environment} "
            f"account={self.account_id}"
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def get_usd_balance(self) -> float:
        """Return the account NAV (net asset value) in its home currency.

        Returns 0.0 on any error (and logs) — same fail-safe convention
        as AlpacaBroker.get_usd_balance().
        """
        try:
            from oandapyV20.endpoints.accounts import AccountSummary
            req = AccountSummary(accountID=self.account_id)
            resp = self._client.request(req)
            return float(resp["account"]["NAV"])
        except Exception as exc:
            logger.error(f"[OandaBroker] Error getting balance: {exc}")
            return 0.0

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------
    def is_market_open(self, reference_instrument: str = "EUR_USD") -> bool:
        """Ask OANDA's own pricing feed whether `reference_instrument`
        is currently tradeable (forex closes roughly Fri 17:00 ET to
        Sun 17:00 ET — asking OANDA directly is strictly more correct
        than reimplementing that calendar here, same rationale as
        AlpacaBroker.is_market_open's docstring).

        Returns True (fail-open) on any error, matching every other
        best-effort broker call in this bot.
        """
        try:
            from oandapyV20.endpoints.pricing import PricingInfo
            req = PricingInfo(
                accountID=self.account_id,
                params={"instruments": reference_instrument},
            )
            resp = self._client.request(req)
            prices = resp.get("prices") or []
            if not prices:
                return True
            return bool(prices[0].get("tradeable", True))
        except Exception as exc:
            logger.error(f"[OandaBroker] is_market_open() failed (fail-open, allowing): {exc}")
            return True

    # ------------------------------------------------------------------
    # Symbol validation (best-effort; OANDA's fixed instrument list
    # rarely changes, so unlike Alpaca's ~10k-symbol universe there's no
    # real "delisted" risk here — this always returns True for any
    # instrument already configured in config.yaml's forex symbol list).
    # ------------------------------------------------------------------
    def is_symbol_tradeable(self, symbol: str) -> bool:
        return True

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_latest_trade_price(self, symbol: str) -> float | None:
        """Latest mid price for `symbol` (either ``EURUSD=X`` or
        ``EUR_USD`` format). Returns None (never raises) on failure —
        same fail-open contract as AlpacaBroker.get_latest_trade_price.
        """
        instrument = to_oanda_instrument(symbol)
        try:
            from oandapyV20.endpoints.pricing import PricingInfo
            req = PricingInfo(
                accountID=self.account_id,
                params={"instruments": instrument},
            )
            resp = self._client.request(req)
            prices = resp.get("prices") or []
            if not prices:
                return None
            bids = prices[0].get("bids") or []
            asks = prices[0].get("asks") or []
            if not bids or not asks:
                return None
            bid = float(bids[0]["price"])
            ask = float(asks[0]["price"])
            mid = (bid + ask) / 2.0
            return mid if mid > 0 else None
        except Exception as exc:
            logger.error(f"[OandaBroker] get_latest_trade_price({symbol}) failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------
    def create_market_order(self, symbol: str, side: str, units: float) -> dict:
        """Submit a market order. Positional (symbol, side, units) —
        matches the crypto ccxt broker's calling convention so
        `broker_routing.send_close_order`'s generic fallback branch
        works for forex unmodified.

        ``units`` must be a positive count; ``side`` ("buy"/"sell")
        determines the sign OANDA expects (positive units = buy,
        negative = sell). OANDA requires a whole-number unit count —
        this rounds to the nearest integer (minimum 1).

        Returns a dict with ``status`` ("filled"/"failed") — see
        AlpacaBroker.create_market_order's return shape for the
        convention every broker adapter in this bot follows.
        """
        side_norm = side.lower()
        if side_norm not in ("buy", "sell"):
            return {"status": "failed", "error": f"invalid side: {side}"}

        instrument = to_oanda_instrument(symbol)
        unit_count = max(1, round(abs(float(units))))
        signed_units = unit_count if side_norm == "buy" else -unit_count

        try:
            from oandapyV20.endpoints.orders import OrderCreate
        except ImportError as exc:
            return {"status": "failed", "error": f"oandapyV20 import failed: {exc}"}

        order_data = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(signed_units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
            }
        }
        try:
            req = OrderCreate(accountID=self.account_id, data=order_data)
            resp = self._client.request(req)
            fill = resp.get("orderFillTransaction")
            if fill is None:
                # Order created but not immediately filled (rare for a
                # market order, e.g. FOK cancel) -- report what OANDA
                # actually did instead of guessing.
                cancel = resp.get("orderCancelTransaction")
                reason = cancel.get("reason") if cancel else "no_fill_transaction"
                return {
                    "status": "failed",
                    "error": f"order not filled: {reason}",
                    "symbol": instrument,
                }
            filled_units = fill.get("units")
            fill_price = fill.get("price")
            return {
                "id": str(fill.get("id", "")),
                "status": "filled",
                "symbol": instrument,
                "side": side_norm,
                "qty": str(abs(float(filled_units))) if filled_units is not None else None,
                "filled": str(abs(float(filled_units))) if filled_units is not None else None,
                "filled_avg_price": str(fill_price) if fill_price is not None else None,
                "submitted_at": fill.get("time"),
                "endpoint": self.environment,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "symbol": instrument,
                "endpoint": self.environment,
            }
