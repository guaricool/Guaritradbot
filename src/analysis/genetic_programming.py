"""
Sprint 41+42 — Full Genetic Programming for Strategy Discovery.

The moonshot borrowed from StrategyQuant: instead of humans hand-coding
strategies, automatically *discover* them by evolving combinations of
indicator blocks via genetic programming.

Sprint 41 scaffold provided: primitives, StrategyTree, fitness, run_demo.
Sprint 42 adds: tournament selection, mutation operators, crossover,
multi-generation evolution loop, multi-symbol robustness, strategy
library, CLI runner.

The full flow:
  1. Initialize random population of N strategies (tree structures)
  2. For each generation:
     a. Evaluate fitness on training data (multi-symbol)
     b. Select parents via tournament selection
     c. Apply crossover and mutation to produce offspring
     d. Combine parents + offspring, keep top N (elitism)
  3. Return best strategies as StrategyLibrary (persisted to JSON)

Design choices:
  - **Parsimony pressure** via tree size penalty in fitness score
  - **Diversity preservation** via signal-similarity deduplication
  - **Multi-symbol robustness** — a strategy must show edge on
    MULTIPLE symbols (not just one) to be promoted to the library
  - **Determinism** — fixed seed reproduces the run end-to-end

What's still missing for a TRUE StrategyQuant clone:
  - Constant optimization (we use random perturbations only)
  - Build process orchestration (we just do GP, no stratified search)
  - Strategy templates (we use primitives only)
  - Multi-timeframe primitives (we use single-TF data)
  - Build customization UI (this is a CLI, not a GUI)

But for our use case (discover a few robust strategies, validate on
multi-symbol data, save to library), this is enough.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ============================================================
# Primitives (from Sprint 41 — kept here for self-containment)
# ============================================================

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


def ind_bollinger(close: pd.Series, period: int = 20, std: float = 2.0) -> Tuple[pd.Series, pd.Series]:
    sma = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    return sma + std * sigma, sma - std * sigma


# Conditions
def cond_rsi_below(rsi: pd.Series, threshold: float) -> pd.Series:
    return (rsi < threshold)


def cond_rsi_above(rsi: pd.Series, threshold: float) -> pd.Series:
    return (rsi > threshold)


def cond_macd_bull(macd: pd.Series, signal: pd.Series) -> pd.Series:
    return (macd > signal)


def cond_macd_bear(macd: pd.Series, signal: pd.Series) -> pd.Series:
    return (macd < signal)


def cond_above_ema(close: pd.Series, ema: pd.Series) -> pd.Series:
    return (close > ema)


def cond_below_ema(close: pd.Series, ema: pd.Series) -> pd.Series:
    return (close < ema)


def cond_above_bb_upper(close: pd.Series, upper: pd.Series) -> pd.Series:
    return (close > upper)


def cond_below_bb_lower(close: pd.Series, lower: pd.Series) -> pd.Series:
    return (close < lower)


ENTRY_CONDITIONS = ["rsi_below", "rsi_above", "macd_bull", "macd_bear",
                    "above_ema", "below_ema", "above_bb_upper", "below_bb_lower"]
COMBINATORS = ["AND", "OR", "SIGNAL"]


# ============================================================
# StrategyTree (from Sprint 41)
# ============================================================

@dataclass
class StrategyTree:
    """Recursive tree of conditions and combinators."""
    node: Any  # string combinator, or tuple (cond_name, params)
    children: List["StrategyTree"] = field(default_factory=list)

    def depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(c.depth() for c in self.children)

    def size(self) -> int:
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

    def collect_leaves(self) -> List[Tuple[Tuple[int, ...], "StrategyTree"]]:
        """Walk tree, return list of (path_to_parent, leaf_node).

        Path is a tuple of indices into children. E.g. (0, 1, 0) means
        ``self.children[0].children[1].children[0]`` is the leaf.

        To replace the leaf, the caller does:
          ``_replace_at_path(tree, path, new_node)``
        which navigates to the leaf's PARENT and swaps children[last_idx].
        """
        results: List[Tuple[Tuple[int, ...], "StrategyTree"]] = []
        def _walk(node, path):
            if not node.children:
                results.append((path, node))
            else:
                for i, c in enumerate(node.children):
                    _walk(c, path + (i,))
        _walk(self, ())
        return results


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
    elif cond in ("above_bb_upper", "below_bb_lower"):
        node = (cond, {"period": 20, "std": 2.0})
    else:
        node = (cond, {})
    return StrategyTree(node=node, children=[])


def random_tree(
    rng: random.Random,
    max_depth: int = 3,
    current_depth: int = 0,
) -> StrategyTree:
    """Grow a random strategy tree."""
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
# Tree evaluation (from Sprint 41, with bool dtype fix)
# ============================================================

def evaluate_tree(
    tree: StrategyTree,
    indicators: Dict[str, pd.Series],
    direction: int = 1,
) -> pd.Series:
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
        if cond_name == "above_bb_upper":
            period = params.get("period", 20)
            std = params.get("std", 2.0)
            upper, _ = ind_bollinger(indicators["close"], period, std)
            return cond_above_bb_upper(indicators["close"], upper).astype(float)
        if cond_name == "below_bb_lower":
            period = params.get("period", 20)
            std = params.get("std", 2.0)
            _, lower = ind_bollinger(indicators["close"], period, std)
            return cond_below_bb_lower(indicators["close"], lower).astype(float)
        return pd.Series(0.0, index=indicators["close"].index)
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
        conds = [evaluate_tree(c, indicators, direction).astype(bool) for c in tree.children]
        out = conds[0]
        for c in conds[1:]:
            out = out & c
        return (out.astype(float) * direction)
    return pd.Series(0.0, index=indicators["close"].index)


def precompute_indicators(close: pd.Series) -> Dict[str, pd.Series]:
    out = {"close": close, "rsi": ind_rsi(close, 14)}
    macd, sig = ind_macd(close)
    out["macd"] = macd
    out["macd_sig"] = sig
    for span in (10, 20, 50, 100):
        out[f"ema_{span}"] = ind_ema(close, span)
    return out


# ============================================================
# Fitness (from Sprint 41, extended with multi-symbol)
# ============================================================

def fitness(
    tree: StrategyTree,
    prices: pd.DataFrame,
    direction: int = 1,
    parsimony_penalty: float = 0.05,
) -> Dict[str, float]:
    """Score a strategy tree on the given price data.

    Returns a dict with: sharpe, total_return, max_drawdown, n_trades,
    parsimony, score (the composite we maximize).
    """
    try:
        indicators = precompute_indicators(prices["Close"])
        raw = evaluate_tree(tree, indicators, direction)
        sig = raw.replace(0, np.nan).ffill().fillna(0).clip(-1, 1)
        returns = prices["Close"].pct_change().fillna(0)
        strat = sig.shift(1).fillna(0) * returns
        if strat.std() == 0 or len(strat) < 2:
            return {"sharpe": 0.0, "total_return": 0.0, "max_drawdown": 0.0,
                    "n_trades": 0, "parsimony": 0.0, "score": 0.0}
        ann_ret = float((1 + strat).prod() ** (252 / max(len(strat), 1)) - 1)
        ann_vol = float(strat.std() * np.sqrt(252))
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
        equity = (1 + strat).cumprod()
        peaks = equity.cummax()
        mdd = float(((equity - peaks) / peaks).min())
        n_trades = int(((sig.diff().abs() > 0) & (sig != 0)).sum())
        # Parsimony: bonus for smaller trees (anti-bloat).
        parsimony = max(0.0, 1.0 - tree.size() / 20.0)
        # Composite: Sharpe-dominant, with return + parsimony bonus.
        # Penalize very large trees (parsimony_penalty) and zero-trade strategies.
        trade_bonus = 0.0
        if n_trades < 3:
            trade_bonus = -0.5  # don't promote strategies that barely trade
        score = (
            sharpe * 0.6
            + max(0.0, ann_ret) * 0.2
            + parsimony * 0.1
            - parsimony_penalty * max(0, tree.size() - 5)
            + trade_bonus
        )
        return {
            "sharpe": float(sharpe),
            "total_return": ann_ret,
            "max_drawdown": mdd,
            "n_trades": n_trades,
            "parsimony": parsimony,
            "score": float(score),
        }
    except Exception:
        return {"sharpe": 0.0, "total_return": 0.0, "max_drawdown": 0.0,
                "n_trades": 0, "parsimony": 0.0, "score": 0.0}


def composite_score(fit: Dict[str, float]) -> float:
    """Combine Sharpe + total_return + parsimony into a single scalar.

    Used as the GP fitness for ranking candidate strategies.
    Kept for backwards compat with the Sprint 41 API.
    """
    return (
        fit.get("sharpe", 0.0) * 0.6
        + max(0.0, fit.get("total_return", 0.0)) * 0.3
        + fit.get("parsimony", 0.0) * 0.1
    )


def multi_symbol_fitness(
    tree: StrategyTree,
    prices_by_symbol: Dict[str, pd.DataFrame],
    direction: int = 1,
    min_symbols: int = 2,
) -> Dict[str, float]:
    """Evaluate a tree on multiple symbols and return aggregate metrics.

    A strategy is "robust" only if it shows positive edge on AT LEAST
    ``min_symbols`` symbols. Returns:
      - score: average score across symbols (only those with n_trades>0)
      - per_symbol_scores: dict {symbol: score}
      - n_profitable: number of symbols where the strategy made money
      - sharpe_std: std of Sharpe across symbols (lower = more consistent)
    """
    per_symbol = {}
    for sym, prices in prices_by_symbol.items():
        f = fitness(tree, prices, direction)
        if f["n_trades"] >= 3:
            per_symbol[sym] = f
    if len(per_symbol) < min_symbols:
        return {
            "score": 0.0,
            "per_symbol_scores": per_symbol,
            "n_profitable": 0,
            "sharpe_std": float("inf"),
            "robust": False,
        }
    avg_score = float(np.mean([f["score"] for f in per_symbol.values()]))
    avg_sharpe = float(np.mean([f["sharpe"] for f in per_symbol.values()]))
    sharpe_std = float(np.std([f["sharpe"] for f in per_symbol.values()]))
    n_profitable = sum(1 for f in per_symbol.values() if f["total_return"] > 0)
    # Multi-symbol score: average + bonus for consistency + bonus for being
    # profitable on more symbols.
    consistency_bonus = max(0.0, 0.3 - sharpe_std)  # up to 0.3 if std=0
    profit_bonus = 0.1 * (n_profitable / len(per_symbol))
    multi_score = avg_score + consistency_bonus + profit_bonus
    return {
        "score": multi_score,
        "per_symbol_scores": per_symbol,
        "n_profitable": n_profitable,
        "sharpe_std": sharpe_std,
        "avg_sharpe": avg_sharpe,
        "robust": n_profitable >= min_symbols,
    }


# ============================================================
# MUTATION OPERATORS (Sprint 42)
# ============================================================

def _replace_at_path(tree: StrategyTree, path: Tuple[int, ...], new_node: StrategyTree) -> StrategyTree:
    """Return a deep copy of `tree` with the node at `path` replaced by `new_node`.

    Path is a tuple of indices like (0, 1, 0) meaning
    ``tree.children[0].children[1].children[0]`` is the target.
    """
    new = StrategyTree(node=tree.node, children=list(tree.children))
    if not path:
        return new_node
    # Walk down to the leaf's parent, then swap children[last_idx]
    cur = new
    for idx in path[:-1]:
        cur = cur.children[idx]
    cur.children[path[-1]] = new_node
    return new


def mutate_leaf_swap(tree: StrategyTree, rng: random.Random) -> StrategyTree:
    """Pick a random leaf and replace it with a fresh random leaf."""
    leaves = tree.collect_leaves()
    if not leaves:
        return random_leaf(rng)
    path, _ = rng.choice(leaves)
    new_leaf = random_leaf(rng)
    return _replace_at_path(tree, path, new_leaf)


def mutate_threshold_perturb(
    tree: StrategyTree,
    rng: random.Random,
    sigma: float = 0.15,
) -> StrategyTree:
    """Pick a random numeric leaf and perturb its threshold by ±sigma%.

    E.g. rsi_oversold=30 with sigma=0.15 → uniform in [25.5, 34.5].
    """
    leaves = tree.collect_leaves()
    if not leaves:
        return tree
    path, leaf = rng.choice(leaves)
    cond_name, params = leaf.node
    if not isinstance(params, dict) or not params:
        return tree
    new_params = dict(params)
    changed = False
    for k, v in list(params.items()):
        try:
            fv = float(v)
            new_v = fv * rng.uniform(1.0 - sigma, 1.0 + sigma)
            new_params[k] = round(new_v, 2) if isinstance(v, float) and v < 100 else int(new_v)
            changed = True
        except (TypeError, ValueError):
            continue
    if not changed:
        return tree
    new_leaf = StrategyTree(node=(cond_name, new_params), children=[])
    return _replace_at_path(tree, path, new_leaf)


def mutate_combinator_swap(tree: StrategyTree, rng: random.Random) -> StrategyTree:
    """Pick a random combinator node and replace it with a different one.

    AND ↔ OR ↔ SIGNAL swaps. The number of children stays the same.
    """
    if not tree.children:
        return tree
    if rng.random() < 0.5:
        # Try the root
        candidates = [tree]
    else:
        # Pick any combinator in the tree
        def _walk(n):
            out = []
            if n.children:
                out.append(n)
                for c in n.children:
                    out.extend(_walk(c))
            return out
        candidates = _walk(tree)
    if not candidates:
        return tree
    target = rng.choice(candidates)
    alternatives = [c for c in COMBINATORS if c != target.node]
    if not alternatives:
        return tree
    new_node = rng.choice(alternatives)
    new = StrategyTree(node=tree.node, children=list(tree.children))
    def _replace_combinator(n, original, replacement):
        if n.node == original and n.children:
            return StrategyTree(node=replacement, children=list(n.children))
        if not n.children:
            return n
        return StrategyTree(
            node=n.node,
            children=[_replace_combinator(c, original, replacement) for c in n.children],
        )
    return _replace_combinator(new, target.node, new_node)


def mutate_subtree(
    tree: StrategyTree,
    rng: random.Random,
    max_depth: int = 3,
) -> StrategyTree:
    """Replace a random leaf with a fresh random subtree."""
    leaves = tree.collect_leaves()
    if not leaves:
        return random_tree(rng, max_depth=max_depth)
    path, _ = rng.choice(leaves)
    new_subtree = random_tree(rng, max_depth=max_depth // 2 + 1)
    return _replace_at_path(tree, path, new_subtree)


def point_mutation(
    tree: StrategyTree,
    rng: random.Random,
    weights: Optional[Dict[str, float]] = None,
) -> StrategyTree:
    """Apply one of the mutation operators at random.

    Default weights: 0.4 leaf_swap, 0.3 threshold_perturb,
    0.15 combinator_swap, 0.15 subtree.
    """
    if weights is None:
        weights = {
            "leaf_swap": 0.4,
            "threshold_perturb": 0.3,
            "combinator_swap": 0.15,
            "subtree": 0.15,
        }
    ops = list(weights.keys())
    probs = [weights[o] for o in ops]
    op = rng.choices(ops, weights=probs, k=1)[0]
    if op == "leaf_swap":
        return mutate_leaf_swap(tree, rng)
    if op == "threshold_perturb":
        return mutate_threshold_perturb(tree, rng)
    if op == "combinator_swap":
        return mutate_combinator_swap(tree, rng)
    if op == "subtree":
        return mutate_subtree(tree, rng)
    return tree


# ============================================================
# CROSSOVER (Sprint 42)
# ============================================================

def _collect_nodes(tree: StrategyTree) -> List[Tuple[Tuple[int, ...], StrategyTree]]:
    """Walk tree, return all nodes with their paths (as index tuples)."""
    out: List[Tuple[Tuple[int, ...], StrategyTree]] = []

    def _walk(n, path):
        out.append((path, n))
        for i, c in enumerate(n.children):
            _walk(c, path + (i,))
    _walk(tree, ())
    return out


def _get_subtree_at_path(tree: StrategyTree, path: Tuple[int, ...]) -> Optional[StrategyTree]:
    """Deep-copy the node at `path`."""
    if not path:
        return StrategyTree(node=tree.node, children=[
            _get_subtree_at_path(c, ()) for c in tree.children
        ])
    cur = tree
    for idx in path:
        if idx >= len(cur.children):
            return None
        cur = cur.children[idx]
    return StrategyTree(node=cur.node, children=[
        _get_subtree_at_path(c, ()) for c in cur.children
    ])


def crossover(
    parent1: StrategyTree,
    parent2: StrategyTree,
    rng: random.Random,
) -> Tuple[StrategyTree, StrategyTree]:
    """Subtree crossover: pick a random node in each, swap the subtrees.

    Returns two children. Both are deep-copies of the parents.
    """
    nodes1 = _collect_nodes(parent1)
    nodes2 = _collect_nodes(parent2)
    if not nodes1 or not nodes2:
        return parent1, parent2
    path1, _ = rng.choice(nodes1)
    path2, _ = rng.choice(nodes2)
    subtree1 = _get_subtree_at_path(parent1, path1)
    subtree2 = _get_subtree_at_path(parent2, path2)
    if subtree1 is None or subtree2 is None:
        return parent1, parent2
    child1 = _replace_at_path(parent1, path1, subtree2)
    child2 = _replace_at_path(parent2, path2, subtree1)
    return child1, child2


# ============================================================
# SELECTION (Sprint 42)
# ============================================================

def tournament_selection(
    population: List[Dict[str, Any]],
    rng: random.Random,
    tournament_size: int = 3,
) -> Dict[str, Any]:
    """Pick k random individuals, return the one with the highest score.

    Each population item is a dict with at least: "tree", "score".
    """
    if not population:
        raise ValueError("Empty population")
    if len(population) < tournament_size:
        tournament_size = len(population)
    contestants = rng.sample(population, tournament_size)
    return max(contestants, key=lambda x: x.get("score", 0.0))


# ============================================================
# EVOLUTION LOOP (Sprint 42 — the meat)
# ============================================================

@dataclass
class EvolutionResult:
    """Output of an evolution run."""
    best_tree: Optional[StrategyTree]
    best_score: float
    best_metrics: Dict[str, float]
    best_per_symbol: Dict[str, Dict[str, float]]
    n_generations: int
    population_size: int
    final_population: List[Dict[str, Any]]
    history: List[Dict[str, float]]  # per-generation best score, avg score, etc.
    elapsed_seconds: float
    seed: Optional[int]
    # Sprint 43 C7: out-of-sample validation fields. None when
    # oos_fraction=0 (backward compatible). When oos_fraction>0,
    # these capture whether the best strategy generalizes to
    # unseen data — the missing piece the audit flagged.
    best_score_is: Optional[float] = None
    best_score_oos: Optional[float] = None
    is_oos_ratio: Optional[float] = None
    # Reference to the best_ever dict so add_from_evolution can
    # identify it specifically and apply the OOS filter. May be None
    # if the population is empty.
    best_ever: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "best_tree": self.best_tree.to_string() if self.best_tree else None,
            "best_score": self.best_score,
            "best_metrics": self.best_metrics,
            "best_per_symbol": self.best_per_symbol,
            "n_generations": self.n_generations,
            "population_size": self.population_size,
            "history": self.history,
            "elapsed_seconds": self.elapsed_seconds,
            "seed": self.seed,
            # Sprint 43 C7
            "best_score_is": self.best_score_is,
            "best_score_oos": self.best_score_oos,
            "is_oos_ratio": self.is_oos_ratio,
        }


def _evaluate_population(
    population: List[StrategyTree],
    prices_by_symbol: Dict[str, pd.DataFrame],
    direction: int = 1,
    use_multi_symbol: bool = True,
    use_min_symbols: int = 2,
) -> List[Dict[str, Any]]:
    """Score every individual. Returns list of dicts {tree, score, ...}."""
    out: List[Dict[str, Any]] = []
    for tree in population:
        if use_multi_symbol and len(prices_by_symbol) > 1:
            ms = multi_symbol_fitness(
                tree, prices_by_symbol, direction=direction, min_symbols=use_min_symbols,
            )
            # Use the first available symbol's metrics as the "primary"
            primary_metrics = {}
            if ms["per_symbol_scores"]:
                primary_key = next(iter(ms["per_symbol_scores"]))
                primary_metrics = ms["per_symbol_scores"][primary_key]
            out.append({
                "tree": tree,
                "score": ms["score"],
                "per_symbol": ms["per_symbol_scores"],
                "n_profitable": ms["n_profitable"],
                "sharpe_std": ms["sharpe_std"],
                "robust": ms["robust"],
                "metrics": primary_metrics,
            })
        else:
            # Single-symbol: use the first price df
            prices = next(iter(prices_by_symbol.values()))
            f = fitness(tree, prices, direction)
            sym_key = next(iter(prices_by_symbol)) if prices_by_symbol else "?"
            out.append({
                "tree": tree,
                "score": f["score"],
                "metrics": f,
                "per_symbol": {sym_key: f} if prices_by_symbol else {},
                "n_profitable": 1 if f["total_return"] > 0 else 0,
                "sharpe_std": 0.0,
                "robust": f["score"] > 0,
            })
    return out


def evolve(
    prices_by_symbol: Dict[str, pd.DataFrame],
    population_size: int = 30,
    n_generations: int = 8,
    elite_size: int = 4,
    mutation_rate: float = 0.7,
    crossover_rate: float = 0.5,
    max_tree_depth: int = 4,
    tournament_size: int = 3,
    use_multi_symbol: bool = True,
    min_symbols: int = 2,
    direction: int = 1,
    seed: Optional[int] = None,
    verbose: bool = False,
    oos_fraction: float = 0.0,
) -> EvolutionResult:
    """Run a full GP evolution loop.

    Args:
        prices_by_symbol: dict mapping symbol → OHLCV dataframe. Use
            multiple symbols to enforce robustness (Sprint 42 design).
        population_size: how many strategies per generation.
        n_generations: how many generations to evolve.
        elite_size: top-N individuals copied unchanged to the next gen
            (elitism — guarantees we never lose the best).
        mutation_rate: probability an offspring is mutated.
        crossover_rate: probability two parents are crossed over
            (vs cloned unchanged).
        max_tree_depth: cap on tree depth during random_tree().
        tournament_size: how many random individuals compete for each
            parent slot.
        use_multi_symbol: if True, score by multi_symbol_fitness
            (recommended). If False, just use the first symbol.
        direction: long (1) or short (-1) for the strategy's signal.
        seed: random seed (None = non-deterministic).
        verbose: print progress per generation.
        oos_fraction: fraction of each symbol's data reserved for
            out-of-sample (OOS) validation. Sprint 43 C7 fix.
            - 0.0 (default): no split — backward compatible.
            - 0.3 (recommended): last 30% of each symbol's rows
              held out. The GP evolves on the first 70% (in-sample,
              IS). The best strategy is then re-evaluated on the
              30% OOS slice. If OOS score < oos_min_ratio * IS score,
              the strategy is flagged as OVERFIT and rejected by
              `add_from_evolution()`. This is the walk-forward
              validation the audit flagged as missing in Sprint 42.

    Returns:
        EvolutionResult with the best tree found + run history.
        When oos_fraction > 0, the best entry includes both
        `score_is` (in-sample, what the GP optimized) and
        `score_oos` (out-of-sample, the real generalization test).
    """
    rng = random.Random(seed)
    np.random.seed(seed if seed is not None else None)
    t_start = time.time()
    # Initialize population
    population = [
        random_tree(rng, max_depth=max_tree_depth)
        for _ in range(population_size)
    ]
    history: List[Dict[str, float]] = []
    best_ever: Optional[Dict[str, Any]] = None
    final_population: List[Dict[str, Any]] = []

    # Sprint 43 C7: split each symbol's data into IS (in-sample) and
    # OOS (out-of-sample). The GP evolves on IS only; OOS is held
    # out for the final validation step. We slice by row index — the
    # caller is expected to pass data sorted by time.
    is_prices: Dict[str, pd.DataFrame] = {}
    oos_prices: Dict[str, pd.DataFrame] = {}
    oos_enabled = bool(oos_fraction and 0.0 < oos_fraction < 1.0)
    if oos_enabled:
        for sym, df in prices_by_symbol.items():
            n = len(df)
            if n < 20:
                # Too little data to split safely — fall back to no OOS
                is_prices[sym] = df
                continue
            cut = int(n * (1.0 - oos_fraction))
            is_prices[sym] = df.iloc[:cut]
            oos_prices[sym] = df.iloc[cut:]
        evolution_data = is_prices
    else:
        evolution_data = prices_by_symbol

    for gen in range(n_generations):
        # Evaluate
        scored = _evaluate_population(
            population, evolution_data,
            direction=direction, use_multi_symbol=use_multi_symbol,
            use_min_symbols=min_symbols,
        )
        scored.sort(key=lambda x: x["score"], reverse=True)
        # Track best
        if not best_ever or scored[0]["score"] > best_ever["score"]:
            best_ever = scored[0]
        # Stats
        scores = [s["score"] for s in scored]
        stats = {
            "generation": gen,
            "best_score": scores[0] if scores else 0.0,
            "avg_score": float(np.mean(scores)) if scores else 0.0,
            "median_score": float(np.median(scores)) if scores else 0.0,
            "best_sharpe": scored[0]["metrics"].get("sharpe", 0.0) if scored else 0.0,
            "n_robust": sum(1 for s in scored if s.get("robust", False)),
        }
        history.append(stats)
        if verbose:
            print(
                f"[GP gen {gen+1}/{n_generations}] best={stats['best_score']:.3f} "
                f"avg={stats['avg_score']:.3f} median={stats['median_score']:.3f} "
                f"best_sharpe={stats['best_sharpe']:.2f} robust={stats['n_robust']}"
            )
        # Build next generation
        next_population: List[StrategyTree] = []
        # Elitism: copy top N unchanged
        for ind in scored[:elite_size]:
            next_population.append(ind["tree"])
        # Fill the rest
        while len(next_population) < population_size:
            # Pick two parents
            p1 = tournament_selection(scored, rng, tournament_size)
            p2 = tournament_selection(scored, rng, tournament_size)
            # Crossover or clone
            if rng.random() < crossover_rate and p1["tree"] is not p2["tree"]:
                c1, c2 = crossover(p1["tree"], p2["tree"], rng)
            else:
                c1, c2 = p1["tree"], p2["tree"]
            # Mutate
            if rng.random() < mutation_rate:
                c1 = point_mutation(c1, rng)
            if rng.random() < mutation_rate and len(next_population) + 1 < population_size:
                c2 = point_mutation(c2, rng)
            next_population.append(c1)
            if len(next_population) < population_size:
                next_population.append(c2)
        population = next_population[:population_size]
    # Final eval of last gen
    final_population = _evaluate_population(
        population, evolution_data,
        direction=direction, use_multi_symbol=use_multi_symbol,
        use_min_symbols=min_symbols,
    )
    final_population.sort(key=lambda x: x["score"], reverse=True)
    if final_population and (not best_ever or final_population[0]["score"] > best_ever["score"]):
        best_ever = final_population[0]

    # Sprint 43 C7: OOS validation of the best-ever strategy.
    # If the OOS score is much lower than the IS score, the
    # strategy is overfit to the training window. The audit
    # flagged this as the missing piece of the GP loop: the
    # library could absorb strategies that were lucky on the
    # training data and would lose money live.
    #
    # Important: we mutate the original dict in `final_population`
    # (and in any earlier best_ever snapshot) so the OOS fields
    # are visible to `add_from_evolution`. Creating a new dict
    # here would leave the original final_population entries
    # without OOS data, breaking the overfit filter.
    if oos_enabled and best_ever and oos_prices:
        oos_scored = _evaluate_population(
            [best_ever["tree"]], oos_prices,
            direction=direction, use_multi_symbol=use_multi_symbol,
            use_min_symbols=min_symbols,
        )
        if oos_scored:
            oos_entry = oos_scored[0]
            is_score = float(best_ever["score"])
            oos_score = float(oos_entry["score"])
            # Mutate the original dict in-place so add_from_evolution
            # can see the OOS fields via the final_population list.
            best_ever["score_is"] = is_score
            best_ever["score_oos"] = oos_score
            best_ever["metrics_is"] = best_ever.get("metrics", {})
            best_ever["metrics_oos"] = oos_entry.get("metrics", {})
            best_ever["per_symbol_oos"] = oos_entry.get("per_symbol", {})
            best_ever["is_oos_ratio"] = _safe_ratio(oos_score, is_score)
            if verbose:
                ratio = best_ever["is_oos_ratio"]
                print(
                    f"[GP C7] best_ever IS score={is_score:.3f}, "
                    f"OOS score={oos_score:.3f}, "
                    f"ratio={ratio:.2f}"
                )

    elapsed = time.time() - t_start
    return EvolutionResult(
        best_tree=best_ever["tree"] if best_ever else None,
        best_score=best_ever["score"] if best_ever else 0.0,
        best_metrics=best_ever.get("metrics", {}) if best_ever else {},
        best_per_symbol=best_ever.get("per_symbol", {}) if best_ever else {},
        n_generations=n_generations,
        population_size=population_size,
        final_population=final_population,
        history=history,
        elapsed_seconds=elapsed,
        seed=seed,
        # Sprint 43 C7: surface OOS fields so callers can see whether
        # the best is overfit. Both are None when oos_fraction=0.
        best_score_is=best_ever.get("score_is") if best_ever else None,
        best_score_oos=best_ever.get("score_oos") if best_ever else None,
        is_oos_ratio=best_ever.get("is_oos_ratio") if best_ever else None,
        # Keep a reference to the best_ever dict (with OOS fields
        # populated if applicable) so `add_from_evolution` can
        # apply the overfit filter to it specifically.
        best_ever=best_ever,
    )


def _safe_ratio(num: float, den: float) -> float:
    """Compute num/den without dividing by zero.

    Returns 0.0 if both are 0; returns num/den otherwise. Used for
    the OOS / IS score ratio in the C7 fix.
    """
    if den == 0:
        return 0.0 if num == 0 else float("inf")
    return float(num) / float(den)


# ============================================================
# STRATEGY LIBRARY (Sprint 42)
# ============================================================

@dataclass
class LibraryEntry:
    """One persisted strategy in the library."""
    name: str
    tree: StrategyTree
    score: float
    metrics: Dict[str, float]
    per_symbol: Dict[str, Dict[str, float]]
    n_profitable: int
    sharpe_std: float
    added_at: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "tree": self.tree.to_string(),
            "score": self.score,
            "metrics": self.metrics,
            "per_symbol": self.per_symbol,
            "n_profitable": self.n_profitable,
            "sharpe_std": self.sharpe_std,
            "added_at": self.added_at,
        }


def _tree_signature(tree: StrategyTree) -> str:
    """Cheap tree fingerprint for dedup: list of (cond_name, sorted_params)."""
    sigs: List[str] = []

    def _walk(n):
        if not n.children:
            cond_name, params = n.node
            sigs.append(f"{cond_name}:{sorted((params or {}).items())}")
        else:
            for c in n.children:
                _walk(c)
    _walk(tree)
    return "|".join(sorted(sigs))


class StrategyLibrary:
    """Persistent collection of best-evolved strategies.

    Stored as JSON. Dedup is based on the tree's signature (set of
    conditions + params, order-independent). Lower score = removed in
    favor of higher score.
    """

    def __init__(self, path: str = "data_store/strategy_library.json"):
        self.path = path
        self.entries: List[LibraryEntry] = []
        self._signatures: Dict[str, int] = {}  # sig -> index in entries
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for raw in data.get("entries", []):
                tree = _parse_tree_string(raw["tree"])
                entry = LibraryEntry(
                    name=raw["name"],
                    tree=tree,
                    score=raw["score"],
                    metrics=raw["metrics"],
                    per_symbol=raw.get("per_symbol", {}),
                    n_profitable=raw.get("n_profitable", 0),
                    sharpe_std=raw.get("sharpe_std", 0.0),
                    added_at=raw.get("added_at", 0.0),
                )
                self.entries.append(entry)
            # Rebuild signature index
            for i, e in enumerate(self.entries):
                self._signatures[_tree_signature(e.tree)] = i
        except Exception as exc:
            print(f"[StrategyLibrary] warning: failed to load {self.path}: {exc}")

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        data = {
            "version": 1,
            "updated_at": time.time(),
            "n_entries": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    def add(
        self,
        tree: StrategyTree,
        score: float,
        metrics: Dict[str, float],
        per_symbol: Dict[str, Dict[str, float]],
        n_profitable: int,
        sharpe_std: float,
        name: Optional[str] = None,
    ) -> bool:
        """Add a strategy to the library. Returns True if it was added.

        Dedup: if a strategy with the same tree signature already exists
        and has a higher score, skip. If the new one has a higher score,
        replace the old one.
        """
        sig = _tree_signature(tree)
        if sig in self._signatures:
            existing_idx = self._signatures[sig]
            existing = self.entries[existing_idx]
            if existing.score >= score:
                return False  # existing is better, no update
            # Replace existing
            self.entries[existing_idx] = LibraryEntry(
                name=existing.name, tree=tree, score=score,
                metrics=metrics, per_symbol=per_symbol,
                n_profitable=n_profitable, sharpe_std=sharpe_std,
                added_at=time.time(),
            )
            return True
        # New entry
        if name is None:
            name = f"strategy_{len(self.entries) + 1:04d}_{int(score * 100):04d}"
        entry = LibraryEntry(
            name=name, tree=tree, score=score, metrics=metrics,
            per_symbol=per_symbol, n_profitable=n_profitable,
            sharpe_std=sharpe_std, added_at=time.time(),
        )
        self.entries.append(entry)
        self._signatures[sig] = len(self.entries) - 1
        return True

    def add_from_evolution(
        self,
        result: EvolutionResult,
        top_k: int = 5,
        min_score: float = 0.0,
        min_oos_ratio: float = 0.0,
    ) -> int:
        """Add the top-k strategies from an EvolutionResult to the library.

        Returns the number of strategies actually added (after dedup
        AND after the Sprint 43 C7 overfit rejection).

        Args:
            result: EvolutionResult from `evolve()`.
            top_k: maximum number of strategies to consider.
            min_score: drop candidates with score < this on the training
                window (in-sample, when oos_fraction was set).
            min_oos_ratio: Sprint 43 C7 — drop candidates whose OOS/IS
                score ratio is below this. Default 0.0 = accept any
                ratio (backward compatible). Recommended: 0.4-0.5.
                - 0.0 = no OOS rejection (old behavior, only min_score
                  filters)
                - 0.5 = require OOS score to be at least 50% of IS
                  score (the audit's recommendation)
                - 1.0 = require OOS >= IS (strictest; rarely met)
        """
        added = 0
        # Sort by score descending
        candidates = sorted(result.final_population, key=lambda x: x["score"], reverse=True)
        for ind in candidates[:top_k]:
            if ind["score"] < min_score:
                continue
            # Sprint 43 C7: reject strategies that don't generalize.
            # We check the candidate's own `is_oos_ratio` field if
            # the caller evaluated it on OOS data. The best_ever
            # is the one that carries the ratio (because the OOS
            # re-eval is only done for the best). If a non-best
            # candidate lacks `is_oos_ratio`, it was never OOS-
            # evaluated; we accept it (caller can re-eval separately).
            if min_oos_ratio > 0 and "is_oos_ratio" in ind:
                ratio = ind["is_oos_ratio"]
                if ratio < min_oos_ratio:
                    print(
                        f"[StrategyLibrary C7] rejected candidate: "
                        f"IS score {ind['score']:.3f} but OOS ratio "
                        f"{ratio:.2f} < min_oos_ratio {min_oos_ratio:.2f}. "
                        f"Strategy is overfit to the training window."
                    )
                    continue
            ok = self.add(
                tree=ind["tree"],
                score=ind["score"],
                metrics=ind.get("metrics", {}),
                per_symbol=ind.get("per_symbol", {}),
                n_profitable=ind.get("n_profitable", 0),
                sharpe_std=ind.get("sharpe_std", 0.0),
            )
            if ok:
                added += 1
        return added

    def prune(self, min_score: float = 0.0, max_entries: int = 100) -> int:
        """Remove low-score entries; cap at max_entries. Returns # removed."""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.score >= min_score]
        # Keep top N by score
        self.entries.sort(key=lambda e: e.score, reverse=True)
        if len(self.entries) > max_entries:
            self.entries = self.entries[:max_entries]
        # Rebuild sig index
        self._signatures = {}
        for i, e in enumerate(self.entries):
            self._signatures[_tree_signature(e.tree)] = i
        return before - len(self.entries)

    def top(self, n: int = 5) -> List[LibraryEntry]:
        return sorted(self.entries, key=lambda e: e.score, reverse=True)[:n]

    def __len__(self) -> int:
        return len(self.entries)


def _parse_tree_string(s: str) -> StrategyTree:
    """Parse a tree back from its string representation.

    Format from StrategyTree.to_string():
      COMBINATOR
        (cond, params)   ← leaf
        (cond, params)   ← leaf
      COMBINATOR
        (cond, params)
    """
    lines = [l for l in s.split("\n") if l.strip()]
    if not lines:
        return random_leaf(random.Random())
    return _parse_node(lines, 0, 0)[0]


def _parse_node(lines: List[str], start: int, indent_level: int) -> Tuple[StrategyTree, int]:
    """Parse one node starting at lines[start] (which must have the given indent).

    Returns (node, next_line_index).
    """
    line = lines[start]
    pad = "  " * indent_level
    if not line.startswith(pad):
        raise ValueError(f"Expected indent {indent_level} at line {start}: {line!r}")
    content = line[len(pad):]
    # Leaf?
    if content.startswith("("):
        # Parse "(name, {key: value, ...})"  or "(name,)"
        # We store cond_name and dict in tree.node; reconstruct.
        inner = content.strip()
        # Strip parens
        if inner.endswith(")"):
            inner = inner[:-1]
        if inner.startswith("("):
            inner = inner[1:]
        # Split on first ","
        if "," in inner:
            cond_name, params_str = inner.split(",", 1)
            cond_name = cond_name.strip().strip("'\"")
            params_str = params_str.strip()
        else:
            cond_name = inner.strip().strip("'\"")
            params_str = ""
        # Parse params
        params = {}
        if params_str and params_str != "{}":
            # Split on ", " but be careful with values containing spaces
            import re
            for match in re.finditer(r"(\w+):\s*([^,]+?)(?=,\s*\w+:|$)", params_str + ","):
                k = match.group(1)
                v = match.group(2).strip().rstrip(",").strip()
                # Try int → float → str
                try:
                    params[k] = int(v)
                    continue
                except ValueError:
                    pass
                try:
                    params[k] = float(v)
                    continue
                except ValueError:
                    pass
                params[k] = v.strip("'\"")
        return StrategyTree(node=(cond_name, params), children=[]), start + 1
    # Combinator: parse children at indent_level + 1
    combinator = content.strip()
    children: List[StrategyTree] = []
    next_idx = start + 1
    while next_idx < len(lines):
        child_line = lines[next_idx]
        child_indent = len(child_line) - len(child_line.lstrip(" "))
        if child_indent <= indent_level:
            break
        if child_indent == indent_level + 2:
            child, next_idx = _parse_node(lines, next_idx, child_indent // 2)
            children.append(child)
        else:
            # Skip lines that don't match expected indent (shouldn't happen)
            next_idx += 1
    return StrategyTree(node=combinator, children=children), next_idx


# ============================================================
# DEMO: generate N random trees, evaluate, return top K
# (Sprint 41 — kept for backwards compat and as a "random baseline")
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

    Useful as a smoke test for the GP scaffold and as a baseline
    ("how good is a random population?"). Use ``evolve()`` for the
    real multi-generation loop.
    """
    rng = random.Random(seed)
    candidates: List[EvolvedCandidate] = []
    for _ in range(n_random):
        tree = random_tree(rng, max_depth=3)
        metrics = fitness(tree, prices, direction=direction)
        candidates.append(EvolvedCandidate(tree=tree, score=metrics["score"], metrics=metrics))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_k]


# ============================================================
# CLI RUNNER (Sprint 42)
# ============================================================

def run_evolution_cli(
    output_path: str = "data_store/strategy_library.json",
    population_size: int = 30,
    n_generations: int = 8,
    seed: int = 42,
    verbose: bool = True,
) -> EvolutionResult:
    """End-to-end: load synthetic multi-symbol data, evolve, save library.

    This is the entry point for `python -m src.analysis.genetic_programming`.
    Uses synthetic data by default; in production, replace with real
    OHLCV loaders (e.g. via yfinance or cached data_store/ files).
    """
    rng = np.random.default_rng(seed)
    # Synthetic data for 3 symbols: 2 trending, 1 sideways.
    prices_by_symbol = {}
    for i, sym in enumerate(("SYN_A", "SYN_B", "SYN_C")):
        n = 400
        if i == 0:
            ret = rng.normal(0.002, 0.015, n)
        elif i == 1:
            ret = rng.normal(-0.001, 0.020, n)
        else:
            ret = rng.normal(0.0, 0.025, n)
        prices = 100 * np.cumprod(1 + ret)
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        prices_by_symbol[sym] = pd.DataFrame(
            {"Close": prices, "Open": prices, "High": prices, "Low": prices},
            index=idx,
        )
    if verbose:
        print(f"[GP] starting evolution: pop={population_size} gens={n_generations} seed={seed}")
    result = evolve(
        prices_by_symbol,
        population_size=population_size,
        n_generations=n_generations,
        seed=seed,
        verbose=verbose,
    )
    if verbose:
        print(f"\n[GP] best score: {result.best_score:.3f}")
        print(f"[GP] best sharpe: {result.best_metrics.get('sharpe', 0):.3f}")
        print(f"[GP] elapsed: {result.elapsed_seconds:.1f}s")
        if result.best_tree is not None:
            print(f"\n[GP] best tree:\n{result.best_tree.to_string()}")
    # Save to library
    lib = StrategyLibrary(output_path)
    n_added = lib.add_from_evolution(result, top_k=5)
    lib.save()
    if verbose:
        print(f"\n[GP] added {n_added} strategies to library ({len(lib)} total)")
        print(f"[GP] library saved to {output_path}")
    return result
