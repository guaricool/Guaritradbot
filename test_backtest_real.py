"""
Backtest real con datos de yfinance.
Evalua las 3 estrategias del bot contra buy & hold (benchmark honesto).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf
import pandas as pd
import numpy as np
from src.optimization.backtester import VectorizedBacktester
from src.agents.strategy_agent import StrategyAgent


def compute_rsi_wilder(close: pd.Series, window: int = 14) -> pd.Series:
    """RSI con suavizado de Wilder (estándar institucional), NO SMA."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    # Wilder smoothing = EMA con alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch(asset: str, period: str = "2y", interval: str = "1d") -> pd.DataFrame:
    print(f"[data] Descargando {asset} ({interval}, {period})...", end=" ", flush=True)
    df = yf.download(asset, period=period, interval=interval, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df["RSI"] = compute_rsi_wilder(df["Close"], 14)
    df["EMA_20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA_50"] = df["Close"].ewm(span=50, adjust=False).mean()
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df = df.dropna()
    print(f"OK ({len(df)} velas)")
    return df


def rsi_signals(df, **p):
    """RSI long-only cuando RSI < oversold, exit cuando RSI > overbought, sino flat."""
    oversold = p.get("rsi_oversold", 30)
    overbought = p.get("rsi_overbought", 70)
    signals = pd.Series(0.0, index=df.index)
    signals[df["RSI"] < oversold] = 1.0
    signals[df["RSI"] > overbought] = 0.0  # explicit flat
    return signals


def macd_signals(df, **p):
    """MACD: entrar long cuando MACD cruza al alza Signal, salir cuando cruza a la baja."""
    long_cross = (df["MACD"] > df["MACD_Signal"]) & (df["MACD"].shift(1) <= df["MACD_Signal"].shift(1))
    exit_cross = (df["MACD"] < df["MACD_Signal"]) & (df["MACD"].shift(1) >= df["MACD_Signal"].shift(1))
    signals = pd.Series(0.0, index=df.index)
    signals[long_cross] = 1.0
    signals[exit_cross] = 0.0
    # forward-fill para mantener posición
    return signals.replace(0, np.nan).ffill().fillna(0)


def ema_signals(df, **p):
    """EMA cross: long cuando EMA20 > EMA50, flat cuando cruza a la baja."""
    signals = pd.Series(0.0, index=df.index)
    signals[df["EMA_20"] > df["EMA_50"]] = 1.0
    return signals


def buy_hold_signals(df, **p):
    return pd.Series(1.0, index=df.index)


def run_test(name, df, signal_func, **params):
    bt = VectorizedBacktester(initial_capital=10000.0, commission=0.001, slippage=0.0005)
    res = bt.run(df, lambda d: signal_func(d, **params))
    m = res["metrics"]
    print(f"  {name:<35} Return={m['total_return']:>7.2%}  Sharpe={m['sharpe_ratio']:>6.2f}  "
          f"MaxDD={m['max_drawdown']:>7.2%}  WinRate={m['win_rate']:>6.2%}  Trades={m['num_trades']}")
    return m


def main():
    print("=" * 90)
    print("BACKTEST REAL — Datos de yfinance (2 años, daily)")
    print("=" * 90)

    assets = ["BTC-USD", "SPY", "GLD", "QQQ", "USO"]
    for asset in assets:
        print(f"\n{'='*90}\n{asset}\n{'='*90}")
        try:
            df = fetch(asset, period="2y", interval="1d")
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        print(f"  Período: {df.index[0].date()} → {df.index[-1].date()}  ({len(df)} velas)")
        print(f"  Precio actual: ${df['Close'].iloc[-1]:.2f}")

        # Buy & Hold (benchmark)
        print("\n  [Benchmarks]")
        run_test("Buy & Hold (benchmark)", df, buy_hold_signals)

        # Estrategias del bot
        print("\n  [Estrategias del bot]")
        if asset == "BTC-USD":
            run_test("MACD Cross (BTC strategy)", df, macd_signals)
        if asset in ["SPY", "QQQ"]:
            run_test("RSI MeanReversion default (30/70)", df, rsi_signals, rsi_oversold=30, rsi_overbought=70)
            run_test("RSI MeanReversion wide (20/80)", df, rsi_signals, rsi_oversold=20, rsi_overbought=80)
            run_test("RSI MeanReversion tight (35/65)", df, rsi_signals, rsi_oversold=35, rsi_overbought=65)
        if asset in ["GLD", "USO"]:
            run_test("EMA Cross (Trend Following)", df, ema_signals)

    print("\n" + "=" * 90)
    print("LEYENDA:")
    print("  Return  = Ganancia/pérdida total del periodo")
    print("  Sharpe  = Ratio riesgo/retorno (>1 = bueno, >2 = excelente)")
    print("  MaxDD   = Máxima caída desde un pico (cerca de 0 = bajo riesgo)")
    print("  WinRate = % de barras con retorno positivo")
    print("  Trades  = número de barras con retorno ≠ 0 (proxy de actividad)")
    print("=" * 90)


if __name__ == "__main__":
    main()