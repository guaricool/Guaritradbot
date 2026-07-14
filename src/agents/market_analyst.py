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
import pytz
import time as _time
from src.core.component import Component, ComponentState
from src.core.data_validator import validate_dataframe, DataIntegrityError
from src.data.yf_safe import safe_yf_download
from src.data.asset_class import get_asset_class, AssetClass

from src.core.logging_setup import get_logger
logger = get_logger(__name__)


# Sprint 46N (audit M4): NYSE/Nasdaq regular session, used both to make
# the staleness check equity-aware (_validate_or_fault) and to fix the
# 4h resample morning-bucket-always-in-progress bug (_resample_ohlcv).
# This is a coarse weekday+hours check -- it does NOT know about market
# holidays. That fuller fix (gating on Alpaca's real GET /v2/clock) is
# tracked separately under audit finding M12; this is enough to resolve
# M4's specific complaint (every night and every weekend).
_NY_TZ = pytz.timezone("America/New_York")


def _is_us_equity_market_open(now: "pd.Timestamp | None" = None) -> bool:
    """True if it's currently within NYSE/Nasdaq regular trading hours
    (Mon-Fri, 09:30-16:00 America/New_York). See module note above for
    the known holiday-calendar gap."""
    try:
        ts = pd.Timestamp.now(tz="UTC") if now is None else now
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ny = ts.tz_convert(_NY_TZ)
        if ny.weekday() >= 5:  # Saturday/Sunday
            return False
        open_t = ny.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = ny.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_t <= ny <= close_t
    except Exception:
        # Can't determine market state -- fail open to "market open" so
        # staleness/resample logic falls back to its stricter, pre-M4
        # behavior rather than silently loosening a safety check.
        return True


# Period mínimo (en días) para garantizar al menos 80 velas tras warmup.
# 80 velas ≈ lo suficiente para EMA_50 + RSI_14 + ATR_14.
def _min_period_days(interval: str) -> str:
    return {
        "15m": "30d",
        "60m": "60d",
        "1d": "2y",
    }.get(interval, "60d")


# Sprint 56: see the comment block in `_validate_or_fault` for the
# full rationale. Short version: yfinance's crypto tickers are
# ~24h behind from a VPS IP, and the multiplier-based staleness
# threshold (1.5h for 15m, 6h for 1h) over-triggers on that lag.
# 48h covers the worst yfinance lag we've seen in production
# (confirmed 22.4h on 2026-07-13) with headroom for the next
# weekend / holiday. Above 48h the feed is genuinely stuck.
_CRYPTO_STALENESS_FLOOR_S = 48 * 3600


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


def _resample_ohlcv(df_60m: pd.DataFrame, rule: str, asset: str = None) -> pd.DataFrame:
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

    Sprint 46N (audit M4): the bar-count completeness check below
    (`len(last_bucket_60m_bars) < expected_bars`) assumes a bucket that
    isn't fully populated is still forming -- true for crypto's 24/7
    bars, but wrong for equities/ETFs. Their session only runs ~6.5h/day
    (09:30-16:00 ET), so a calendar-aligned 4h bucket that straddles the
    open (e.g. 08:00-12:00 ET) can NEVER accumulate the naive "4 hourly
    bars" no matter how long you wait -- it only ever gets 3 (09:30,
    10:30, 11:30). The old code discarded that bucket as "in progress"
    forever, delaying equity 4h signals by up to a full bucket. `asset`
    lets us switch to a wall-clock completeness check for non-crypto
    assets instead (see below).
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
        is_crypto_like = asset is None or get_asset_class(asset) == AssetClass.CRYPTO
        if is_crypto_like:
            # Original Sprint 43 H12 heuristic — correct for a 24/7
            # market where a complete bucket always gets exactly
            # `expected_bars` source rows.
            if len(last_bucket_60m_bars) < expected_bars:
                # In-progress — drop it
                resampled = resampled.iloc[:-1]
        else:
            # Sprint 46N (audit M4): equity/ETF bucket — bar count alone
            # can't tell "still forming" from "as complete as it will
            # ever be" near market open/close. Use the bucket's own end
            # boundary vs. wall-clock time instead: by the time we reach
            # this code, `_trim_in_progress_bar` (called upstream, in
            # both fetch_and_analyze and fetch_one) has already removed
            # any still-forming raw 60m bar, so every row we see here is
            # a genuinely closed bar. If the bucket's end time has
            # already passed, no more bars are coming for it — keep it,
            # regardless of how few bars it ended up with. Only drop it
            # if the bucket's window is still open (end time in the
            # future) AND it hasn't yet reached the full expected count.
            last_bucket_end = last_bucket_start + pd.Timedelta(seconds=rule_seconds)
            try:
                last_ts = df_60m.index[-1]
                now = (
                    pd.Timestamp.now(tz=last_ts.tz)
                    if getattr(last_ts, "tzinfo", None) is not None
                    else pd.Timestamp.now()
                )
            except Exception:
                now = (
                    pd.Timestamp.now(tz=last_bucket_start.tz)
                    if getattr(last_bucket_start, "tzinfo", None) is not None
                    else pd.Timestamp.now()
                )
            bucket_still_open = now < last_bucket_end
            if bucket_still_open and len(last_bucket_60m_bars) < expected_bars:
                resampled = resampled.iloc[:-1]
    except Exception:
        # If we can't parse the rule (unusual), fall back to
        # the old behavior (drop nothing). Better to show a
        # partial bar than to drop everything.
        pass
    return resampled


