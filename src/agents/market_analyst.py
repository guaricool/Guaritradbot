"""
Sprint 0+6 — MarketAnalystAgent.

Sprint 0 fixes:
1. RSI Wilder smoothing (estándar institucional).
2. ATR(14) calculado y publicado en el state para risk_agent.
3. Timeframe "4h" resampleado desde 60m.
4. Period de descarga proporcional al timeframe.

Sprint 6 añade:
5. Fail-fast data integrity: cada vela se valida contra NaN/Inf/
   negativos antes de procesarse. Si yfinance devuelve basura,
   marcamos el estado del componente como DEGRADED o FAULTED.
6. Component State Machine: PRE_INIT → READY → RUNNING con
   transiciones auditadas.
"""
import yfinance as yf
import pandas as pd
import numpy as np
from src.core.component import Component, ComponentState
from src.core.data_validator import validate_dataframe, DataIntegrityError


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


class MarketAnalystAgent(Component):
    """
    Agente responsable de descargar datos y calcular indicadores.
    Estilo NautilusTrader DataNode: pub/sub via event_bus.
    Sprint 6: hereda de Component con State Machine integrada.
    """

    def __init__(self, event_bus=None, audit=None):
        super().__init__(name="MarketAnalystAgent", audit=audit)
        self.event_bus = event_bus
        self.ready()

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

    def _validate_or_fault(self, df, asset_tf: str) -> bool:
        """Sprint 6: fail-fast. Devuelve False si los datos son corruptos."""
        try:
            validate_dataframe(df)
            return True
        except DataIntegrityError as e:
            print(f"  ⚠️  {asset_tf}: data integrity fail — {e}")
            self.degrade(f"data integrity: {e}")
            return False

    def fetch_and_analyze(self, inputs: dict, state: dict):
        assets = inputs.get("assets", [])
        timeframes = inputs.get("timeframes", ["1h"])

        tf_map = {
            "15m": ("15m", None),
            "1h":  ("60m", None),
            "4h":  ("60m", "4h"),
        }

        self.start() if self.state == ComponentState.READY else None
        print(f"[MarketAnalystAgent] Fetching {len(assets)} assets × {len(timeframes)} timeframes...")
        data = {}
        fail_count = 0
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
                        fail_count += 1
                        continue

                    if resample_rule:
                        df = _resample_ohlcv(df, resample_rule)

                    # Sprint 6 fail-fast: validar ANTES de calcular indicadores
                    if not self._validate_or_fault(df, f"{asset}@{tf}"):
                        fail_count += 1
                        continue

                    close = df["Close"]
                    df["EMA_20"] = close.ewm(span=20, adjust=False).mean()
                    df["EMA_50"] = close.ewm(span=50, adjust=False).mean()
                    df["RSI"] = _wilder_rsi(close, 14)
                    ema12 = close.ewm(span=12, adjust=False).mean()
                    ema26 = close.ewm(span=26, adjust=False).mean()
                    df["MACD"] = ema12 - ema26
                    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
                    df["ATR_14"] = _atr(df, 14)

                    df = df.dropna(subset=["Close", "EMA_50", "RSI", "MACD", "ATR_14"])
                    if df.empty:
                        print(f"  ⚠️  {asset}@{tf}: sin velas tras warmup")
                        fail_count += 1
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
                    fail_count += 1

        # Si fallaron demasiados assets, marcar como DEGRADED pero seguir
        if fail_count > 0 and fail_count < len(assets) * len(timeframes):
            self.degrade(f"{fail_count} feeds failed but workflow continues")
        elif fail_count >= len(assets) * len(timeframes):
            self.fault(f"all {fail_count} feeds failed")
        else:
            self.recover()

        if self.event_bus:
            self.event_bus.publish("MARKET_DATA_READY", data)

        return {"market_data": data}
