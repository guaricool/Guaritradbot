"""
Sprint 0 fix (Guaritradbot) — MarketAnalystAgent reescrito.

Fixes vs la versión previa:
1. RSI ahora usa suavizado de WILDER (EMA con alpha=1/period), no SMA — estándar institucional.
2. ATR(14) calculado y publicado en el state para que risk_agent pueda usar stop-loss basado en volatilidad real.
3. Timeframe "4h" resampleado desde 60m (descargamos 60m y agregamos a 4h) en vez de mentir con 60m.
4. Period de descarga aumenta según timeframe para garantizar mínimo de velas para EMA_50 / RSI.
"""
import yfinance as yf
import pandas as pd
import numpy as np


# Period mínimo (en días) para garantizar al menos 80 velas tras warmup.
# 80 velas ≈ lo suficiente para EMA_50 + RSI_14 + ATR_14.
def _min_period_days(interval: str) -> str:
    return {
        "15m": "30d",
        "60m": "60d",
        "1d": "2y",
    }.get(interval, "60d")


def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI estándar con suavizado de Wilder (EMA con alpha=1/period).

    El que estaba en v1 usaba `rolling(window=14).mean()`, que es SMA y da
    señales más lentas. Wilder es lo que usan TradingView, TA-Lib y todos los
    backtesters institucionales.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)  # default neutral donde no hay data


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder). True Range = max(H-L, |H-Cp|, |L-Cp|)."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _resample_ohlcv(df_60m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample OHLCV 60m → 4h (o lo que sea)."""
    if df_60m.empty:
        return df_60m
    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    return (
        df_60m.resample(rule)
        .agg(agg)
        .dropna(subset=["Close"])
    )


class MarketAnalystAgent:
    """
    Agente responsable de descargar datos y calcular indicadores.
    Estilo NautilusTrader DataNode: pub/sub via event_bus.
    """

    def __init__(self, event_bus=None):
        self.event_bus = event_bus

    def fetch_one(self, asset: str, interval: str = "1d", period: str = "1y") -> "pd.DataFrame | None":
        """Helper público (Sprint 2): trae datos OHLCV de un solo asset. Usado por PositionMonitor."""
        try:
            import yfinance as yf
            tf_map = {"15m": "15m", "60m": "60m", "1h": "60m", "4h": "60m", "1d": "1d"}
            yf_interval = tf_map.get(interval, "1d")
            df = yf.download(asset, period=period, interval=yf_interval, progress=False, auto_adjust=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(how="all")
            return df if not df.empty else None
        except Exception:
            return None

    def fetch_and_analyze(self, inputs: dict, state: dict):
        assets = inputs.get("assets", [])
        timeframes = inputs.get("timeframes", ["1h"])

        # Mapeo conceptual timeframes → intervalo yfinance real.
        # 4h se obtiene resampleando desde 60m (lo manejamos abajo).
        tf_map = {
            "15m": ("15m", None),     # yfinance nativo
            "1h":  ("60m", None),     # yfinance nativo
            "4h":  ("60m", "4h"),     # descargar 60m y resamplear a 4h
        }

        print(f"[MarketAnalystAgent] Fetching {len(assets)} assets × {len(timeframes)} timeframes...")
        data = {}
        for asset in assets:
            data[asset] = {}
            for tf in timeframes:
                yf_interval, resample_rule = tf_map.get(tf, ("1d", None))
                period = _min_period_days(yf_interval)
                try:
                    df = yf.download(
                        asset,
                        period=period,
                        interval=yf_interval,
                        progress=False,
                        auto_adjust=False,
                    )
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df = df.dropna(how="all")
                    if df.empty:
                        print(f"  ⚠️  {asset}@{tf}: sin datos")
                        continue

                    if resample_rule:
                        df = _resample_ohlcv(df, resample_rule)

                    close = df["Close"]

                    # EMAs (tendencia)
                    df["EMA_20"] = close.ewm(span=20, adjust=False).mean()
                    df["EMA_50"] = close.ewm(span=50, adjust=False).mean()

                    # RSI Wilder (estándar)
                    df["RSI"] = _wilder_rsi(close, 14)

                    # MACD (señal + línea)
                    ema12 = close.ewm(span=12, adjust=False).mean()
                    ema26 = close.ewm(span=26, adjust=False).mean()
                    df["MACD"] = ema12 - ema26
                    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()

                    # ATR(14) — Sprint 0 nuevo, lo usará risk_agent
                    df["ATR_14"] = _atr(df, 14)

                    df = df.dropna(subset=["Close", "EMA_50", "RSI", "MACD", "ATR_14"])
                    if df.empty:
                        print(f"  ⚠️  {asset}@{tf}: sin velas tras warmup")
                        continue

                    data[asset][tf] = df
                    print(
                        f"  ✅ {asset}@{tf}: {len(df)} velas | "
                        f"close=${close.iloc[-1]:.2f} | "
                        f"RSI={df['RSI'].iloc[-1]:.1f} | "
                        f"ATR={df['ATR_14'].iloc[-1]:.4f}"
                    )
                except Exception as e:
                    print(f"  ❌ {asset}@{tf}: {e}")

        # Publicar en event bus (estilo Nautilus DataNode)
        if self.event_bus:
            self.event_bus.publish("MARKET_DATA_READY", data)

        return {"market_data": data}
