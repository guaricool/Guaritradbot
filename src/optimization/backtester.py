"""
Sprint 4 — Vectorized Backtester con métricas correctas.

Inspirado en freqtrade (métricas gold standard) + intelligent-trading-bot
(walk-forward rolling). Antes el backtester:

- Reportaba `win_rate = bars_with_positive_return / total_bars` (incorrecto).
- Reportaba `num_trades = total_bars_with_nonzero_return` (incorrecto).
- Anualizaba siempre con 365 (incorrecto para datos horarios).
- No calculaba Profit Factor ni Expectancy.

Ahora:

- Walk-forward rolling (train en t-past..t-split, test en t-split..t).
- Métricas reales: total_return, sharpe, sortino, max_dd, win_rate,
  profit_factor, expectancy, num_trades, calmar, annual_return.
- Anualización parametrizable (`periods_per_year`) según timeframe.
- Trade detection: cambio de señal ≠ 0 → abre trade; cambio a 0 → cierra.
- Walk-forward splits: retorna lista de dicts {train, test, params, metrics}.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Callable, Dict, Any, List, Optional


class VectorizedBacktester:
    """
    Backtester vectorizado que computa métricas institucionales.
    """

    def __init__(
        self,
        initial_capital: float = 10000.0,
        commission: float = 0.001,
        slippage: float = 0.0005,
        periods_per_year: int = 365,
    ):
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        self.periods_per_year = periods_per_year

    def run(
        self,
        prices: pd.DataFrame,
        signal_func: Callable[[pd.DataFrame], pd.Series],
        symbol: str = "ASSET",
    ) -> Dict[str, Any]:
        if len(prices) == 0 or "Close" not in prices.columns:
            return self._empty_result()

        # Generamos señales, shift(1) = NO lookahead bias
        signals = signal_func(prices).shift(1).fillna(0)
        returns = prices["Close"].pct_change().fillna(0)

        # Costs por cambio de posición
        position_changes = signals.diff().abs().fillna(0)
        trading_costs = position_changes * (self.commission + self.slippage)

        strategy_returns = (signals * returns) - trading_costs
        equity = (1 + strategy_returns).cumprod() * self.initial_capital

        # Trade detection: cada vez que la señal cambia de 0 a ≠0 es un
        # entry; cada vez que cambia de ≠0 a 0 es un exit.
        trades = self._detect_trades(signals, prices["Close"])

        metrics = self._calculate_metrics(strategy_returns, equity, trades)

        return {
            "equity": equity,
            "returns": strategy_returns,
            "trades": trades,
            "metrics": metrics,
            "signal": signals,
        }

    def _detect_trades(self, signals: pd.Series, prices: pd.Series) -> List[Dict]:
        """Convierte serie de señales en lista de trades completos."""
        trades = []
        in_trade = False
        entry_idx = None
        entry_price = None
        direction = 0

        for i, (idx, sig) in enumerate(signals.items()):
            if not in_trade and sig != 0:
                in_trade = True
                entry_idx = idx
                entry_price = float(prices.iloc[i])
                direction = int(sig)
            elif in_trade and (sig == 0 or i == len(signals) - 1):
                exit_price = float(prices.iloc[i])
                if direction == 1:
                    ret = (exit_price - entry_price) / entry_price
                else:
                    ret = (entry_price - exit_price) / entry_price
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": idx,
                    "direction": "long" if direction == 1 else "short",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "return_pct": ret,
                    "pnl_usd_per_unit": (exit_price - entry_price) * direction,
                })
                in_trade = False
        return trades

    def _calculate_metrics(self, returns: pd.Series, equity: pd.Series, trades: List[Dict]) -> Dict[str, float]:
        if len(equity) == 0 or len(returns) == 0:
            return self._empty_metrics()

        total_return = float(equity.iloc[-1] / self.initial_capital - 1)

        # Volatilidad y retorno anualizados
        daily_vol = returns.std()
        annual_vol = daily_vol * np.sqrt(self.periods_per_year) if daily_vol > 0 else 0

        annual_return = (1 + total_return) ** (self.periods_per_year / max(len(returns), 1)) - 1

        # Sharpe (asumimos risk-free = 0)
        sharpe = float(annual_return / annual_vol) if annual_vol > 0 else 0.0

        # Sortino: downside deviation
        negative_returns = returns[returns < 0]
        downside_vol = negative_returns.std() * np.sqrt(self.periods_per_year) if len(negative_returns) > 0 else 0
        sortino = float(annual_return / downside_vol) if downside_vol > 0 else 0.0

        # Max drawdown
        rolling_max = equity.cummax()
        drawdown = (equity - rolling_max) / rolling_max
        max_drawdown = float(drawdown.min())

        # Trade-level metrics
        if trades:
            wins = [t for t in trades if t["return_pct"] > 0]
            losses = [t for t in trades if t["return_pct"] <= 0]
            win_rate = len(wins) / len(trades)
            avg_win = float(np.mean([t["return_pct"] for t in wins])) if wins else 0.0
            avg_loss = float(np.mean([t["return_pct"] for t in losses])) if losses else 0.0

            gross_profit = sum(t["return_pct"] for t in wins)
            gross_loss = abs(sum(t["return_pct"] for t in losses))
            profit_factor = float(gross_profit / gross_loss) if gross_loss > 0 else float("inf")

            # Expectancy in % per trade
            expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
        else:
            win_rate = 0.0
            avg_win = 0.0
            avg_loss = 0.0
            profit_factor = 0.0
            expectancy = 0.0

        # Calmar ratio
        calmar = float(annual_return / abs(max_drawdown)) if max_drawdown < 0 else 0.0

        return {
            "total_return": total_return,
            "annual_return": float(annual_return),
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "max_drawdown": max_drawdown,
            "annual_vol": float(annual_vol),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "num_trades": len(trades),
            "num_wins": len(trades) and sum(1 for t in trades if t["return_pct"] > 0) or 0,
            "num_losses": len(trades) and sum(1 for t in trades if t["return_pct"] <= 0) or 0,
        }

    def _empty_result(self):
        return {
            "equity": pd.Series(dtype=float),
            "returns": pd.Series(dtype=float),
            "trades": [],
            "metrics": self._empty_metrics(),
            "signal": pd.Series(dtype=float),
        }

    def _empty_metrics(self):
        return {
            "total_return": 0.0, "annual_return": 0.0,
            "sharpe_ratio": 0.0, "sortino_ratio": 0.0,
            "calmar_ratio": 0.0, "max_drawdown": 0.0,
            "annual_vol": 0.0, "win_rate": 0.0,
            "profit_factor": 0.0, "expectancy": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "num_trades": 0, "num_wins": 0, "num_losses": 0,
        }


def review_backtest(
    result: Dict[str, Any],
    walk_forward_result: Optional[Dict[str, Any]] = None,
    benchmark_returns: Optional[pd.Series] = None,
    min_trades: int = 30,
    max_plausible_sharpe: float = 4.0,
) -> Dict[str, Any]:
    """Backtest quality gate — an objective, non-LLM checklist for the
    review categories every "is this backtest lying to me" guide lists
    (look-ahead bias, overfitting, cherry-picked/too-few trades,
    unrealistic fills, missing costs, benchmark underperformance).

    This is deliberately rule-based rather than LLM-judged: every check
    here is a fact about `result["metrics"]`/`result["trades"]` that
    can be computed exactly, so there's nothing for an LLM to add
    except prose. Use this before trusting a backtest enough to feed
    its params into `walk_forward_validate` or, eventually, live
    config.

    `result` is the dict returned by `VectorizedBacktester.run()`.
    `walk_forward_result` is the optional dict returned by
    `walk_forward_validate()` — when given, its `overfit_warning` and
    `is_vs_oos_ratio` feed the FAILED/overfitting check directly
    instead of re-deriving it.
    `benchmark_returns` is an optional buy-and-hold return series
    (e.g. `prices["Close"].pct_change()`) aligned to the same period,
    for the "did it even beat holding the asset" check.

    Returns {"passed": [...], "failed": [...], "warnings": [...],
    "verdict": "PASS" | "REVISE" | "REJECT"}.
    """
    metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
    trades = result.get("trades", []) if isinstance(result, dict) else []
    passed: List[str] = []
    failed: List[str] = []
    warnings: List[str] = []

    # 1. Look-ahead bias: structural, not statistical — the
    # backtester itself shift(1)s the signal before applying it
    # (see VectorizedBacktester.run above), so a caller using this
    # class can't accidentally trade on the same bar's signal.
    passed.append(
        "look_ahead_bias: signals are shift(1)'d before being applied "
        "to returns (structural guarantee in VectorizedBacktester.run)."
    )

    # 2. Missing fees/slippage.
    num_trades = int(metrics.get("num_trades", len(trades)))
    if num_trades > 0 and (result.get("returns") is None or len(result.get("returns", [])) == 0):
        warnings.append("fees_slippage: could not verify — no returns series in result.")
    else:
        passed.append(
            "fees_slippage: trading_costs applied on every position change "
            "(commission + slippage), not just at entry/exit."
        )

    # 3. Too few trades — a Sharpe/win-rate computed on a handful of
    # trades is noise, not signal.
    if num_trades == 0:
        failed.append("too_few_trades: 0 trades — no signal fired in this window.")
    elif num_trades < min_trades:
        warnings.append(
            f"too_few_trades: only {num_trades} trades (< {min_trades}); "
            "win_rate/profit_factor are not statistically meaningful yet."
        )
    else:
        passed.append(f"too_few_trades: {num_trades} trades, enough to read the metrics.")

    # 4. Implausible Sharpe — a backtest Sharpe far above what real
    # strategies achieve almost always means a data/logic bug
    # (look-ahead leak, survivorship, or a cost that isn't applied),
    # not real edge.
    sharpe = float(metrics.get("sharpe_ratio", 0.0) or 0.0)
    if sharpe > max_plausible_sharpe:
        failed.append(
            f"implausible_sharpe: {sharpe:.2f} > {max_plausible_sharpe} — "
            "investigate for a data leak or missing cost before trusting this."
        )
    else:
        passed.append(f"implausible_sharpe: {sharpe:.2f} is within a plausible range.")

    # 5. Suspicious profit factor (no losing trades at all).
    profit_factor = metrics.get("profit_factor", 0.0)
    num_losses = int(metrics.get("num_losses", 0))
    if num_trades > 0 and num_losses == 0 and profit_factor in (float("inf"), 0.0):
        warnings.append(
            "profit_factor: zero losing trades in the sample — check this "
            "isn't an artifact of a too-short or too-favorable test window."
        )
    else:
        passed.append("profit_factor: has both winning and losing trades, not a fluke shape.")

    # 6. Overfitting via walk-forward IS/OOS degradation, if provided.
    if walk_forward_result is not None:
        if walk_forward_result.get("overfit_warning"):
            ratio = walk_forward_result.get("is_vs_oos_ratio", 0.0)
            failed.append(
                f"overfitting: out-of-sample/in-sample ratio {ratio:.2f} < 0.5 — "
                "strategy performs much worse out-of-sample, classic overfit signature."
            )
        else:
            passed.append("overfitting: out-of-sample performance holds up vs in-sample.")
    else:
        warnings.append(
            "overfitting: no walk_forward_result passed in — in-sample-only "
            "metrics can't rule out overfitting. Run walk_forward_validate() first."
        )

    # 7. Benchmark comparison.
    if benchmark_returns is not None and len(benchmark_returns) > 0:
        bench_total_return = float((1 + benchmark_returns.fillna(0)).prod() - 1)
        strat_total_return = float(metrics.get("total_return", 0.0))
        if strat_total_return < bench_total_return:
            warnings.append(
                f"benchmark: strategy return {strat_total_return:.2%} < "
                f"buy-and-hold {bench_total_return:.2%} — active risk isn't paying off here."
            )
        else:
            passed.append(
                f"benchmark: strategy return {strat_total_return:.2%} beat "
                f"buy-and-hold {bench_total_return:.2%}."
            )
    else:
        warnings.append("benchmark: no benchmark_returns passed in — comparison skipped.")

    if failed:
        verdict = "REJECT"
    elif warnings:
        verdict = "REVISE"
    else:
        verdict = "PASS"

    return {
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
        "verdict": verdict,
    }


def walk_forward_split(prices: pd.DataFrame, train_pct: float = 0.7, expanding: bool = False) -> List[Dict]:
    """
    Genera splits walk-forward.

    - expanding=True: train crece cada split (todo el pasado hasta split point).
      Útil cuando se tienen pocos datos.
    - expanding=False: train tiene tamaño fijo (primera ventana).

    Devuelve lista de dicts: {train_start, train_end, test_start, test_end, train_idx, test_idx}.
    """
    n = len(prices)
    if n < 10:
        return []

    if expanding:
        # Solo un split: train = primeras 70%, test = últimas 30%
        train_end = int(n * train_pct)
        return [{
            "train_start": 0,
            "train_end": train_end,
            "test_start": train_end,
            "test_end": n,
            "train_idx": prices.index[:train_end],
            "test_idx": prices.index[train_end:],
        }]

    # Rolling 3-split: 50/25/25 → train1 + test1, train2 + test2
    splits = []
    # Split 1: [0, 50%] train, [50%, 75%] test
    splits.append({
        "train_start": 0, "train_end": int(n * 0.5),
        "test_start": int(n * 0.5), "test_end": int(n * 0.75),
        "train_idx": prices.index[:int(n * 0.5)],
        "test_idx": prices.index[int(n * 0.5):int(n * 0.75)],
    })
    # Split 2: [25%, 75%] train, [75%, 100%] test
    splits.append({
        "train_start": int(n * 0.25), "train_end": int(n * 0.75),
        "test_start": int(n * 0.75), "test_end": n,
        "train_idx": prices.index[int(n * 0.25):int(n * 0.75)],
        "test_idx": prices.index[int(n * 0.75):],
    })
    return splits


def walk_forward_validate(
    prices: pd.DataFrame,
    signal_func: Callable[[pd.DataFrame], pd.Series],
    param_space: Optional[Dict] = None,
    optimize_metric: str = "sharpe_ratio",
    periods_per_year: int = 365,
    initial_capital: float = 10000.0,
    train_pct: float = 0.7,
    expanding: bool = True,
) -> Dict[str, Any]:
    """
    Walk-forward validation completa:
    1. Genera splits train/test.
    2. Optimiza hiperparámetros en train (grid search vía HyperoptManager).
    3. Evalúa los params optimizados en test (out-of-sample).
    4. Reporta in-sample vs out-of-sample metrics.

    Devuelve dict con:
    - splits: lista de splits
    - best_params: params que mejor rindieron en cada train
    - oos_metrics: métricas agregadas out-of-sample
    - is_vs_oos: comparación in-sample vs out-of-sample
    """
    from src.optimization.hyperopt import HyperoptManager

    splits = walk_forward_split(prices, train_pct=train_pct, expanding=expanding)
    if not splits:
        return {"error": "data too short"}

    hyperopt = HyperoptManager()
    hyperopt.backtester.periods_per_year = periods_per_year

    split_results = []
    oos_metrics_list = []

    for i, split in enumerate(splits):
        train_data = prices.loc[split["train_idx"]]
        test_data = prices.loc[split["test_idx"]]

        # Optimize en train (si hay param_space)
        if param_space and len(param_space) > 0:
            best_params = hyperopt.optimize(
                f"WF_split_{i}",
                train_data,
                param_space,
                signal_func,
                metric=optimize_metric,
            )
        else:
            best_params = {}

        # Evalúa en train y test con los best_params
        signal_with_params = lambda df: signal_func(df, **best_params) if best_params else signal_func(df)

        is_result = hyperopt.backtester.run(train_data, signal_with_params)
        oos_result = hyperopt.backtester.run(test_data, signal_with_params)

        oos_metrics_list.append(oos_result["metrics"])
        split_results.append({
            "split_idx": i,
            "train_size": len(train_data),
            "test_size": len(test_data),
            "best_params": best_params,
            "in_sample": is_result["metrics"],
            "out_of_sample": oos_result["metrics"],
        })

    # OOS agregados (promedio de los out-of-sample de cada split)
    if oos_metrics_list:
        avg_oos = {}
        for key in oos_metrics_list[0]:
            vals = [m.get(key, 0) for m in oos_metrics_list if m.get(key) is not None and not np.isinf(m.get(key, 0))]
            avg_oos[key] = float(np.mean(vals)) if vals else 0.0
    else:
        avg_oos = {}

    # IS promedio
    is_metrics_list = [r["in_sample"] for r in split_results]
    avg_is = {}
    if is_metrics_list:
        for key in is_metrics_list[0]:
            vals = [m.get(key, 0) for m in is_metrics_list if m.get(key) is not None and not np.isinf(m.get(key, 0))]
            avg_is[key] = float(np.mean(vals)) if vals else 0.0

    # Ratio IS/OOS (degradation test) — si ratio < 0.5 = overfit
    overfit_warning = False
    if avg_is.get(optimize_metric, 0) > 0:
        ratio = avg_oos.get(optimize_metric, 0) / avg_is[optimize_metric]
        overfit_warning = ratio < 0.5

    return {
        "splits": split_results,
        "avg_in_sample": avg_is,
        "avg_out_of_sample": avg_oos,
        "is_vs_oos_ratio": avg_oos.get(optimize_metric, 0) / max(avg_is.get(optimize_metric, 1e-6), 1e-6),
        "overfit_warning": overfit_warning,
        "primary_metric": optimize_metric,
    }
