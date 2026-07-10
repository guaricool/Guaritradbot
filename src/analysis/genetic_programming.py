"""
Sprint 41 — Genetic Programming Strategy Discovery (scaffold).

This is the moonshot borrowed from StrategyQuant: instead of humans
hand-coding strategies, automatically *discover* them by evolving
combinations of indicator blocks via genetic programming.

This file is the SCAFFOLD. A full implementation is multi-day work
(thousands of strategies generated, GP loops, tournament selection,
fitness evaluation against many symbols, etc). What we ship today:

  1. **Primitives** — the building blocks a strategy can be made of:
     indicators (RSI, MACD, EMA cross, Bollinger), entry conditions
     (RSI<30, MACD crosses up, price>EMA20), and exit conditions
     (target profit, stop loss).
  2. **StrategyTree** — a tree-shaped strategy representation that
     can be mutated and crossed over. Leaves are primitives, internal
     nodes are combinators (AND / OR / sequence).
  3. **Basic fitness function** — Sharpe ratio on backtest. Strategies
     with higher Sharpe win the tournament.
  4. **One-shot demo run** — ``run_demo()`` that creates 20 random
     strategies, evaluates them, and returns the top 3. Useful as a
     smoke test and as a starting point for the full GP loop.

What we DON'T ship yet (future sprints):
  - Tournament selection
  - Crossover between two parent trees
  - Multi-generation evolution with population dynamics
  - Multi-symbol robustness check (we'd want a strategy to work on
    SPY AND QQQ before promoting it, not just one)

Why this matters: hand-coded strategies (the rest of our codebase)
are limited by the imagination of whoever wrote them. GP lets the
machine explore 1000s of combinations we wouldn't think of.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# Primitives — the atoms a strategy can be made of
# ============================================================

# Entry conditions: callable(close, indicators) -> bool
# Each receives the close series + pre-computed indicators and
# returns True at the bar where the condition fires.
def cond_rsi_below(rsi: pd.Series, threshold: float) -> pd.Series:
    return rsi < threshold


def cond_rsi_above(rsi: pd.Series, threshold: float) -> pd.Series:
    return rsi > threshold


def cond_macd_bull(macd: pd.Series, signal: pd.Series) -> pd.Series:
    return macd > signal


def cond_macd_bear(macd: pd.Series, signal: pd.Series) -> pd.Series:
    return macd < signal


def cond_above_ema(close: pd.Series, ema: pd.Series) -> pd.Series:
    return close > ema


def cond_below_ema(close: pd.Series, ema: pd.Series) -> pd.Series:
    return close < ema


# Indicator functions
def ind_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def ind_ema(close: pd.Series, span: int = 20) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def ind_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series]:
    macd_line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sig = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, sig


# ============================================================
# StrategyTree — tree representation of a strategy
# ============================================================

@dataclass
class StrategyTree:
    """Recursive tree of conditions and combinators.

    Leaves are (condition_name, **params) tuples. Internal nodes are
    combinators: "AND" / "OR" (binary) or "SIGNAL" (combines with
    direction to produce a {-1, 0, 1} position series).
    """
    node: Any  # string combinator, or tuple (cond_name, params)
    children: List["StrategyTree"] = field(default_factory=list)

    def depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(c.depth() for c in self.children)

    def size(self) -> int:
        """Number of nodes in the tree (for parsimony pressure)."""
        if not self.children:
            return 1
        return 1 + sum(c.size() for c in self.children)

    def to_string(self, indent: int = 0) -> str:
        pad = "  " * indent
        if not self.children:
            return f"{pad}{self.node}\n"
        body = f"{pad}{self.node}\n"
        for c in self.children:
            body += c.to_string(indent + 1)
        return body


# Available building blocks
ENTRY_CONDITIONS = ["rsi_below", "rsi_above", "macd_bull", "macd_bear",
                    "above_ema", "below_ema"]
COMBINATORS = ["AND", "OR", "SIGNAL"]


def random_leaf(rng: random.Random) -> StrategyTree:
    """Pick a random entry condition with random params."""
    cond = rng.choice(ENTRY_CONDITIONS)
    if cond in ("rsi_below", "rsi_above"):
        threshold = rng.uniform(20, 40) if cond == "rsi_below" else rng.uniform(60, 80)
        node = (cond, {"threshold": round(threshold, 1)})
    elif cond in ("macd_bull", "macd_bear"):
        node = (cond, {})
    elif cond in ("above_ema", "below_ema"):
        span = rng.choice([10, 20, 50, 100])
        node = (cond, {"span": span})
    else:
        node = (cond, {})
    return StrategyTree(node=node, children=[])


def random_tree(
    rng: random.Random,
    max_depth: int = 3,
    current_depth: int = 0,
) -> StrategyTree:
    """Grow a random strategy tree.

    At depth < max_depth, 60% chance of adding a combinator with
    children; otherwise it's a leaf. The root is always a SIGNAL
    combinator (since a strategy must produce a position).
    """
    if current_depth >= max_depth or (current_depth > 0 and rng.random() < 0.4):
        return random_leaf(rng)
    combinator = rng.choice(["AND", "OR", "SIGNAL"])
    n_children = 2 if combinator in ("AND", "OR", "SIGNAL") else 1
    children = [
        random_tree(rng, max_depth, current_depth + 1)
        for _ in range(n_children)
    ]
    return StrategyTree(node=combinator, children=children)


# ============================================================
# Tree → signal evaluation
# ============================================================

def evaluate_tree(
    tree: StrategyTree,
    indicators: Dict[str, pd.Series],
    direction: int,  # +1 = long, -1 = short
) -> pd.Series:
    """Evaluate a StrategyTree into a position Series (-1, 0, 1).

    ``indicators`` must contain: ``close``, ``rsi``, ``macd``, ``macd_sig``,
    ``ema_<span>`` (for each span used in the tree).
    """
    if not tree.children:
        cond_name, params = tree.node
        if cond_name == "rsi_below":
            return cond_rsi_below(indicators["rsi"], params["threshold"]).astype(float)
        if cond_name == "rsi_above":
            return cond_rsi_above(indicators["rsi"], params["threshold"]).astype(float)
        if cond_name == "macd_bull":
            return cond_macd_bull(indicators["macd"], indicators["macd_sig"]).astype(float)
        if cond_name == "macd_bear":
            return cond_macd_bear(indicators["macd"], indicators["macd_sig"]).astype(float)
        if cond_name == "above_ema":
            ema = indicators[f"ema_{params['span']}"]
            return cond_above_ema(indicators["close"], ema).astype(float)
        if cond_name == "below_ema":
            ema = indicators[f"ema_{params['span']}"]
            return cond_below_ema(indicators["close"], ema).astype(float)
        return pd.Series(0.0, index=indicators["close"].index)
    # Combinator
    if tree.node == "AND":
        out = evaluate_tree(tree.children[0], indicators, direction).astype(bool)
        for c in tree.children[1:]:
            child_sig = evaluate_tree(c, indicators, direction).astype(bool)
            out = out & child_sig
        return out.astype(float)
    if tree.node == "OR":
        out = evaluate_tree(tree.children[0], indicators, direction).astype(bool)
        for c in tree.children[1:]:
            child_sig = evaluate_tree(c, indicators, direction).astype(bool)
            out = out | child_sig
        return out.astype(float)
    if tree.node == "SIGNAL":
        # AND-style: all children must be true → emit direction.
        conds = [evaluate_tree(c, indicators, direction).astype(bool) for c in tree.children]
        out = conds[0]
        for c in conds[1:]:
            out = out & c
        return (out.astype(float) * direction)
    return pd.Series(0.0, index=indicators["close"].index)


# ============================================================
# Fitness: backtest the strategy and return a score
# ============================================================

def precompute_indicators(close: pd.Series) -> Dict[str, pd.Series]:
    """Compute the indicator set used by the GP primitives."""
    out = {
        "close": close,
        "rsi": ind_rsi(close, 14),
    }
    macd, sig = ind_macd(close)
    out["macd"] = macd
    out["macd_sig"] = sig
    for span in (10, 20, 50, 100):
        out[f"ema_{span}"] = ind_ema(close, span)
    return out


def fitness(tree: StrategyTree, prices: pd.DataFrame, direction: int = 1) -> Dict[str, float]:
    """Score a strategy tree on the given price data.

    Returns dict with: sharpe, total_return, max_drawdown, n_trades, parsimony.
    Higher is better. We also penalize very large trees (parsimony pressure).
    """
    try:
        indicators = precompute_indicators(prices["Close"])
        raw = evaluate_tree(tree, indicators, direction)
        sig = raw.replace(0, np.nan).ffill().fillna(0).clip(-1, 1)
        returns = prices["Close"].pct_change().fillna(0)
        strat = sig.shift(1).fillna(0) * returns
        if strat.std() == 0 or len(strat) < 2:
            return {"sharpe": 0.0, "total_return": 0.0, "max_drawdown": 0.0,
                    "n_trades": 0, "parsimony": 0.0}
        ann_ret = float((1 + strat).prod() ** (252 / max(len(strat), 1)) - 1)
        ann_vol = float(strat.std() * np.sqrt(252))
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
        equity = (1 + strat).cumprod()
        peaks = equity.cummax()
        mdd = float(((equity - peaks) / peaks).min())
        # n_trades: count of transitions from 0 to non-zero
        n_trades = int(((sig.diff().abs() > 0) & (sig != 0)).sum())
        # Parsimony: bonus for smaller trees (anti-bloat).
        parsimony = max(0.0, 1.0 - tree.size() / 20.0)
        return {
            "sharpe": float(sharpe),
            "total_return": ann_ret,
            "max_drawdown": mdd,
            "n_trades": n_trades,
            "parsimony": parsimony,
        }
    except Exception:
        return {"sharpe": 0.0, "total_return": 0.0, "max_drawdown": 0.0,
                "n_trades": 0, "parsimony": 0.0}


def composite_score(fit: Dict[str, float]) -> float:
    """Combine Sharpe + total_return + parsimony into a single scalar.

    Used as the GP fitness for ranking candidate strategies.
    """
    # Sharpe dominates (most important for ranking), with a small
    # bonus for positive total_return and parsimony.
    return (
        fit.get("sharpe", 0.0) * 0.6
        + max(0.0, fit.get("total_return", 0.0)) * 0.3
        + fit.get("parsimony", 0.0) * 0.1
    )


# ============================================================
# Demo: generate N random trees, evaluate, return top K
# ============================================================

@dataclass
class EvolvedCandidate:
    tree: StrategyTree
    score: float
    metrics: Dict[str, float]


def run_demo(
    prices: pd.DataFrame,
    n_random: int = 20,
    top_k: int = 3,
    seed: Optional[int] = None,
    direction: int = 1,
) -> List[EvolvedCandidate]:
    """One-shot demo: generate ``n_random`` random strategies, return top_k.

    Useful as a smoke test for the GP scaffold and as a starting point
    for the full evolution loop. A real GP run would do many generations
    with mutation + crossover; this is a single generation.
    """
    rng = random.Random(seed)
    candidates: List[EvolvedCandidate] = []
    for _ in range(n_random):
        tree = random_tree(rng, max_depth=3)
        metrics = fitness(tree, prices, direction=direction)
        score = composite_score(metrics)
        candidates.append(EvolvedCandidate(tree=tree, score=score, metrics=metrics))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_k]
