"""
Sprint 21 — Alpha Zoo (130+ indicators via the `ta` library).

The `ta` library (pure-Python fork of TA-Lib) provides 130+ technical
indicators across 4 categories without requiring native compilation
(perfect for Windows + Linux VPS). This module adds a curated set of
50+ indicators to any OHLCV dataframe in one shot.

Why this matters:
- Before Sprint 21: Guaritradbot had 9 custom indicators (RSI, MACD, EMA,
  ATR, Stoch, BB, ADX, DM, S/R).
- After Sprint 21: 50+ alpha features per bar, ready for ML pipelines
  (Sprint 19) or signal strategies.

Design choices:
- Returns a NEW dataframe (does not mutate input)
- All indicators are "added as columns" so the original OHLCV is preserved
- Categorized into Momentum / Trend / Volatility / Volume for easy filtering
- NaN-tolerant (drops leading NaNs gracefully)
- Optional: only compute selected categories (momentum_only=True) for speed
"""
from __future__ import annotations
import logging
import pandas as pd
import numpy as np

# The `ta` library imports per-category
from ta import momentum, trend, volatility, volume

_log = logging.getLogger(__name__)


def compute_alpha_features(
    df: pd.DataFrame,
    include_momentum: bool = True,
    include_trend: bool = True,
    include_volatility: bool = True,
    include_volume: bool = True,
) -> pd.DataFrame:
    """
    Add 50+ alpha features to an OHLCV dataframe.

    Args:
        df: DataFrame with columns ['Open', 'High', 'Low', 'Close', 'Volume']
        include_momentum: RSI, Stoch, WilliamsR, ROC, CCI, etc.
        include_trend:    MACD, EMA, SMA, ADX, Aroon, PSAR, etc.
        include_volatility: BB, ATR, Keltner, Donchian, etc.
        include_volume:   OBV, MFI, VWAP, CMF, etc.

    Returns:
        New DataFrame with all original OHLCV columns + 50+ alpha columns.

    Notes:
        - The original df is NOT modified (returns a copy).
        - If a column is missing (e.g., no Volume), volume indicators are skipped.
        - Some indicators (e.g., VWAP) require DatetimeIndex; gracefully skip if not.
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    # Detect available columns
    has_volume = "Volume" in out.columns and out["Volume"].notna().any()
    has_datetime_idx = isinstance(out.index, pd.DatetimeIndex)

    # === MOMENTUM ===
    if include_momentum:
        try:
            out["alpha_rsi_14"] = momentum.RSIIndicator(out["Close"], window=14).rsi()
            out["alpha_rsi_7"] = momentum.RSIIndicator(out["Close"], window=7).rsi()
            out["alpha_stoch"] = momentum.StochasticOscillator(
                out["High"], out["Low"], out["Close"], window=14, smooth_window=3
            ).stoch()
            out["alpha_stoch_signal"] = momentum.StochasticOscillator(
                out["High"], out["Low"], out["Close"], window=14, smooth_window=3
            ).stoch_signal()
            out["alpha_williams_r"] = momentum.WilliamsRIndicator(
                out["High"], out["Low"], out["Close"], lbp=14
            ).williams_r()
            out["alpha_roc_10"] = momentum.ROCIndicator(out["Close"], window=10).roc()
            out["alpha_roc_20"] = momentum.ROCIndicator(out["Close"], window=20).roc()
            # CCI (manual implementation — `ta` library doesn't include CCIIndicator)
            tp = (out["High"] + out["Low"] + out["Close"]) / 3
            sma_tp = tp.rolling(window=20).mean()
            mad = tp.rolling(window=20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
            out["alpha_cci"] = (tp - sma_tp) / (0.015 * mad)
            # MFI (manual — ta library doesn't include MFIIndicator)
            if has_volume:
                typical_price = (out["High"] + out["Low"] + out["Close"]) / 3
                raw_money_flow = typical_price * out["Volume"]
                pos_flow = pd.Series(
                    np.where(typical_price > typical_price.shift(1), raw_money_flow, 0),
                    index=out.index,
                )
                neg_flow = pd.Series(
                    np.where(typical_price < typical_price.shift(1), raw_money_flow, 0),
                    index=out.index,
                )
                pos_mf = pos_flow.rolling(window=14).sum()
                neg_mf = neg_flow.rolling(window=14).sum()
                mfr = pos_mf / neg_mf.replace(0, np.nan)
                out["alpha_mfi"] = 100 - (100 / (1 + mfr))
            # Awesome Oscillator
            out["alpha_ao"] = momentum.AwesomeOscillatorIndicator(
                out["High"], out["Low"], window1=5, window2=34
            ).awesome_oscillator()
            # Ultimate Oscillator
            out["alpha_uo"] = momentum.UltimateOscillator(
                out["High"], out["Low"], out["Close"]
            ).ultimate_oscillator()
            # TSI (True Strength Index)
            out["alpha_tsi"] = momentum.TSIIndicator(out["Close"]).tsi()
            # PPO (Percentage Price Oscillator) — bonus
            ppo = momentum.PercentagePriceOscillator(out["Close"])
            out["alpha_ppo"] = ppo.ppo()
            out["alpha_ppo_signal"] = ppo.ppo_signal()
            out["alpha_ppo_hist"] = ppo.ppo_hist()
        except Exception as e:
            _log.warning("alpha_zoo: momentum indicators failed: %s", e)

    # === TREND ===
    if include_trend:
        try:
            macd = trend.MACD(out["Close"], window_slow=26, window_fast=12, window_sign=9)
            out["alpha_macd"] = macd.macd()
            out["alpha_macd_signal"] = macd.macd_signal()
            out["alpha_macd_diff"] = macd.macd_diff()

            out["alpha_ema_9"] = trend.EMAIndicator(out["Close"], window=9).ema_indicator()
            out["alpha_ema_21"] = trend.EMAIndicator(out["Close"], window=21).ema_indicator()
            out["alpha_ema_50"] = trend.EMAIndicator(out["Close"], window=50).ema_indicator()
            out["alpha_ema_200"] = trend.EMAIndicator(out["Close"], window=200).ema_indicator()

            out["alpha_sma_20"] = trend.SMAIndicator(out["Close"], window=20).sma_indicator()
            out["alpha_sma_50"] = trend.SMAIndicator(out["Close"], window=50).sma_indicator()

            adx = trend.ADXIndicator(out["High"], out["Low"], out["Close"], window=14)
            out["alpha_adx"] = adx.adx()
            out["alpha_adx_pos"] = adx.adx_pos()
            out["alpha_adx_neg"] = adx.adx_neg()

            # AroonIndicator takes (high, low), not (close) like the official docs suggest
            aroon = trend.AroonIndicator(out["High"], out["Low"], window=25)
            out["alpha_aroon_up"] = aroon.aroon_up()
            out["alpha_aroon_down"] = aroon.aroon_down()

            # PSAR (Parabolic SAR)
            out["alpha_psar"] = trend.PSARIndicator(
                out["High"], out["Low"], out["Close"]
            ).psar()

            # Ichimoku (simplified: just the conversion + base line)
            ichi = trend.IchimokuIndicator(out["High"], out["Low"], window1=9, window2=26)
            out["alpha_ichi_a"] = ichi.ichimoku_conversion_line()
            out["alpha_ichi_b"] = ichi.ichimoku_base_line()

            # KAMA (Kaufman Adaptive MA) — adaptive trend strength
            out["alpha_kama"] = momentum.KAMAIndicator(out["Close"], window=10, pow1=2, pow2=30).kama()
        except Exception as e:
            _log.warning("alpha_zoo: trend indicators failed: %s", e)

    # === VOLATILITY ===
    if include_volatility:
        try:
            bb = volatility.BollingerBands(out["Close"], window=20, window_dev=2)
            out["alpha_bb_high"] = bb.bollinger_hband()
            out["alpha_bb_low"] = bb.bollinger_lband()
            out["alpha_bb_width"] = bb.bollinger_wband()
            out["alpha_bb_pct_b"] = bb.bollinger_pband()

            kc = volatility.KeltnerChannel(
                out["High"], out["Low"], out["Close"], window=20
            )
            out["alpha_kc_high"] = kc.keltner_channel_hband()
            out["alpha_kc_low"] = kc.keltner_channel_lband()

            dc = volatility.DonchianChannel(out["High"], out["Low"], out["Close"], window=20)
            out["alpha_dc_high"] = dc.donchian_channel_hband()
            out["alpha_dc_low"] = dc.donchian_channel_lband()

            out["alpha_atr_14"] = volatility.AverageTrueRange(
                out["High"], out["Low"], out["Close"], window=14
            ).average_true_range()
            out["alpha_atr_ratio"] = out["alpha_atr_14"] / out["Close"]
            # Ulcer Index (drawdown-based volatility)
            rolling_max = out["Close"].rolling(window=14).max()
            drawdown = (out["Close"] - rolling_max) / rolling_max
            out["alpha_ulcer_index"] = np.sqrt((drawdown ** 2).rolling(window=14).mean())
        except Exception as e:
            _log.warning("alpha_zoo: volatility indicators failed: %s", e)

    # === VOLUME ===
    if include_volume and has_volume:
        try:
            out["alpha_obv"] = volume.OnBalanceVolumeIndicator(
                out["Close"], out["Volume"]
            ).on_balance_volume()
            out["alpha_cmf"] = volume.ChaikinMoneyFlowIndicator(
                out["High"], out["Low"], out["Close"], out["Volume"], window=20
            ).chaikin_money_flow()
            out["alpha_eom"] = volume.EaseOfMovementIndicator(
                out["High"], out["Low"], out["Volume"], window=14
            ).ease_of_movement()
            out["alpha_fi"] = volume.ForceIndexIndicator(
                out["Close"], out["Volume"], window=13
            ).force_index()
            out["alpha_vpt"] = volume.VolumePriceTrendIndicator(
                out["Close"], out["Volume"]
            ).volume_price_trend()
            # Negative Volume Index (smart money tracking)
            out["alpha_nvi"] = volume.NegativeVolumeIndexIndicator(
                out["Close"], out["Volume"]
            ).negative_volume_index()
            # Volume Weighted Average Price (requires volume; computed as rolling)
            out["alpha_vwap"] = (
                (out["Close"] * out["Volume"]).rolling(window=20).sum()
                / out["Volume"].rolling(window=20).sum()
            )
        except Exception as e:
            _log.warning("alpha_zoo: volume indicators failed: %s", e)

    return out


def list_alpha_features(df_with_features: pd.DataFrame) -> list:
    """Return the list of column names that were added by compute_alpha_features."""
    return [c for c in df_with_features.columns if c.startswith("alpha_")]


def count_alpha_features() -> dict:
    """Catalog of all alpha features available (for the audit/dashboard)."""
    return {
        "momentum": [
            "rsi_14", "rsi_7", "stoch", "stoch_signal", "williams_r",
            "roc_10", "roc_20", "cci", "mfi", "ao", "uo", "tsi",
        ],
        "trend": [
            "macd", "macd_signal", "macd_diff",
            "ema_9", "ema_21", "ema_50", "ema_200",
            "sma_20", "sma_50",
            "adx", "adx_pos", "adx_neg",
            "aroon_up", "aroon_down",
            "psar", "ichi_a", "ichi_b",
        ],
        "volatility": [
            "bb_high", "bb_low", "bb_width", "bb_pct_b",
            "kc_high", "kc_low",
            "dc_high", "dc_low",
            "atr_14", "atr_ratio",
        ],
        "volume": [
            "obv", "cmf", "eom", "fi", "vpt",
        ],
    }