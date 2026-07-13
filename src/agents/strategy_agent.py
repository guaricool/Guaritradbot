"""
Sprint 0 fix — StrategyAgent.

Fixes vs la versión previa:
1. MACD ahora detecta CRUCES (cuando la línea pasa por encima/debajo de la
   señal), no solo compara el estado actual. Antes, en cualquier tendencia
   alcista el bot estaba long indefinidamente → no había edge.
2. `generate_vectorized_signals` mantiene estado FLAT (0) por default y solo
   cambia cuando hay cruce. Antes siempre devolvía 1/-1 (backtest siempre
   invertido, sin cash).
3. Mejor manejo de NaN.

Sprint 10 — More signals (Carlos's "Equity curve waiting" issue):
Las estrategias originales eran ultra-estrictas (solo cruce exacto en la
última vela de RSI<30 o RSI>70). El bot estaba corriendo bien pero el
mercado actual no tiene esos cruces → 0 señales todo el día. Ahora:

- RSI: también dispara cuando está en zona (<35 o >65) sin requerir cruce
  exacto, con TF 15m + 1h
- MACD: detecta cruce en las últimas 3 barras (no solo la última) +
  histogram turning
- EMA: detecta cruce reciente (últimas 3 barras 4h) + zona tendencial
- NUEVO: Stochastic oversold/overbought + Bollinger bounce + ADX trend
  confirmation (PDF Manual indicators)
- Deduplicación: no genera 2 señales del mismo asset/direction en mismo
  ciclo
"""
import pandas as pd
import numpy as np

# Sprint 46G: Qlib-inspired alpha factors (KBAR/RSV/CNTD/WVMA/etc — see
# src/analysis/alpha_factors.py's module docstring for the Qlib
# provenance). Used below by the new "Alpha Factor" hypothesis block,
# which draws on volume and multi-bar breadth data none of the
# existing RSI/MACD/EMA/Stochastic/Bollinger/Support-Resistance blocks
# use — a genuinely new information source, not a re-parameterization
# of an existing one.
from src.analysis.alpha_factors import latest_alpha_snapshot
# Sprint 46S (audit M1 follow-up): needed to filter out crypto "short"
# hypotheses before they reach the Debate Agent — see the
# `allow_crypto_short` filtering block at the end of
# `evaluate_strategies` below for the full rationale.
from src.data.asset_class import get_asset_class, AssetClass

from src.core.logging_setup import get_logger
logger = get_logger(__name__)


def _was_in_zone_recently(series: pd.Series, threshold: float, lookback: int = 5, direction: str = "below") -> bool:
    """True if the series crossed below/above `threshold` within last `lookback` bars.

    direction='below' → looking for rsi crossing below threshold (oversold)
    direction='above' → looking for rsi crossing above threshold (overbought)
    """
    if len(series) < 2:
        return False
    window = series.tail(lookback + 1)
    if direction == "below":
        # was >= threshold at some earlier bar AND is < threshold at the latest
        return (window.iloc[:-1] >= threshold).any() and window.iloc[-1] < threshold
    else:
        return (window.iloc[:-1] <= threshold).any() and window.iloc[-1] > threshold


def _was_crossed_recently(a: pd.Series, b: pd.Series, lookback: int = 3, direction: str = "up") -> bool:
    """True if series `a` crossed above/below `b` within last `lookback` bars."""
    if len(a) < 2 or len(b) < 2:
        return False
    diff = a - b
    window = diff.tail(lookback + 1)
    if direction == "up":
        return (window.iloc[:-1] <= 0).any() and window.iloc[-1] > 0
    else:
        return (window.iloc[:-1] >= 0).any() and window.iloc[-1] < 0


def _hypothesis_strength(h: dict) -> float:
    """
    B022: derive a 0..1 strength score for a hypothesis.

    Used by PositionMonitor.check_with_signals() to decide whether an
    opposing signal is strong enough to trigger SMART_PROFIT_TAKE on a
    profitable open position.

    Heuristic:
      - Mean-reversion (RSI/Stoch/Bollinger): deeper into the extreme =
        stronger. RSI=20 is stronger than RSI=29.
      - Trend/breakout (MACD/EMA/ADX): ADX value or recency of cross.
      - Default: 0.5 (neutral).
    """
    strategy = h.get("strategy", "").lower()
    rsi = h.get("rsi_at_signal", 0)
    direction = h.get("direction", "long")

    # RSI-based mean reversion
    if "rsi" in strategy:
        if direction == "long" and rsi > 0:
            # RSI < 30 → strong long. RSI 30-40 → medium. > 40 → weak.
            if rsi < 25:
                return 0.9
            elif rsi < 30:
                return 0.8
            elif rsi < 35:
                return 0.65
            else:
                return 0.5
        elif direction == "short" and rsi > 0:
            if rsi > 75:
                return 0.9
            elif rsi > 70:
                return 0.8
            elif rsi > 65:
                return 0.65
            else:
                return 0.5

    # Stochastic-based
    if "stoch" in strategy:
        return 0.75  # treat as strong by default

    # ADX-based trend signals
    if "adx" in strategy or "breakout" in strategy:
        return 0.8

    # MACD / EMA crosses
    if "macd" in strategy or "ema" in strategy or "cross" in strategy:
        return 0.7

    # Sprint 19: ML baseline. Strength = how far prob is from 0.5 (uncertainty).
    if "ml_" in strategy or "baseline" in strategy:
        prob = float(h.get("ml_probability", 0.5))
        # prob in [0, 1]; map distance from 0.5 to strength in [0.5, 0.95]
        distance = abs(prob - 0.5) * 2  # 0..1
        return round(0.5 + 0.45 * distance, 3)

    # Bollinger / S/R
    if "bb_" in strategy or "resistance" in strategy or "support" in strategy:
        return 0.65

    # Default
    return 0.5


