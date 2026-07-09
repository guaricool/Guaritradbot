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
"""
import pandas as pd
import numpy as np


class StrategyAgent:
    """
    Analiza market_data y genera hipótesis de trade (entry signals).
    """

    def __init__(self, strategy_params: dict = None):
        self.params = strategy_params or {
            "rsi_oversold": 30,
            "rsi_overbought": 70,
        }

    def evaluate_strategies(self, inputs: dict, state: dict):
        market_data = state.get("analyze_market", {}).get("market_data", {})
        print(f"[StrategyAgent] Evaluando {len(market_data)} assets...")

        hypotheses = []

        # SPY / QQQ → mean reversion con RSI
        for asset in ("SPY", "QQQ"):
            for tf in ("15m", "1h"):
                df = market_data.get(asset, {}).get(tf)
                if df is None:
                    continue
                if len(df) == 0:
                    continue
                if "RSI" not in df.columns.values:
                    continue
                last = df.iloc[-1]
                prev = df.iloc[-2]
                rsi_now = float(last["RSI"])
                rsi_prev = float(prev["RSI"])
                price = float(last["Close"])
                oversold = self.params.get("rsi_oversold", 30)
                overbought = self.params.get("rsi_overbought", 70)

                # Entry LONG cuando RSI cruza POR DEBAJO de oversold
                if rsi_prev >= oversold and rsi_now < oversold:
                    hypotheses.append(
                        {
                            "asset": asset,
                            "tf": tf,
                            "strategy": f"MeanReversion_LONG_RSI<{oversold}",
                            "direction": "long",
                            "price": price,
                            "rsi_at_signal": rsi_now,
                            "atr_at_signal": float(last.get("ATR_14", 0)),
                        }
                    )
                # Entry SHORT cuando RSI cruza POR ENCIMA de overbought
                elif rsi_prev <= overbought and rsi_now > overbought:
                    hypotheses.append(
                        {
                            "asset": asset,
                            "tf": tf,
                            "strategy": f"MeanReversion_SHORT_RSI>{overbought}",
                            "direction": "short",
                            "price": price,
                            "rsi_at_signal": rsi_now,
                            "atr_at_signal": float(last.get("ATR_14", 0)),
                        }
                    )

        # BTC → breakout por cruce MACD
        for asset in ("BTC-USD", "BTCUSDT"):
            df = market_data.get(asset, {}).get("1h")
            if df is None:
                continue
            if len(df) == 0:
                continue
            if "MACD" not in df.columns.values or "MACD_Signal" not in df.columns.values:
                continue
            last = df.iloc[-1]
            prev = df.iloc[-2]
            macd_now = float(last["MACD"])
            sig_now = float(last["MACD_Signal"])
            macd_prev = float(prev["MACD"])
            sig_prev = float(prev["MACD_Signal"])
            price = float(last["Close"])

            if macd_prev <= sig_prev and macd_now > sig_now:  # cruce alcista
                hypotheses.append(
                    {
                        "asset": asset,
                        "tf": "1h",
                        "strategy": "MACD_BullCross",
                        "direction": "long",
                        "price": price,
                        "macd_at_signal": macd_now,
                        "atr_at_signal": float(last.get("ATR_14", 0)),
                    }
                )
            elif macd_prev >= sig_prev and macd_now < sig_now:  # cruce bajista
                hypotheses.append(
                    {
                        "asset": asset,
                        "tf": "1h",
                        "strategy": "MACD_BearCross",
                        "direction": "short",
                        "price": price,
                        "macd_at_signal": macd_now,
                        "atr_at_signal": float(last.get("ATR_14", 0)),
                    }
                )

        # GLD / USO → trend following con cruce EMA
        for asset in ("GLD", "USO"):
            df = market_data.get(asset, {}).get("4h")
            if df is None or len(df) == 0:
                df = market_data.get(asset, {}).get("1h")
            if df is None or len(df) == 0:
                continue
            if "EMA_20" not in df.columns.values or "EMA_50" not in df.columns.values:
                continue
            last = df.iloc[-1]
            prev = df.iloc[-2]
            ema20_now = float(last["EMA_20"])
            ema50_now = float(last["EMA_50"])
            ema20_prev = float(prev["EMA_20"])
            ema50_prev = float(prev["EMA_50"])
            price = float(last["Close"])

            if ema20_prev <= ema50_prev and ema20_now > ema50_now:  # golden cross
                hypotheses.append(
                    {
                        "asset": asset,
                        "tf": "4h" if "4h" in market_data.get(asset, {}) else "1h",
                        "strategy": "EMA_GoldenCross",
                        "direction": "long",
                        "price": price,
                        "ema20_at_signal": ema20_now,
                        "ema50_at_signal": ema50_now,
                        "atr_at_signal": float(last.get("ATR_14", 0)),
                    }
                )
            elif ema20_prev >= ema50_prev and ema20_now < ema50_now:  # death cross
                hypotheses.append(
                    {
                        "asset": asset,
                        "tf": "4h" if "4h" in market_data.get(asset, {}) else "1h",
                        "strategy": "EMA_DeathCross",
                        "direction": "short",
                        "price": price,
                        "ema20_at_signal": ema20_now,
                        "ema50_at_signal": ema50_now,
                        "atr_at_signal": float(last.get("ATR_14", 0)),
                    }
                )

        if hypotheses:
            print(f"[StrategyAgent] → {len(hypotheses)} hipótesis nuevas:")
            for h in hypotheses:
                print(f"   • {h['direction'].upper():5} {h['asset']:8} @ ${h['price']:.2f} via {h['strategy']}")
        else:
            print("[StrategyAgent] → 0 hipótesis (sin cruces detectados)")

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
