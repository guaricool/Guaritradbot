"""
Sprint 43 M10 — DEPRECATED module.

This file is no longer imported by the bot. The live path uses
`market_analyst.safe_yf_download()` (in src/agents/market_analyst.py)
which has retry + curl_cffi + the B015 bug fix.

Kept here as a historical reference / standalone CLI for one-off
downloads, but with the imports and `__main__` block DISABLED so
that any future import attempt that accidentally brings this code
into the live path doesn't reintroduce the B015 bug (no retry /
backoff / curl_cffi). To run a one-off download, use:

  python -c "from src.data import download; download.download_data(...)"

(after un-commenting the import / main block).

The `download_data` function below is preserved for reference
but is no longer executed.
"""
# import yfinance as yf
# import pandas as pd
# import os


def download_data(ticker, interval, start_date=None, end_date=None, period="60d"):
    raise NotImplementedError(
        "src/data/download.py is deprecated. "
        "Use src.agents.market_analyst.safe_yf_download instead, "
        "which has retry + curl_cffi + the B015 bug fix."
    )


# Original code preserved below for reference. UNCOMMENT AT YOUR
# OWN RISK — re-enabling the imports + the __main__ block
# reintroduces the B015 bug (no retry, no curl_cffi, raw yf.download).
#
# import yfinance as yf
# import pandas as pd
# import os
#
# def download_data(ticker, interval, start_date=None, end_date=None, period="60d"):
#     """
#     Downloads historical data from Yahoo Finance.
#     ... (deprecated)
#     """
#     ... (deprecated, see safe_yf_download)
#
# if __name__ == "__main__":
#     ... (deprecated)