def _estimate_expected_move_pct(
    df: pd.DataFrame,
    price: float,
    atr: float,
    kind: str = "reversion",
    target: float = None,
) -> float:
    """Estimate the % magnitude of the move a fresh hypothesis is
    betting on.

    Sprint 46E fix: `RiskManagerAgent.score_new_hypothesis` (used for
    position-replacement scoring, "is this new signal better than my
    worst open position?") reads `expected_move_pct` off each
    hypothesis — but `StrategyAgent` never set it, so every single
    hypothesis fell back to the SAME generic proxy
    (`atr * 4 / entry_price * 100`) regardless of strategy type or
    actual signal strength. That made the replacement scorer nearly
    blind to real differences in edge between hypotheses — a strong
    RSI-30 oversold bounce and a weak RSI-34 zone-touch looked
    identical to it (same ATR-based number either way).

    This computes a per-hypothesis estimate instead:
      - `target` given (Support/Resistance bounce/fade): distance from
        current price to that explicit level — the real reversal
        target for that trade.
      - `kind="reversion"` (RSI/Stochastic/Bollinger mean-reversion):
        distance from current price to BB_Middle, which every df
        already has computed (fetch_and_analyze computes the full
        indicator set for every asset/timeframe regardless of which
        strategy fires) — a real, data-derived reversion target
        rather than a constant.
      - `kind="trend"` (MACD/EMA cross/ADX breakout/ML): recent
        realized momentum — mean absolute bar-to-bar % change over
        the last 10 bars, scaled up 3x as a "this continues" estimate.
      - Fallback (missing columns, bad data): the same ATR-based proxy
        RiskManagerAgent used before this existed.

    Always clamped to [0.1%, 15%] so a data glitch can't hand the
    replacement scorer an absurd number.
    """
    try:
        if target is not None and price > 0 and target > 0:
            pct = abs(float(target) - price) / price * 100.0
            return max(0.1, min(pct, 15.0))
        if kind == "reversion" and "BB_Middle" in df.columns:
            bb_mid = df["BB_Middle"].iloc[-1]
            if bb_mid == bb_mid and price > 0:  # not NaN
                pct = abs(float(bb_mid) - price) / price * 100.0
                if pct > 0:
                    return max(0.1, min(pct, 15.0))
        if kind == "trend" and "Close" in df.columns:
            closes = df["Close"].tail(11)
            if len(closes) >= 2:
                pct = float(closes.pct_change().abs().mean() * 100.0 * 3.0)
                if pct == pct and pct > 0:  # not NaN
                    return max(0.1, min(pct, 15.0))
    except Exception:
        pass
    # Fallback: same ATR-based proxy RiskManagerAgent used before this
    # existed — still better than nothing if the above can't compute.
    if price > 0:
        return max(0.1, min(atr * 4 / price * 100.0, 15.0))
    return 0.1


