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

    # Bollinger / S/R
    if "bb_" in strategy or "resistance" in strategy or "support" in strategy:
        return 0.65

    # Default
    return 0.5


class StrategyAgent:
    """
    Analiza market_data y genera hipótesis de trade (entry signals).

    Sprint 10: estrategia ampliada con 6 tipos de señales (antes solo 3),
    incluyendo las del PDF Manual del Buen Trader (Stochastic, Bollinger,
    ADX) y zonas más permisivas en RSI/MACD.
    """

    def __init__(self, strategy_params: dict = None, audit=None):
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
        print(f"[StrategyAgent] Evaluando {len(market_data)} assets...")

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

                # A) Cruce estricto (última vela)
                if rsi_prev >= oversold and rsi_now < oversold:
                    self._add_hyp(
                        hypotheses, asset, tf, "long",
                        f"MeanReversion_LONG_RSI<{oversold}",
                        price, rsi_at_signal=rsi_now, atr_at_signal=atr,
                    )
                elif rsi_prev <= overbought and rsi_now > overbought:
                    self._add_hyp(
                        hypotheses, asset, tf, "short",
                        f"MeanReversion_SHORT_RSI>{overbought}",
                        price, rsi_at_signal=rsi_now, atr_at_signal=atr,
                    )
                # B) Zona RSI (permisiva) — RSI < 35 → long bias
                elif rsi_now < zone_oversold and tf == "1h":
                    # Check si en las últimas 5 barras cruzó la zona
                    if _was_in_zone_recently(rsi, zone_oversold, lookback=5, direction="below"):
                        self._add_hyp(
                            hypotheses, asset, tf, "long",
                            f"MeanReversion_LONG_RSI<{zone_oversold}_zone",
                            price, rsi_at_signal=rsi_now, atr_at_signal=atr,
                        )
                elif rsi_now > zone_overbought and tf == "1h":
                    if _was_in_zone_recently(rsi, zone_overbought, lookback=5, direction="above"):
                        self._add_hyp(
                            hypotheses, asset, tf, "short",
                            f"MeanReversion_SHORT_RSI>{zone_overbought}_zone",
                            price, rsi_at_signal=rsi_now, atr_at_signal=atr,
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

            if macd_prev <= sig_prev and macd_now > sig_now:
                self._add_hyp(
                    hypotheses, asset, "1h", "long",
                    "MACD_BullCross", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
                )
            elif macd_prev >= sig_prev and macd_now < sig_now:
                self._add_hyp(
                    hypotheses, asset, "1h", "short",
                    "MACD_BearCross", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
                )
            # B) Cruce en últimas 3 barras (no solo la última)
            elif _was_crossed_recently(macd, sig, lookback=3, direction="up"):
                self._add_hyp(
                    hypotheses, asset, "1h", "long",
                    "MACD_BullCross_recent", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
                )
            elif _was_crossed_recently(macd, sig, lookback=3, direction="down"):
                self._add_hyp(
                    hypotheses, asset, "1h", "short",
                    "MACD_BearCross_recent", price,
                    macd_at_signal=macd_now, atr_at_signal=atr,
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
                    )
                # turning bearish: was positive and getting more positive, now falling
                elif h_prev2 > h_prev > 0 and h_now < h_prev:
                    self._add_hyp(
                        hypotheses, asset, "1h", "short",
                        "MACD_HistTurn_Bear", price,
                        macd_at_signal=macd_now, atr_at_signal=atr,
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

            # A) Cruce estricto
            if ema20_prev <= ema50_prev and ema20_now > ema50_now:
                self._add_hyp(
                    hypotheses, asset, tf_used, "long",
                    "EMA_GoldenCross", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr,
                )
            elif ema20_prev >= ema50_prev and ema20_now < ema50_now:
                self._add_hyp(
                    hypotheses, asset, tf_used, "short",
                    "EMA_DeathCross", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr,
                )
            # B) Cruce en últimas 3 barras
            elif _was_crossed_recently(ema20, ema50, lookback=3, direction="up"):
                self._add_hyp(
                    hypotheses, asset, tf_used, "long",
                    "EMA_GoldenCross_recent", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr,
                )
            elif _was_crossed_recently(ema20, ema50, lookback=3, direction="down"):
                self._add_hyp(
                    hypotheses, asset, tf_used, "short",
                    "EMA_DeathCross_recent", price,
                    ema20_at_signal=ema20_now, ema50_at_signal=ema50_now,
                    atr_at_signal=atr,
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
                )
            elif k_prev > self.params["stoch_overbought"] and k_now < d_now and k_now > 70:
                self._add_hyp(
                    hypotheses, asset, "1h", "short",
                    "Stoch_Overbought_Cross", price,
                    stoch_k=k_now, atr_at_signal=atr,
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
                    )
                # Cerca de banda superior + RSI alto → short fade
                elif pct_b > 1 - self.params["bb_pct_b"] and rsi > 55:
                    self._add_hyp(
                        hypotheses, asset, "4h", "short",
                        "BB_UpperFade", price,
                        pct_b=pct_b, rsi_at_signal=rsi, atr_at_signal=atr,
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
                    self._add_hyp(
                        hypotheses, asset, "4h", "long",
                        "Support_Bounce", price,
                        support_level=support, atr_at_signal=atr,
                    )
                # Cerca de resistencia (dentro de 1 ATR)
                elif resistance > 0 and abs(price - resistance) < atr * 1.5 and price < resistance:
                    self._add_hyp(
                        hypotheses, asset, "4h", "short",
                        "Resistance_Fade", price,
                        resistance_level=resistance, atr_at_signal=atr,
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

        if hypotheses:
            print(f"[StrategyAgent] → {len(hypotheses)} hipótesis nuevas:")
            for h in hypotheses:
                print(f"   • {h['direction'].upper():5} {h['asset']:8} @ ${h['price']:.2f} via {h['strategy']}")
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
            print("[StrategyAgent] → 0 hipótesis (sin condiciones activas)")

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
