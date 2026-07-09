"""
yfinance wrapper con retry + backoff.

Sprint 9: el bot solía reventar con `JSONDecodeError` para los 15 feeds (5 assets × 3 timeframes).
Tres causas posibles concurrentes:
- yfinance 0.2.40 desactualizado (resuelto en requirements.txt: yfinance>=1.0)
- Anti-bot de Yahoo desde data-center IPs (resuelto via curl_cffi impersonation en yf_session.py)
- Rate limit transitorio (resuelto aquí con retries + exponential backoff)

API:
    df = safe_yf_download("SPY", period="60d", interval="1h", max_retries=3)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from src.data.yf_session import get_yf_session

log = logging.getLogger(__name__)


def safe_yf_download(
    ticker: str,
    period: str = "60d",
    interval: str = "1d",
    *,
    max_retries: int = 3,
    backoff_base: float = 1.5,
    auto_adjust: bool = False,
    progress: bool = False,
) -> Optional[pd.DataFrame]:
    """
    `yf.download` con retry + backoff + curl_cffi session.

    Devuelve None si todos los reintentos fallan (no lanza excepción).
    """
    import yfinance as yf

    session = get_yf_session()
    last_err: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            kwargs = {
                "period": period,
                "interval": interval,
                "progress": progress,
                "auto_adjust": auto_adjust,
            }
            if session is not None:
                kwargs["session"] = session
            df = yf.download(ticker, **kwargs)

            if df is None or df.empty:
                raise RuntimeError(f"empty dataframe for {ticker}@{interval}")

            # Sprint 6 fail-fast: si los datos están vacíos o no son parseables, retry
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(how="all")
            if df.empty:
                raise RuntimeError(f"all-NaN dataframe for {ticker}@{interval}")

            if attempt > 1:
                log.info(f"[yfinance] {ticker}@{interval} OK on attempt {attempt}/{max_retries}")
            return df

        except Exception as e:
            last_err = e
            wait = backoff_base ** attempt
            log.warning(
                f"[yfinance] {ticker}@{interval} attempt {attempt}/{max_retries} failed: "
                f"{type(e).__name__}: {e} — retrying in {wait:.1f}s"
            )
            if attempt < max_retries:
                time.sleep(wait)

    log.error(f"[yfinance] {ticker}@{interval} gave up after {max_retries} retries: {last_err}")
    return None