_YF_INTERVAL_SECONDS = {
    "1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 900 * 2,
    "60m": 3600, "90m": 5400, "1h": 3600,
}


def _trim_in_progress_bar(df: pd.DataFrame, interval_seconds: int) -> pd.DataFrame:
    """Drop the last row if it's a still-forming (not yet closed) candle.

    Sprint 46E fix: `_resample_ohlcv` above already does this for the
    60m→4h resample path (Sprint 43 H12), but the RAW yfinance
    fetches for 15m/60m intervals (used directly by
    `fetch_and_analyze`/`fetch_one` — e.g. SPY/QQQ's RSI mean-
    reversion on 15m, BTC's MACD on 1h) had no equivalent check.
    yfinance's last row for an intraday interval is frequently the
    bar that's still accumulating ticks — its Close is a snapshot of
    "right now", not a closed bar's final price. `StrategyAgent` then
    reads that bar's RSI/MACD/EMA as if it were the last CLOSED bar,
    which can flip a signal in and out of existence as the current
    minute progresses (not classic look-ahead — no future data leaks
    in — but the opposite failure mode: acting on a bar that hasn't
    finished forming yet).

    Uses wall-clock time (yfinance intraday indices are the bar's
    OPEN time) — if `now < bar_open + interval_seconds`, that bar
    isn't closed yet. For assets with real trading hours (SPY/QQQ),
    this naturally does nothing outside market hours, since `now` is
    already past the last bar's close by then.
    """
    if df.empty or len(df) < 2:
        return df
    try:
        last_ts = df.index[-1]
        now = pd.Timestamp.now(tz=last_ts.tz) if last_ts.tzinfo is not None else pd.Timestamp.now()
        bar_close = last_ts + pd.Timedelta(seconds=interval_seconds)
        if now < bar_close:
            return df.iloc[:-1]
    except Exception:
        # Can't parse timestamps for some reason — fail open (return
        # as-is) rather than risk dropping a valid closed bar.
        pass
    return df


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

    def __init__(self, event_bus=None, audit=None, staleness_multiplier: float = 6.0):
        super().__init__(name="MarketAnalystAgent", audit=audit)
        self.event_bus = event_bus
        # Sprint 52.2: configurable staleness tolerance. The
        # pre-52.2 hard-coded rule was "3x the bar's own
        # interval" (Sprint 6, with a Sprint 46N M4 bypass for
        # non-crypto when the US equity market is closed).
        # That's been tripping on the live VPS since at least
        # 2026-07-13: yfinance's 1h endpoint returns bars
        # 5-6 hours old (well past the 3h threshold for a
        # 1h bar) during US equity market hours, even though
        # crypto trades 24/7 and the underlying asset (BTC-USD)
        # is moving. The fetch itself succeeds — it's a
        # data-freshness gap, not a network failure.
        # 6x covers a full US trading session (1h × 6 = 6h)
        # while still catching a delisted/paused symbol
        # (where the same stale bar would persist for days).
        # Override via the constructor for testing or
        # non-default risk preferences.
        self.staleness_multiplier = float(staleness_multiplier)
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
            # Sprint 46E fix: trim the currently-forming intraday bar
            # (see _trim_in_progress_bar) BEFORE any resampling or
            # indicator calc — the H12 fix only covered the resampled
            # 4h bucket, not the raw 15m/60m fetch itself.
            if yf_interval in _YF_INTERVAL_SECONDS:
                df = _trim_in_progress_bar(df, _YF_INTERVAL_SECONDS[yf_interval])
            if df.empty:
                return None
            # Sprint 43 M6: if caller asked for 4h, resample the
            # 60m bars we just downloaded. Otherwise return as-is.
            # Note: _resample_ohlcv is a module-level function
            # (defined as `def _resample_ohlcv(df_60m, rule)`,
            # not a method), so we call it without self.
            if interval == "4h":
                df = _resample_ohlcv(df, "4h", asset=asset)
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

    # Sprint 45 fix (M5): map each supported timeframe string to its
    # duration in seconds, so `_validate_or_fault` can pass a sane
    # `max_staleness_seconds` to `validate_dataframe`. The staleness
    # check itself was implemented correctly in Sprint 43 (M5), but
    # this — the only production call site — never passed the
    # parameter that activates it, leaving the exact scenario the
    # audit flagged (a delisted/paused symbol returning the same
    # stale last bar for days) uncaught in the live pipeline.
    _TF_SECONDS = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "60m": 3600, "4h": 14400,
        "1d": 86400, "1wk": 604800,
    }

    def _validate_or_fault(self, df, asset_tf: str, tf: str = None, asset: str = None) -> bool:
        """Sprint 6: fail-fast. Devuelve False si los datos son corruptos."""
        # Allow N× the bar's own interval before flagging staleness —
        # generous enough to tolerate a slow/late data provider or a
        # weekend/holiday gap on daily bars, tight enough to still
        # catch a symbol that's stopped updating entirely. The
        # multiplier is configurable per-instance (Sprint 52.2) and
        # defaults to 6× (was 3× pre-52.2). The 3× value tripped
        # constantly on the live VPS because yfinance's 1h endpoint
        # routinely returns bars 5-6 hours old even for actively-
        # traded crypto. 6× covers a full US trading session
        # (1h × 6 = 6h) while still catching a delisted/paused
        # symbol (where the same stale bar would persist for days).
        max_staleness = None
        if tf is not None:
            interval_s = self._TF_SECONDS.get(tf)
            if interval_s:
                max_staleness = interval_s * self.staleness_multiplier
        # Sprint 46N (audit M4): the multiplier rule above assumes the
        # feed keeps updating continuously, which holds for crypto (24/7)
        # but not for equities/ETFs — SPY/QQQ/GLD/USO legitimately stop
        # updating every night and every weekend, which was tripping this
        # check constantly and marking the component DEGRADED for no real
        # reason. While the US equity market is closed, skip the
        # staleness check for non-crypto assets (the other integrity
        # checks — NaN/Inf/negative/monotonic/duplicate-index — still run
        # unconditionally inside validate_dataframe). Coarse weekday+hours
        # check only, no holiday calendar — see _is_us_equity_market_open.
        if asset is not None and max_staleness is not None:
            try:
                if get_asset_class(asset) != AssetClass.CRYPTO and not _is_us_equity_market_open():
                    max_staleness = None
            except Exception:
                pass
        # Sprint 56: yfinance has a known ~24h lag for crypto tickers
        # (BTC-USD, ETH-USD, SOL-USD) when accessed from a VPS IP. The
        # 6x multiplier-based threshold (1.5h for 15m, 6h for 1h, 24h
        # for 4h) trips on every cycle. The 4h bucket just barely passes
        # (22h < 24h); the 15m and 1h buckets fail. The result is 9 of
        # 9 feeds `data integrity fail` per cycle, the agent goes
        # DEGRADED then FAULTED, the workflow continues with empty
        # market_data, and 0 hypotheses are generated.
        # Fix: a 48h FLOOR for crypto, so data up to 2 days old still
        # passes. Above 48h we still treat it as stale (a truly
        # delisted/paused symbol sits at the same timestamp for
        # days). The equity path is unchanged (still uses the
        # market-hours gate above for SPY/QQQ/GLD/USO).
        if asset is not None and max_staleness is not None:
            try:
                if get_asset_class(asset) == AssetClass.CRYPTO:
                    max_staleness = max(max_staleness, _CRYPTO_STALENESS_FLOOR_S)
            except Exception:
                pass
        try:
            validate_dataframe(df, max_staleness_seconds=max_staleness)
            return True
        except DataIntegrityError as e:
            logger.warning(f'  ⚠️  {asset_tf}: data integrity fail — {e}')
            self.degrade(f"data integrity: {e}")
            return False

    # Sprint 54: dedup window for the total-failure SYSTEM_ERROR alert.
    # Pre-54 the bot sent one alert per cycle on a sustained yfinance
    # outage, flooding Telegram. With a 30-minute window the operator
    # gets one ping per outage, not one per 30-minute cycle. The window
    # resets on a successful fetch (any non-total cycle).
    _TOTAL_FAILURE_ALERT_DEDUP_S = 30 * 60
    # Sprint 54: retry cooldown for the total-failure path. If the
    # retry also fails, the agent faults but the NEXT cycle waits at
    # least this long before retrying again — otherwise we'd burn a
    # recursive call every cycle (and the corresponding 30s sleep)
    # on what is clearly a sustained upstream problem, uselessly
    # inflating the cycle duration.
    _TOTAL_FAILURE_RETRY_COOLDOWN_S = 5 * 60

    def fetch_and_analyze(self, inputs: dict, state: dict):
        assets = inputs.get("assets", [])
        timeframes = inputs.get("timeframes", ["1h"])

        tf_map = {
            "15m": ("15m", None),
            "1h":  ("60m", None),
            "4h":  ("60m", "4h"),
        }

        # Sprint 54: auto-recover from FAULTED/DEGRADED at the top of
        # each cycle. Pre-54, once `self.fault()` ran on a total
        # yfinance failure, the agent was stuck in FAULTED forever —
        # `Component.recover()` only handles DEGRADED → RUNNING, and
        # `Component.start()` refuses non-READY states. Every
        # subsequent cycle aborted with
        # "Agent 'MarketAnalystAgent' is in state 'FAULTED'",
        # flooding Telegram with hundreds of identical alerts.
        # The fix: treat FAULTED as a recoverable state at the cycle
        # boundary. The next cycle tries again; if the data comes
        # back, we silently leave FAULTED behind. If it doesn't,
        # the retry/recur logic below handles sustained outages.
        if self.state == ComponentState.FAULTED:
            logger.info(
                "[MarketAnalyst] auto-recovering from FAULTED at cycle start "
                "(Sprint 54 — pre-54 the agent was stuck here forever)"
            )
            self._transition(
                ComponentState.RUNNING,
                "auto-recover: try fresh at cycle start",
            )
        elif self.state == ComponentState.DEGRADED:
            self.recover()
        else:
            self.start()  # READY → RUNNING (no-op if already RUNNING)
        logger.info(f'[MarketAnalystAgent] Fetching {len(assets)} assets × {len(timeframes)} timeframes...')
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
                        logger.warning(f'  ⚠️  {asset}@{tf}: descarga falló tras 3 reintentos')
                        fail_count += 1
                        continue
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df = df.dropna(how="all")
                    if df.empty:
                        logger.warning(f'  ⚠️  {asset}@{tf}: sin datos')
                        fail_count += 1
                        continue

                    # Sprint 46E fix: trim the forming intraday bar for
                    # the RAW fetch (15m, 60m) before any resampling —
                    # the existing H12 fix only covers the resampled
                    # 4h bucket, not the raw yfinance interval itself.
                    if yf_interval in _YF_INTERVAL_SECONDS:
                        df = _trim_in_progress_bar(df, _YF_INTERVAL_SECONDS[yf_interval])
                    if df.empty:
                        logger.warning(f'  ⚠️  {asset}@{tf}: sin velas tras trim de vela en formación')
                        fail_count += 1
                        continue

                    if resample_rule:
                        df = _resample_ohlcv(df, resample_rule, asset=asset)

                    # Sprint 6 fail-fast: validar ANTES de calcular indicadores
                    if not self._validate_or_fault(df, f"{asset}@{tf}", tf=tf, asset=asset):
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
                        logger.warning(f'  ⚠️  {asset}@{tf}: sin velas tras warmup')
                        fail_count += 1
                        continue

                    data[asset][tf] = df
                    logger.info(f"  ✅ {asset}@{tf}: {len(df)} velas | close=${close.iloc[-1]:.2f} | RSI={df['RSI'].iloc[-1]:.1f} | ADX={df['ADX_14'].iloc[-1]:.1f} | StochK={df['Stoch_K'].iloc[-1]:.1f} | ATR={df['ATR_14'].iloc[-1]:.4f}")
                except Exception as e:
                    logger.error(f'  ❌ {asset}@{tf}: {e}')
                    fail_count += 1

        # Sprint 54: also reset the dedup/retry cooldown on a successful
        # fetch. The total-failure path is rare; resetting on success is
        # cheap and ensures the next real outage can fire a fresh alert
        # + retry without being gated by a previous incident.
        total_requested = len(assets) * len(timeframes)
        if fail_count == 0:
            self._last_total_failure_retry_at = 0.0
            self._last_total_failure_alert_at = 0.0

        # Si fallaron demasiados assets, marcar como DEGRADED pero seguir
        if fail_count > 0 and fail_count < total_requested:
            self.degrade(f"{fail_count} feeds failed but workflow continues")
        elif fail_count >= total_requested and total_requested > 0:
            # Sprint 54: TOTAL failure handling with retry + dedup.
            # Pre-54, the agent went straight to FAULTED and stayed
            # there. After 54:
            #   1. If we haven't retried in the last 5 min, sleep 30s
            #      and recurse into this method once. This catches
            #      transient yfinance rate-limits (the dominant cause
            #      in practice) without bothering the operator.
            #   2. If we have retried recently, fault + emit ONE
            #      SYSTEM_ERROR per 30 min, not one per cycle. This
            #      keeps Telegram quiet during sustained outages.
            now = _time.time()
            last_retry = getattr(self, "_last_total_failure_retry_at", 0.0)
            if (now - last_retry) > self._TOTAL_FAILURE_RETRY_COOLDOWN_S:
                # First attempt (or cooldown elapsed) — try once more.
                self._last_total_failure_retry_at = now
                logger.warning(
                    f"[MarketAnalyst] all {fail_count}/{total_requested} "
                    f"feeds failed — retrying once in 30s "
                    f"(Sprint 54 resilience; pre-54 the agent would have "
                    f"faulted and stayed faulted forever)"
                )
                _time.sleep(30)
                # Reset faulted state before recursing (the inner
                # call's auto-recover block will re-emit a clean
                # transition, but we want the state machine to be
                # in a sane starting point for the retry).
                if self.state == ComponentState.FAULTED:
                    self._transition(
                        ComponentState.RUNNING,
                        "retry after total-failure backoff",
                    )
                elif self.state == ComponentState.DEGRADED:
                    self.recover()
                return self.fetch_and_analyze(inputs, state)

            # Retry was already attempted recently and still failed.
            # Fault + emit dedup'd alert.
            self.fault(f"all {fail_count} feeds failed (retry exhausted)")
            # Sprint 43 C6 fix: total data-feed failure is a critical
            # state — the bot has no market context. Without this
            # alert Carlos would only know if he happened to look at
            # the dashboard. SYSTEM_ERROR → NotificationAgent →
            # Telegram, regardless of paper/live.
            #
            # Sprint 54 dedup: don't re-publish the same alert more
            # than once per _TOTAL_FAILURE_ALERT_DEDUP_S window.
            # The next cycle (≤30 min later) will still attempt a
            # fresh fetch (auto-recover at top), so the operator
            # gets pinged at most once per outage window, not once
            # per cycle.
            last_alert = getattr(self, "_last_total_failure_alert_at", 0.0)
            if (now - last_alert) > self._TOTAL_FAILURE_ALERT_DEDUP_S:
                self._last_total_failure_alert_at = now
                if self.event_bus is not None:
                    try:
                        self.event_bus.publish("SYSTEM_ERROR", {
                            "kind": "MARKET_DATA_TOTAL_FAILURE",
                            "fail_count": fail_count,
                            "assets_requested": total_requested,
                            "error": (f"📉 Market data: TODOS los {fail_count} feeds fallaron. "
                                      f"Bot operando a ciegas."),
                        })
                    except Exception as e:
                        logger.error(f'[MarketAnalyst] ⚠️ No se pudo publicar SYSTEM_ERROR: {e}')
            else:
                logger.info(
                    f"[MarketAnalyst] total failure alert suppressed "
                    f"(last alert {now - last_alert:.0f}s ago, "
                    f"dedup window {self._TOTAL_FAILURE_ALERT_DEDUP_S}s)"
                )
        else:
            self.recover()

        if self.event_bus:
            self.event_bus.publish("MARKET_DATA_READY", data)

        return {"market_data": data}