class StrategyAgent:
    """
    Analiza market_data y genera hipótesis de trade (entry signals).

    Sprint 10: estrategia ampliada con 6 tipos de señales (antes solo 3),
    incluyendo las del PDF Manual del Buen Trader (Stochastic, Bollinger,
    ADX) y zonas más permisivas en RSI/MACD.
    """

    def __init__(self, strategy_params: dict = None, audit=None, ml_predictors: dict = None,
                 ml_long_threshold: float = 0.6, ml_short_threshold: float = 0.4,
                 allow_crypto_short: bool = False, decision_log=None,
                 loss_streak_suppress: int = 3):
        self.params = strategy_params or {
            "rsi_oversold": 30,         # cruce estricto
            "rsi_overbought": 70,
            "rsi_zone_oversold": 35,   # zona permisiva (Sprint 10)
            "rsi_zone_overbought": 65,
            "stoch_oversold": 20,
            "stoch_overbought": 80,
            "bb_pct_b": 0.05,          # dentro del 5% de la banda
            "adx_trend_min": 20,        # ADX > 20 = tendencia
        }
        # B022 fix: audit ledger para emitir HYPOTHESIS_GENERATED events.
        # Esto permite que PositionMonitor.check_with_signals() encuentre
        # las señales recientes y dispare SMART_PROFIT_TAKE cuando hay
        # un reversal fuerte contra una posición abierta en profit.
        self.audit = audit
        # Sprint 19: ML predictors per asset.
        # Each entry is a `Predictor` instance (loaded from a trained model).
        # If a model is available for the asset, the StrategyAgent will
        # generate ML_BASELINE_LONG / ML_BASELINE_SHORT hypotheses based on
        # the predicted probability of a positive forward return.
        self.ml_predictors = ml_predictors or {}
        self.ml_long_threshold = ml_long_threshold
        self.ml_short_threshold = ml_short_threshold
        # Sprint 46S (audit M1 follow-up): mirrors RiskManagerAgent's
        # `allow_crypto_short` (default False — binance.us spot has no
        # margin/borrow, so a crypto "short" hypothesis can never
        # actually execute). Before this, StrategyAgent generated crypto
        # short hypotheses same as any other, the HypothesisScorer spent a
        # full cycle scoring/approving one, and ONLY THEN did
        # RiskManagerAgent's `validate_and_size` reject it as
        # `crypto_short_not_supported` — by which point the cycle's
        # only other candidate (the long side) may have already lost
        # the debate on its own merits, so the bot took zero trades
        # that hour even though a non-crypto-short cycle might have
        # let the debate focus solely on the executable long. See
        # `evaluate_strategies`'s filtering block below for where this
        # is actually applied.
        self.allow_crypto_short = allow_crypto_short
        # Sprint 52.4: optional decision_log reference so the
        # StrategyAgent can suppress hypotheses for (asset,
        # direction) combinations with a losing streak. The
        # bot already records every outcome in the decision
        # log (Sprint 48), so the data is there — we just
        # need to read it before generating. Set
        # `loss_streak_suppress=0` to disable the suppression
        # (the scorer still consults `recent_lessons_for` on
        # its own — this is an additional, source-side filter
        # that prevents the most clearly-broken setups from
        # even reaching the score debate).
        self.decision_log = decision_log
        self.loss_streak_suppress = int(loss_streak_suppress)

    def _add_hyp(self, hypotheses, asset, tf, direction, strategy, price, **extras):
        hypotheses.append({
            "asset": asset,
            "tf": tf,
            "direction": direction,
            "strategy": strategy,
            "price": price,
            **extras,
        })

    def evaluate_strategies(self, inputs: dict, state: dict):
        market_data = state.get("analyze_market", {}).get("market_data", {})
        logger.info(f'[StrategyAgent] Evaluando {len(market_data)} assets...')

        hypotheses = []

        # ============================================================
        # SPY / QQQ → mean reversion con RSI (15m + 1h)
        # ============================================================
        for asset in ("SPY", "QQQ"):
            for tf in ("15m", "1h"):
                df = market_data.get(asset, {}).get(tf)
                if df is None or len(df) < 20:
                    continue
                if "RSI" not in df.columns.values:
                    continue

                rsi = df["RSI"]
                last = df.iloc[-1]
                price = float(last["Close"])
                rsi_now = float(last["RSI"])
                rsi_prev = float(df["RSI"].iloc[-2])
                atr = float(last.get("ATR_14", 0) or 0)

                oversold = self.params["rsi_oversold"]
                overbought = self.params["rsi_overbought"]
                zone_oversold = self.params["rsi_zone_oversold"]
                zone_overbought = self.params["rsi_zone_overbought"]

                # Sprint 46E: real per-hypothesis expected move (distance
                # to BB_Middle) instead of RiskManagerAgent's old generic
                # atr*4 proxy — same df, so BB_Middle is already computed.
                _exp_move = _estimate_expected_move_pct(df, price, atr, kind="reversion")

                # A) Cruce estricto (última vela)
                if rsi_prev >= oversold and rsi_now < oversold:
                    self._add_hyp(
                        hypotheses, asset, tf, "long",
                        f"MeanReversion_LONG_RSI<{oversold}",
                        price, rsi_at_signal=rsi_now, atr_at_signal=atr,
                        expected_move_pct=_exp_move,
                    )
                elif rsi_prev <= overbought and rsi_now > overbought:
                    self._add_hyp(
                        hypotheses, asset, tf, "short",
                        f"MeanReversion_SHORT_RSI>{overbought}",
                        price, rsi_at_signal=rsi_now, atr_at_signal=atr,
                        expected_move_pct=_exp_move,
                    )
                # B) Zona RSI (permisiva) — RSI < 35 → long bias
                elif rsi_now < zone_oversold and tf == "1h":
                    # Check si en las últimas 5 barras cruzó la zona
                    if _was_in_zone_recently(rsi, zone_oversold, lookback=5, direction="below"):
                        self._add_hyp(
                            hypotheses, asset, tf, "long",
                            f"MeanReversion_LONG_RSI<{zone_oversold}_zone",
                            price, rsi_at_signal=rsi_now, atr_at_signal=atr,
                            expected_move_pct=_exp_move,
                        )
                elif rsi_now > zone_overbought and tf == "1h":
                    if _was_in_zone_recently(rsi, zone_overbought, lookback=5, direction="above"):
                        self._add_hyp(
                            hypotheses, asset, tf, "short",
                            f"MeanReversion_SHORT_RSI>{zone_overbought}_zone",
                            price, rsi_at_signal=rsi_now, atr_at_signal=atr,
                            expected_move_pct=_exp_move,
                        )

        # ============================================================
        # BTC → MACD cross (1h) con tolerancia + histogram turning
        # ============================================================
        for asset in ("BTC-USD", "BTCUSDT"):
            df = market_data.get(asset, {}).get("1h")
            if df is None or len(df) < 30:
                continue
            if "MACD" not in df.columns.values or "MACD_Signal" not in df.columns.values:
                continue

            macd = df["MACD"]
            sig = df["MACD_Signal"]
            hist = macd - sig
            last = df.iloc[-1]
            price = float(last["Close"])
            atr = float(last.get("ATR_14", 0) or 0)

            # A) Cruce estricto en última vela
            macd_now = float(macd.iloc[-1])
            sig_now = float(sig.iloc[-1])
            macd_prev = float(macd.iloc[-2])
            sig_prev = float(sig.iloc[-2])
            # Sprint 46E: trend-type expected move (recent momentum), not
            # the flat ATR proxy RiskManagerAgent used before this existed.
            _exp_move = _estimate_expected_move_pct(df, price, atr, kind="trend")

            if macd_prev <= sig_prev and macd_now > sig_now:
                self._add_hyp(
                    hypotheses, asset, "1h", "long",
                    "MACD_BullCross", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
                    expected_move_pct=_exp_move,
                )
            elif macd_prev >= sig_prev and macd_now < sig_now:
                self._add_hyp(
                    hypotheses, asset, "1h", "short",
                    "MACD_BearCross", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
                    expected_move_pct=_exp_move,
                )
            # B) Cruce en últimas 3 barras (no solo la última)
            elif _was_crossed_recently(macd, sig, lookback=3, direction="up"):
                self._add_hyp(
                    hypotheses, asset, "1h", "long",
                    "MACD_BullCross_recent", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
                    expected_move_pct=_exp_move,
                )
            elif _was_crossed_recently(macd, sig, lookback=3, direction="down"):
                self._add_hyp(
                    hypotheses, asset, "1h", "short",
                    "MACD_BearCross_recent", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
                    expected_move_pct=_exp_move,
                )
            # C) MACD histogram turning (momentum shift)
            elif len(hist) >= 3:
                h_prev = float(hist.iloc[-2])
                h_prev2 = float(hist.iloc[-3])
                h_now = float(hist.iloc[-1])
                # turning bullish: was negative and getting more negative, now rising
                if h_prev2 < h_prev < 0 and h_now > h_prev:
                    self._add_hyp(
                        hypotheses, asset, "1h", "long",
                        "MACD_HistTurn_Bull", price,
                        macd_at_signal=macd_now, atr_at_signal=atr,
                        expected_move_pct=_exp_move,
                    )
                # turning bearish: was positive and getting more positive, now falling
                elif h_prev2 > h_prev > 0 and h_now < h_prev:
                    self._add_hyp(
                        hypotheses, asset, "1h", "short",
                        "MACD_HistTurn_Bear", price,
                        macd_at_signal=macd_now, atr_at_signal=atr,
                        expected_move_pct=_exp_move,
                    )

        # ============================================================
        # GLD / USO → EMA trend (4h preferred, 1h fallback)
        # ============================================================
        for asset in ("GLD", "USO"):
            df = market_data.get(asset, {}).get("4h")
            tf_used = "4h"
            if df is None or len(df) < 60:
                df = market_data.get(asset, {}).get("1h")
                tf_used = "1h"
            if df is None or len(df) < 60:
                continue
            if "EMA_20" not in df.columns.values or "EMA_50" not in df.columns.values:
                continue

            ema20 = df["EMA_20"]
            ema50 = df["EMA_50"]
            last = df.iloc[-1]
            price = float(last["Close"])
            atr = float(last.get("ATR_14", 0) or 0)
            adx_now = float(last.get("ADX_14", 0) or 0)

            ema20_now = float(ema20.iloc[-1])
            ema50_now = float(ema50.iloc[-1])
            ema20_prev = float(ema20.iloc[-2])
            ema50_prev = float(ema50.iloc[-2])

            # Sprint 46E: trend-type expected move for EMA cross hypotheses.
            _exp_move = _estimate_expected_move_pct(df, price, atr, kind="trend")

            # A) Cruce estricto
            if ema20_prev <= ema50_prev and ema20_now > ema50_now:
                self._add_hyp(
                    hypotheses, asset, tf_used, "long",
                    "EMA_GoldenCross", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr, expected_move_pct=_exp_move,
                )
            elif ema20_prev >= ema50_prev and ema20_now < ema50_now:
                self._add_hyp(
                    hypotheses, asset, tf_used, "short",
                    "EMA_DeathCross", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr, expected_move_pct=_exp_move,
                )
            # B) Cruce en últimas 3 barras
            elif _was_crossed_recently(ema20, ema50, lookback=3, direction="up"):
                self._add_hyp(
                    hypotheses, asset, tf_used, "long",
                    "EMA_GoldenCross_recent", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr, expected_move_pct=_exp_move,
                )
            elif _was_crossed_recently(ema20, ema50, lookback=3, direction="down"):
                self._add_hyp(
                    hypotheses, asset, tf_used, "short",
                    "EMA_DeathCross_recent", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr, expected_move_pct=_exp_move,
                )

        # ============================================================
        # NUEVO — Stochastic oversold/overbought (1h, todos los assets)
        # ============================================================
        for asset in ("SPY", "QQQ", "BTC-USD", "GLD", "USO"):
            df = market_data.get(asset, {}).get("1h")
            if df is None or len(df) < 20:
                continue
            if "Stoch_K" not in df.columns.values or "Stoch_D" not in df.columns.values:
                continue

            stoch_k = df["Stoch_K"]
            stoch_d = df["Stoch_D"]
            last = df.iloc[-1]
            price = float(last["Close"])
            atr = float(last.get("ATR_14", 0) or 0)
            k_now = float(stoch_k.iloc[-1])
            d_now = float(stoch_d.iloc[-1])
            k_prev = float(stoch_k.iloc[-2])

            # Cruce estocástico en zona oversold/overbought
            if k_prev < self.params["stoch_oversold"] and k_now > d_now and k_now < 30:
                # K cruzó POR ARRIBA de D en zona oversold = señal long
                self._add_hyp(
                    hypotheses, asset, "1h", "long",
                    "Stoch_Oversold_Cross", price,
                    stoch_k=k_now, atr_at_signal=atr,
                    expected_move_pct=_estimate_expected_move_pct(df, price, atr, kind="reversion"),
                )
            elif k_prev > self.params["stoch_overbought"] and k_now < d_now and k_now > 70:
                self._add_hyp(
                    hypotheses, asset, "1h", "short",
                    "Stoch_Overbought_Cross", price,
                    stoch_k=k_now, atr_at_signal=atr,
                    expected_move_pct=_estimate_expected_move_pct(df, price, atr, kind="reversion"),
                )

        # ============================================================
        # NUEVO — Bollinger bounce (4h, todos los assets)
        # Si precio toca banda inferior + RSI<40 → long bounce
        # Si precio toca banda superior + RSI>60 → short fade
        # ============================================================
        for asset in ("SPY", "QQQ", "BTC-USD", "GLD", "USO"):
            df = market_data.get(asset, {}).get("4h")
            if df is None or len(df) < 25:
                continue
            for col in ("Close", "BB_Upper", "BB_Lower", "RSI", "ATR_14"):
                if col not in df.columns.values:
                    break
            else:
                last = df.iloc[-1]
                price = float(last["Close"])
                bb_upper = float(last["BB_Upper"])
                bb_lower = float(last["BB_Lower"])
                bb_range = bb_upper - bb_lower
                if bb_range == 0:
                    continue
                rsi = float(last["RSI"])
                atr = float(last["ATR_14"] or 0)

                # %B = (price - lower) / (upper - lower). <0 = debajo de banda inf.
                pct_b = (price - bb_lower) / bb_range

                # Cerca de banda inferior + RSI bajo → long bounce
                if pct_b < self.params["bb_pct_b"] and rsi < 45:
                    self._add_hyp(
                        hypotheses, asset, "4h", "long",
                        "BB_LowerBounce", price,
                        pct_b=pct_b, rsi_at_signal=rsi, atr_at_signal=atr,
                        expected_move_pct=_estimate_expected_move_pct(df, price, atr, kind="reversion"),
                    )
                # Cerca de banda superior + RSI alto → short fade
                elif pct_b > 1 - self.params["bb_pct_b"] and rsi > 55:
                    self._add_hyp(
                        hypotheses, asset, "4h", "short",
                        "BB_UpperFade", price,
                        pct_b=pct_b, rsi_at_signal=rsi, atr_at_signal=atr,
                        expected_move_pct=_estimate_expected_move_pct(df, price, atr, kind="reversion"),
                    )

        # ============================================================
        # NUEVO — Soporte / Resistencia bounce (1d para confirmar)
        # Si precio cerca de soporte 50-periodos → long
        # Si precio cerca de resistencia 50-periodos → short
        # ============================================================
        for asset in ("SPY", "QQQ", "BTC-USD", "GLD", "USO"):
            df = market_data.get(asset, {}).get("4h")
            if df is None or len(df) < 55:
                continue
            for col in ("Close", "Support_50", "Resistance_50", "ATR_14"):
                if col not in df.columns.values:
                    break
            else:
                last = df.iloc[-1]
                price = float(last["Close"])
                support = float(last["Support_50"])
                resistance = float(last["Resistance_50"])
                atr = float(last["ATR_14"] or 0)

                # Cerca de soporte (dentro de 1 ATR)
                if support > 0 and abs(price - support) < atr * 1.5 and price > support:
                    # Sprint 46E: explicit target = resistance (the real
                    # upside reversal target for a support bounce), not a
                    # generic ATR proxy.
                    self._add_hyp(
                        hypotheses, asset, "4h", "long",
                        "Support_Bounce", price,
                        support_level=support, atr_at_signal=atr,
                        expected_move_pct=_estimate_expected_move_pct(
                            df, price, atr, target=resistance if resistance > price else None,
                        ),
                    )
                # Cerca de resistencia (dentro de 1 ATR)
                elif resistance > 0 and abs(price - resistance) < atr * 1.5 and price < resistance:
                    # Sprint 46E: explicit target = support (the real
                    # downside reversal target for a resistance fade).
                    self._add_hyp(
                        hypotheses, asset, "4h", "short",
                        "Resistance_Fade", price,
                        resistance_level=resistance, atr_at_signal=atr,
                        expected_move_pct=_estimate_expected_move_pct(
                            df, price, atr, target=support if 0 < support < price else None,
                        ),
                    )

        # ============================================================
        # Sprint 19 — ML Baseline signal
        # If a trained ML model exists for this asset, predict the
        # probability of a positive forward return and emit a hypothesis
        # when the probability crosses our thresholds (default 0.6 long,
        # 0.4 short).
        # ============================================================
        if self.ml_predictors:
            from src.ml.pipeline import FeatureExtractor
            _extractor = FeatureExtractor()
            for asset in ("BTC-USD", "SPY", "QQQ", "GLD", "USO"):
                predictor = self.ml_predictors.get(asset)
                if predictor is None:
                    continue
                # Prefer 4h data (matches our other signals)
                df_ml = market_data.get(asset, {}).get("4h")
                if df_ml is None or len(df_ml) < 60:
                    continue
                try:
                    X, feat_names = _extractor.transform(df_ml)
                except Exception as e:
                    if self.audit is not None:
                        self.audit.append("ML_PREDICT_FAILED", {
                            "asset": asset, "reason": str(e)[:200],
                        })
                    continue
                if len(X) == 0:
                    continue
                # Predict probability for the latest bar
                prob_long = predictor.predict_one(X.iloc[-1])
                _ml_price = float(df_ml["Close"].iloc[-1])
                if prob_long >= self.ml_long_threshold:
                    atr_ml = float(df_ml["ATR_14"].iloc[-1]) if "ATR_14" in df_ml.columns else 0
                    # Sprint 46E: scale the trend-momentum estimate by how
                    # confident the model is (distance of prob from 0.5) —
                    # a 0.95-probability call implies more conviction in
                    # the move than a barely-over-threshold 0.61.
                    _confidence = abs(float(prob_long) - 0.5) * 2
                    self._add_hyp(
                        hypotheses, asset, "4h", "long",
                        "ML_Baseline", _ml_price,
                        ml_probability=float(prob_long),
                        atr_at_signal=atr_ml,
                        n_features=len(feat_names),
                        expected_move_pct=_estimate_expected_move_pct(
                            df_ml, _ml_price, atr_ml, kind="trend",
                        ) * (0.5 + 0.5 * _confidence),
                    )
                elif prob_long <= self.ml_short_threshold:
                    atr_ml = float(df_ml["ATR_14"].iloc[-1]) if "ATR_14" in df_ml.columns else 0
                    _confidence = abs(float(prob_long) - 0.5) * 2
                    self._add_hyp(
                        hypotheses, asset, "4h", "short",
                        "ML_Baseline", _ml_price,
                        ml_probability=float(prob_long),
                        atr_at_signal=atr_ml,
                        n_features=len(feat_names),
                        expected_move_pct=_estimate_expected_move_pct(
                            df_ml, _ml_price, atr_ml, kind="trend",
                        ) * (0.5 + 0.5 * _confidence),
                    )

        # ============================================================
        # Sprint 46G — Alpha Factor block (Qlib-inspired)
        # Draws on volume + candle-shape + multi-bar breadth data that
        # none of the blocks above use:
        #   - CNTD20 (breadth: %up-bars - %down-bars over 20 bars) as a
        #     momentum-confirmation filter
        #   - RSV10/RSV20 (price position within its recent High/Low
        #     range, 0=at the low, 1=at the high) as the trigger
        #   - WVMA20 (volume-weighted price-change volatility) to flag
        #     unusually erratic, volume-heavy moves — a capitulation/
        #     exhaustion signature worth fading
        # See src/analysis/alpha_factors.py for the exact formulas
        # (ported from qlib/contrib/data/loader.py).
        # ============================================================
        for asset in ("SPY", "QQQ", "BTC-USD", "GLD", "USO"):
            df = market_data.get(asset, {}).get("1h")
            if df is None or len(df) < 25:
                continue
            if not {"Open", "High", "Low", "Close", "Volume"}.issubset(set(df.columns)):
                continue
            try:
                snap = latest_alpha_snapshot(df, windows=(10, 20))
            except Exception as e:
                if self.audit is not None:
                    self.audit.append("ALPHA_FACTOR_SNAPSHOT_FAILED", {
                        "asset": asset, "reason": str(e)[:200],
                    })
                continue
            if not snap:
                continue

            last = df.iloc[-1]
            price = float(last["Close"])
            atr = float(last.get("ATR_14", 0) or 0)
            cntd20 = snap.get("CNTD20")
            rsv10 = snap.get("RSV10")
            rsv20 = snap.get("RSV20")
            wvma20 = snap.get("WVMA20")

            # A) Momentum breakout: strong up-breadth + price near the
            # top of its recent range → trend continuation long.
            if cntd20 is not None and rsv10 is not None and cntd20 > 0.5 and rsv10 > 0.85:
                self._add_hyp(
                    hypotheses, asset, "1h", "long",
                    "AlphaFactor_MomentumBreakout", price,
                    cntd20=cntd20, rsv10=rsv10, atr_at_signal=atr,
                    expected_move_pct=_estimate_expected_move_pct(df, price, atr, kind="trend"),
                )
            # B) Momentum breakdown: strong down-breadth + price near
            # the bottom of its recent range → trend continuation short.
            elif cntd20 is not None and rsv10 is not None and cntd20 < -0.5 and rsv10 < 0.15:
                self._add_hyp(
                    hypotheses, asset, "1h", "short",
                    "AlphaFactor_MomentumBreakdown", price,
                    cntd20=cntd20, rsv10=rsv10, atr_at_signal=atr,
                    expected_move_pct=_estimate_expected_move_pct(df, price, atr, kind="trend"),
                )
            # C) Capitulation bounce: price pinned at the bottom of its
            # range AND volume-weighted volatility is unusually high
            # (a sharp, volume-heavy selloff) → contrarian long.
            elif rsv20 is not None and wvma20 is not None and rsv20 < 0.10 and wvma20 > 2.0:
                self._add_hyp(
                    hypotheses, asset, "1h", "long",
                    "AlphaFactor_CapitulationBounce", price,
                    rsv20=rsv20, wvma20=wvma20, atr_at_signal=atr,
                    expected_move_pct=_estimate_expected_move_pct(df, price, atr, kind="reversion"),
                )

        # ============================================================
        # Dedupe: max 1 señal por (asset, direction) — el bot decide
        # el resto en RiskManager
        # ============================================================
        seen = set()
        deduped = []
        for h in hypotheses:
            key = (h["asset"], h["direction"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(h)
        hypotheses = deduped

        # ============================================================
        # Sprint 46S (audit M1 follow-up) — drop crypto "short"
        # hypotheses BEFORE the Debate Agent ever sees them.
        #
        # binance.us spot has no margin/borrow, so a crypto short was
        # never a real exchange position — RiskManagerAgent already
        # rejects these as `crypto_short_not_supported` (Sprint 46M),
        # but that rejection happens AFTER `debate_hypotheses`, which
        # means every cycle with a crypto short candidate spent real
        # debate work approving a trade that could never execute. Live
        # audit evidence (2026-07-12, ~10:21 and ~11:21 cycles): BTC-USD
        # generated a Resistance_Fade SHORT (4h) and a MACD_BullCross
        # LONG (1h) every hour; the debate approved the short and
        # rejected the long on its own merits — net zero trades that
        # hour, even though nothing was actually wrong with the market
        # data or the bot's health. Filtering here instead of only in
        # RiskManagerAgent means the debate's attention (and the audit
        # trail) focuses on hypotheses that can actually be executed.
        # `allow_crypto_short=True` (opt-in, e.g. once real margin/
        # futures trading is wired in) disables this filter entirely,
        # mirroring RiskManagerAgent's own flag of the same name.
        # ============================================================
        if not self.allow_crypto_short:
            kept = []
            suppressed = []
            for h in hypotheses:
                if h["direction"] == "short" and get_asset_class(h["asset"]) == AssetClass.CRYPTO:
                    suppressed.append(h)
                else:
                    kept.append(h)
            if suppressed:
                for h in suppressed:
                    logger.info(f"  🚫 {h['asset']:8} short — crypto_short_not_supported (binance.us spot, no margin) — suprimida antes del debate (via {h['strategy']})")
                    if self.audit is not None:
                        self.audit.append("HYPOTHESIS_SUPPRESSED", {
                            "asset": h["asset"],
                            "tf": h.get("tf", ""),
                            "direction": h["direction"],
                            "strategy": h["strategy"],
                            "price": h["price"],
                            "reason": "crypto_short_not_supported",
                            "detail": (
                                "binance.us spot has no margin/borrow; a short "
                                "here is not a real exchange position. Suppressed "
                                "before the debate stage instead of wasting a "
                                "debate cycle on it. Set trading.allow_crypto_short"
                                "=true only if real margin/futures trading is "
                                "wired in."
                            ),
                        })
            hypotheses = kept

        # Sprint 52.4: loss-streak suppression. The bot already
        # records every closed position in the decision log
        # (Sprint 48). Before sending a hypothesis to the score
        # debate, ask the log "have the last N outcomes for this
        # (asset, direction) all been losses?" If yes, suppress.
        # Defense-in-depth: the scorer still consults
        # `recent_lessons_for` on its own, but suppressing at the
        # source saves a full debate cycle and prevents the most
        # clearly-broken setups from even reaching the risk gate.
        if self.decision_log is not None and self.loss_streak_suppress > 0 and hypotheses:
            suppressed_streak = []
            kept_after_streak = []
            for h in hypotheses:
                try:
                    recent = self.decision_log.recent_outcomes_for(
                        asset=h["asset"],
                        direction=h["direction"],
                        n=self.loss_streak_suppress,
                    )
                except Exception as _e:
                    # Decision log failure must NEVER block a trade
                    # — same fail-open pattern as researchers.py.
                    logger.info(f"[DecisionLog] could not query outcomes: {_e}")
                    recent = []
                if len(recent) >= self.loss_streak_suppress and all(
                    (r.get("pnl_usd", 0.0) or 0.0) < 0 for r in recent
                ):
                    suppressed_streak.append((h, recent))
                else:
                    kept_after_streak.append(h)
            if suppressed_streak:
                for h, recent in suppressed_streak:
                    pnl_summary = ", ".join(
                        f"${r.get('pnl_usd', 0):.2f}"
                        for r in recent[: self.loss_streak_suppress]
                    )
                    logger.info(
                        f"  📉 {h['asset']:8} {h['direction']:5} "
                        f"— {len(recent)} pérdidas consecutivas "
                        f"[{pnl_summary}] — suprimida por loss-streak (Sprint 52.4)"
                    )
                    if self.audit is not None:
                        self.audit.append("HYPOTHESIS_SUPPRESSED", {
                            "asset": h["asset"],
                            "tf": h.get("tf", ""),
                            "direction": h["direction"],
                            "strategy": h["strategy"],
                            "price": h["price"],
                            "reason": "loss_streak",
                            "detail": (
                                f"last {len(recent)} outcomes for "
                                f"{h['asset']} {h['direction']} all had "
                                f"pnl_usd<0 [{pnl_summary}]. Suppressed at "
                                f"source to avoid wasting a debate cycle on "
                                f"a strategy the recent track record says "
                                f"is broken. Set loss_streak_suppress=0 to "
                                f"disable."
                            ),
                        })
            hypotheses = kept_after_streak

        if hypotheses:
            logger.info(f'[StrategyAgent] → {len(hypotheses)} hipótesis nuevas:')
            for h in hypotheses:
                logger.info(f"   • {h['direction'].upper():5} {h['asset']:8} @ ${h['price']:.2f} via {h['strategy']}")
            # B022 fix: emit HYPOTHESIS_GENERATED events so PositionMonitor
            # can use them for SMART_PROFIT_TAKE reversal detection. Each
            # hypothesis carries direction + asset + price; we add a
            # derived `strength` field (0..1) based on how extreme the
            # indicator reading was (deeper into oversold = stronger).
            if self.audit is not None:
                import time as _t
                for h in hypotheses:
                    self.audit.append("HYPOTHESIS_GENERATED", {
                        "asset": h["asset"],
                        "tf": h.get("tf", ""),
                        "direction": h["direction"],
                        "strategy": h["strategy"],
                        "price": h["price"],
                        "atr_at_signal": h.get("atr_at_signal", 0),
                        "rsi_at_signal": h.get("rsi_at_signal", 0),
                        "strength": _hypothesis_strength(h),
                    })
        else:
            logger.info('[StrategyAgent] → 0 hipótesis (sin condiciones activas)')

        return {"hypotheses": hypotheses}

    @staticmethod
    def generate_vectorized_signals(df: pd.DataFrame, strategy_type: str = "RSI", **params) -> pd.Series:
        """
        Generador vectorizado para el Hyperopt/Backtester.

        ESTADO BASE = FLAT (0). Solo emite 1/-1 en cruces, y forward-fillea
        la posición. Antes siempre devolvía 1/-1 (backtest always-inverted).
        """
        if df.empty:
            return pd.Series(dtype=float)

        signals = pd.Series(0.0, index=df.index)

        if strategy_type == "RSI":
            oversold = params.get("rsi_oversold", 30)
            overbought = params.get("rsi_overbought", 70)
            rsi = df["RSI"]
            # Cruce POR DEBAJO de oversold = entry long
            cross_below = (rsi.shift(1) >= oversold) & (rsi < oversold)
            # Cruce POR ENCIMA de overbought = entry short (o exit long → flat)
            cross_above = (rsi.shift(1) <= overbought) & (rsi > overbought)
            signals[cross_below] = 1.0
            signals[cross_above] = -1.0

        elif strategy_type == "MACD":
            macd = df["MACD"]
            sig = df["MACD_Signal"]
            cross_up = (macd.shift(1) <= sig.shift(1)) & (macd > sig)
            cross_down = (macd.shift(1) >= sig.shift(1)) & (macd < sig)
            signals[cross_up] = 1.0
            signals[cross_down] = -1.0

        elif strategy_type == "EMA_CROSS":
            ema20 = df["EMA_20"]
            ema50 = df["EMA_50"]
            golden = (ema20.shift(1) <= ema50.shift(1)) & (ema20 > ema50)
            death = (ema20.shift(1) >= ema50.shift(1)) & (ema20 < ema50)
            signals[golden] = 1.0
            signals[death] = -1.0

        else:
            raise ValueError(f"Estrategia desconocida: {strategy_type}")

        # Forward-fill para mantener la posición hasta el próximo cruce
        return signals.replace(0, np.nan).ffill().fillna(0)
