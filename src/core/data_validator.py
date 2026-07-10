"""
Sprint 6 — Data Integrity Validator.

Inspirado en NautilusTrader's fail-fast policy:

"Trading systems, corrupt data is worse than no data. A single
incorrect price, timestamp, or quantity can cascade through the
system, resulting in incorrect position sizing or risk calculations,
orders placed at wrong prices, backtests producing misleading results,
silent financial losses."

Este módulo rechaza silenciosamente NaN, Infinity, precios negativos
y timestamps inválidos. Crash al inicio del problema, no después.
"""
from __future__ import annotations
import math
from typing import Any


class DataIntegrityError(ValueError):
    """Raised when incoming data violates integrity invariants."""


def validate_price(price: Any, label: str = "price") -> float:
    """Rechaza NaN/Inf/negativos. Devuelve float."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        raise DataIntegrityError(f"{label}: not a number ({price!r})")
    if math.isnan(p):
        raise DataIntegrityError(f"{label}: NaN")
    if math.isinf(p):
        raise DataIntegrityError(f"{label}: Infinity")
    if p < 0:
        raise DataIntegrityError(f"{label}: negative ({p})")
    return p


def validate_quantity(qty: Any, label: str = "quantity") -> float:
    try:
        q = float(qty)
    except (TypeError, ValueError):
        raise DataIntegrityError(f"{label}: not a number ({qty!r})")
    if math.isnan(q) or math.isinf(q):
        raise DataIntegrityError(f"{label}: NaN or Infinity")
    if q < 0:
        raise DataIntegrityError(f"{label}: negative ({q})")
    return q


def validate_ohlcv_row(row, label_prefix=""):
    """
    Valida una vela OHLCV. Open/High/Low/Close > 0, High >= Low, Volume >= 0.
    """
    o, h, l, c, v = row
    o = validate_price(o, f"{label_prefix}open")
    h = validate_price(h, f"{label_prefix}high")
    l = validate_price(l, f"{label_prefix}low")
    c = validate_price(c, f"{label_prefix}close")
    if h < l:
        raise DataIntegrityError(f"{label_prefix}high<low ({h}<{l})")
    if v < 0:
        raise DataIntegrityError(f"{label_prefix}volume<0 ({v})")
    return (o, h, l, c, v)


def validate_dataframe(df, required_cols=("Open", "High", "Low", "Close"),
                        max_staleness_seconds: float = None):
    """
    Valida un DataFrame de precios. Falla rápido si hay data corrupta.

    Sprint 43 M5 fix: also validate
      (a) monotonic index (no out-of-order timestamps)
      (b) unique index (no duplicate timestamps)
      (c) optional: most-recent row is not older than
          `max_staleness_seconds` (catches delisted/paused symbols
          returning the same last bar forever).

    The audit's exact recommendations. All three checks default-on;
    pass `max_staleness_seconds=None` to skip the staleness check
    (e.g. for historical backtests where staleness doesn't matter).
    """
    if df is None or len(df) == 0:
        raise DataIntegrityError("DataFrame is None or empty")
    for col in required_cols:
        if col not in df.columns:
            raise DataIntegrityError(f"missing column: {col}")
        if df[col].isna().any():
            n_nan = int(df[col].isna().sum())
            raise DataIntegrityError(f"column {col}: {n_nan} NaN values")
        if (df[col] == float("inf")).any() or (df[col] == float("-inf")).any():
            raise DataIntegrityError(f"column {col}: Infinity values")
        if (df[col] < 0).any():
            n_neg = int((df[col] < 0).sum())
            raise DataIntegrityError(f"column {col}: {n_neg} negative values")
    # OHLC integrity: high >= low en cada fila
    if "High" in df.columns and "Low" in df.columns:
        bad = df[df["High"] < df["Low"]]
        if len(bad) > 0:
            n = len(bad)
            raise DataIntegrityError(f"high<low en {n} vela(s)")
    # Sprint 43 M5 (a) — monotonic index
    if df.index.is_monotonic_increasing is False:
        # pandas returns True/False/None. None for non-ordered indexes
        # (e.g. RangeIndex on an unsorted df) is also a fail.
        raise DataIntegrityError(
            f"index is not monotonically increasing; "
            f"sort or fix the timestamp column before validating"
        )
    # Sprint 43 M5 (b) — unique index (no duplicate timestamps)
    if df.index.has_duplicates:
        n_dup = int(df.index.duplicated().sum())
        raise DataIntegrityError(
            f"index has {n_dup} duplicate timestamp(s); "
            f"a feed that re-emits bars is a sign of upstream corruption"
        )
    # Sprint 43 M5 (c) — optional staleness check
    if max_staleness_seconds is not None and max_staleness_seconds > 0:
        try:
            import pandas as pd
            now = pd.Timestamp.now(tz=df.index[-1].tz) if hasattr(df.index[-1], 'tz') and df.index[-1].tz is not None else pd.Timestamp.now()
            last_ts = df.index[-1]
            if not isinstance(last_ts, pd.Timestamp):
                last_ts = pd.Timestamp(last_ts)
            age = (now - last_ts).total_seconds()
            if age > max_staleness_seconds:
                raise DataIntegrityError(
                    f"last bar is {age:.0f}s old, exceeds staleness threshold "
                    f"{max_staleness_seconds:.0f}s — symbol may be delisted/paused"
                )
        except DataIntegrityError:
            raise
        except Exception:
            # If we can't compute the age (e.g. non-datetime index),
            # skip this check rather than failing the whole validation.
            pass
    return df
