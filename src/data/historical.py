"""
Sprint 43 M10 — DEPRECATED module.

The docstring claimed "Binance via CCXT" but the code actually
instantiates `ccxt.kraken(...)`. If anyone imported this and
expected Binance data, they'd silently get Kraken data (which
might support different pairs or have different rates).

No one imports this file in production — the live historical
fetcher is part of `market_analyst.fetch_and_analyze()`. Kept
here only for reference, with the active code DISABLED so the
Kraken/Binance mismatch can't be reintroduced by an
unwitting `from src.data.historical import ...`.

The `fetch_historical_data` function below is preserved for
reference but raises NotImplementedError when called.
"""
# import ccxt
# import pandas as pd
# import datetime
# import time


def fetch_historical_data(symbol: str, timeframe: str, since: str, limit: int = 1000) -> "pd.DataFrame":
    raise NotImplementedError(
        "src/data/historical.py is deprecated AND BROKEN: the "
        "docstring says Binance but the code uses ccxt.kraken. "
        "Use src.agents.market_analyst for the live historical "
        "fetch (which routes to the correct exchange per asset). "
        "If you need raw historical data, use ccxt.binanceus or "
        "ccxt.binance directly with explicit error handling."
    )


# Original code preserved below for reference. UNCOMMENT AT YOUR
# OWN RISK — re-enabling reintroduces the Kraken/Binance
# mismatch that the audit flagged as a latent bug.
#
# import ccxt
# import pandas as pd
# import datetime
# import time
#
# def fetch_historical_data(symbol, timeframe, since, limit=1000):
#     """DEPRECATED: docstring said Binance but used ccxt.kraken."""
#     exchange = ccxt.kraken({'enableRateLimit': True})
#     ... (deprecated, see market_analyst)
