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
from src.data.yf_safe import safe_yf_download


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


def _dm(df: pd.DataFrame, period: int = 14) -> tuple:
    """
    Directional Movement (Wilder). Devuelve (+DI, -DI, ADX).
    Inspirado en el Manual del Buen Trader Algorítmico — DM/ADX
    mide la fuerza de la tendencia (no la dirección).
    """
    high = df["High"]
    low = df["Low"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    tr = pd.concat(
        [
            (high - low),
            (high - prev_high).abs(),
            (low - prev_low).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_dm = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_dm.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_dm.replace(0, np.nan))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return (plus_di.fillna(0), minus_di.fillna(0), adx.fillna(0))


def _stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> tuple:
    """
    Stochastic Oscillator (%K, %D). Inspirado en el Manual.
    %K = (close - lowest_low) / (highest_high - lowest_low) * 100
    %D = SMA(%K, 3)
    """
    close = df["Close"]
    low_k = df["Low"].rolling(k_period).min()
    high_k = df["High"].rolling(k_period).max()
    k = ((close - low_k) / (high_k - low_k).replace(0, np.nan)) * 100
    d = k.rolling(d_period).mean()
    return (k.fillna(50), d.fillna(50))


def _bollinger(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> tuple:
    """
    Bollinger Bands (BB_upper, BB_middle, BB_lower). Inspirado en el Manual.
    Si close rompe la banda superior → señal fuerte en esa dirección;
    si high fuera / low dentro → reversión.
    """
    close = df["Close"]
    middle = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    # Sprint 43 L6 fix: don't bfill() the warmup-period NaNs.
    # bfill retroactively fills the oldest historical bars with
    # values from LATER bars — a form of look-ahead that biases
    # any backtest running on this data. The decision-bar (the
    # most recent bar) is unaffected, but the first `period`
    # bars get contaminated. We leave the NaNs in place and let
    # downstream consumers (which already handle NaN via the
    # Sprint 43 C3 isfinite checks) deal with them. If a
    # specific caller needs a value for those early bars, they
    # can fill explicitly with a documented method (e.g. .ffill()
    # for forward-only fill, or NaN to mean "warmup incomplete").
    return (upper, middle, lower)


def _support_resistance(df: pd.DataFrame, window: int = 50) -> tuple:
    """
    Soporte y resistencia dinámicos (rolling max/min). Inspirado en el Manual.
    Útil para identificar niveles clave donde el precio puede revertir.

    Sprint 43 L6 fix: no bfill() — see the comment in _bollinger.
    """
    resistance = df["High"].rolling(window).max()
    support = df["Low"].rolling(window).min()
    return (support, resistance)


def _resample_ohlcv(df_60m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample OHLCV 60m → 4h (o lo que sea).

    Sprint 43 H12 fix: drop the current in-progress bar from the
    resampled output. Without this fix, a 60m bar at 14:30 was
    being included in the 12:00-16:00 4h bucket that was only
    2.5h old, making the 4h Open/High/Low/Close a mix of
    historical + live-in-progress data. The 4h indicator derived
    from that bar would change intra-bar as new 60m ticks
    arrived, which is wrong for any "I want to act on a closed
    4h bar" use case.
    Now: we drop the last (in-progress) bucket. The historical
    4h bars are still there; the current partial one is
    excluded. If the caller wants the partial for display
    purposes, they can ask explicitly.
    """
    if df_60m.empty:
        return df_60m
    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    resampled = (
        df_60m.resample(rule)
        .agg(agg)
        .dropna(subset=["Close"])
    )
    if resampled.empty:
        return resampled
    # Drop the last (in-progress) bar. We detect "in-progress"
    # by COUNTING how many source bars actually landed in the
    # last bucket. A complete 4h bucket of 60m bars has 4
    # source rows; a 3h-in-progress bucket has 3. The simple
    # "is the bucket end in the future" check was wrong
    # because a complete bucket [04:00, 08:00) with 60m bars
    # at 04, 05, 06, 07 has end 08:00 and last 60m at 07:00
    # — 08:00 > 07:00, but the bucket IS complete.
    try:
        last_bucket_start = resampled.index[-1]
        rule_seconds = pd.Timedelta(_rule_to_timedelta(rule)).total_seconds()
        # How many 60m bars are in the last bucket?
        last_bucket_60m_bars = df_60m[
            (df_60m.index >= last_bucket_start) &
            (df_60m.index < last_bucket_start + pd.Timedelta(seconds=rule_seconds))
        ]
        # Expected number of 60m bars in a complete bucket.
        # For rule "4h": rule_seconds=14400, expected=4 hourly bars.
        # For rule "1h": rule_seconds=3600, expected=1.
        # For rule "1d": rule_seconds=86400, expected=24.
        expected_bars = int(rule_seconds // 3600) or 1  # floor of hours
        if len(last_bucket_60m_bars) < expected_bars:
            # In-progress — drop it
            resampled = resampled.iloc[:-1]
    except Exception:
        # If we can't parse the rule (unusual), fall back to
        # the old behavior (drop nothing). Better to show a
        # partial bar than to drop everything.
        pass
    return resampled


def _rule_to_timedelta(rule: str) -> str:
    """Convert a pandas resample rule (e.g. '4h', '1D') to a
    Timedelta-parseable string (e.g. '4h', '1D').
    This is a pass-through — pandas accepts the same rule
    format for Timedelta. The helper exists so the H12 fix
    has a single point to extend if we ever need to handle
    weird rules (e.g. '1M' for month-end).
    """
    return rule


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
        """Helper público (Sprint 2): trae datos OHLCV de un solo asset. Usado por PositionMonitor.

        Sprint 43 M6 fix: when `interval='4h'`, the previous code
        mapped it to yfinance's 60m and returned 4x more bars,
        labeled as '4h' but actually 1h data. Now we resample
        60m → 4h via _resample_ohlcv (the same helper
        fetch_and_analyze uses) so the result is consistent.
        The fix is dormant when called with interval='1d' (the
        only current caller per the audit).
        """
        try:
            import yfinance as yf
            tf_map = {"15m": "15m", "60m": "60m", "1h": "60m", "4h": "60m", "1d": "1d"}
            yf_interval = tf_map.get(interval, "1d")
            # Sprint 9: use safe_yf_download (retry + curl_cffi session).
            df = safe_yf_download(asset, period=period, interval=yf_interval, max_retries=3)
            if df is None:
                return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(how="all")
            if df.empty:
                return None
            # Sprint 43 M6: if caller asked for 4h, resample the
            # 60m bars we just downloaded. Otherwise return as-is.
            # Note: _resample_ohlcv is a module-level function
            # (defined as `def _resample_ohlcv(df_60m, rule)`,
            # not a method), so we call it without self.
            if interval == "4h":
                df = _resample_ohlcv(df, "4h")
            # Calcular todos los indicadores (incluyendo los nuevos del PDF2)
            close = df["Close"]
            df["EMA_20"] = close.ewm(span=20, adjust=False).mean()
            df["EMA_50"] = close.ewm(span=50, adjust=False).mean()
            df["RSI"] = _wilder_rsi(close, 14)
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            df["MACD"] = ema12 - ema26
            df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
            df["ATR_14"] = _atr(df, 14)
            plus_di, minus_di, adx = _dm(df, 14)
            df["DI_Plus_14"] = plus_di
            df["DI_Minus_14"] = minus_di
            df["ADX_14"] = adx
            stoch_k, stoch_d = _stochastic(df, 14)
            df["Stoch_K"] = stoch_k
            df["Stoch_D"] = stoch_d
            bb_u, bb_m, bb_l = _bollinger(df, 20, 2.0)
            df["BB_Upper"] = bb_u
            df["BB_Middle"] = bb_m
            df["BB_Lower"] = bb_l
            sup, res = _support_resistance(df, 50)
            df["Support_50"] = sup
            df["Resistance_50"] = res
            return df
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
                    # Sprint 9: use safe_yf_download (retry + curl_cffi + backoff).
                    df = safe_yf_download(
                        asset,
                        period=period,
                        interval=yf_interval,
                        max_retries=3,
                    )
                    if df is None:
                        print(f"  ⚠️  {asset}@{tf}: descarga falló tras 3 reintentos")
                        fail_count += 1
                        continue
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

                    # PDF Manual: añadir indicadores faltantes (DM, Estocástico, Bollinger, S/R)
                    plus_di, minus_di, adx = _dm(df, 14)
                    df["DI_Plus_14"] = plus_di
                    df["DI_Minus_14"] = minus_di
                    df["ADX_14"] = adx
                    stoch_k, stoch_d = _stochastic(df, 14)
                    df["Stoch_K"] = stoch_k
                    df["Stoch_D"] = stoch_d
                    bb_upper, bb_middle, bb_lower = _bollinger(df, 20, 2.0)
                    df["BB_Upper"] = bb_upper
                    df["BB_Middle"] = bb_middle
                    df["BB_Lower"] = bb_lower
                    support, resistance = _support_resistance(df, 50)
                    df["Support_50"] = support
                    df["Resistance_50"] = resistance

                    df = df.dropna(subset=["Close", "EMA_50", "RSI", "MACD", "ATR_14",
                                            "DI_Plus_14", "DI_Minus_14", "ADX_14",
                                            "Stoch_K", "Stoch_D",
                                            "BB_Upper", "BB_Middle", "BB_Lower",
                                            "Support_50", "Resistance_50"])
                    if df.empty:
                        print(f"  ⚠️  {asset}@{tf}: sin velas tras warmup")
                        fail_count += 1
                        continue

                    data[asset][tf] = df
                    print(
                        f"  ✅ {asset}@{tf}: {len(df)} velas | "
                        f"close=${close.iloc[-1]:.2f} | "
                        f"RSI={df['RSI'].iloc[-1]:.1f} | "
                        f"ADX={df['ADX_14'].iloc[-1]:.1f} | "
                        f"StochK={df['Stoch_K'].iloc[-1]:.1f} | "
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
            # Sprint 43 C6 fix: total data-feed failure is a critical
            # state — the bot has no market context. Without this
            # alert Carlos would only know if he happened to look at
            # the dashboard. SYSTEM_ERROR → NotificationAgent →
            # Telegram, regardless of paper/live.
            if self.event_bus is not None:
                try:
                    self.event_bus.publish("SYSTEM_ERROR", {
                        "kind": "MARKET_DATA_TOTAL_FAILURE",
                        "fail_count": fail_count,
                        "assets_requested": len(assets) * len(timeframes),
                        "error": (f"📉 Market data: TODOS los {fail_count} feeds fallaron. "
                                  f"Bot operando a ciegas."),
                    })
                except Exception as e:
                    print(f"[MarketAnalyst] ⚠️ No se pudo publicar SYSTEM_ERROR: {e}")
        else:
            self.recover()

        if self.event_bus:
            self.event_bus.publish("MARKET_DATA_READY", data)

        return {"market_data": data}
