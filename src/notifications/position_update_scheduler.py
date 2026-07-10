"""
Sprint 34 — PositionUpdateScheduler.

Emits a POSITION_UPDATE event for each open position on a per-position
cadence (default 60 minutes). The scheduler tracks the last update ts
per position_id in memory, so:
  - first call after a position opens: emits immediately (so Carlos
    gets a "30 min in" update that wasn't buffered)
  - subsequent calls: emit only if `interval_seconds` have elapsed since
    the last update for THAT specific position
  - when a position closes: caller should call `clear_position(id)` to
    free the entry

Why per-position (not global): if position A is 1h old and position B
just opened, we don't want B's "first hourly update" to wait until A
emits again. Each position has its own clock.

Wire-in: main.py job_with_monitor calls `tick(current_prices)` after
the position monitor + equity tracker pass. `tick` is cheap — O(N open
positions) dict lookups.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional


class PositionUpdateScheduler:
    def __init__(
        self,
        position_repo,
        event_bus,
        interval_minutes: int = 60,
        min_pnl_usd: float = 0.0,
        on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """
        Args:
            position_repo: PositionRepository instance
            event_bus: EventBus (publishes POSITION_UPDATE)
            interval_minutes: cadence per position (default 60)
            min_pnl_usd: skip updates where |unrealized P&L| is below this
            on_update: optional direct callback (in addition to event_bus).
                       Useful for tests or alternate transports.
        """
        self.repo = position_repo
        self.event_bus = event_bus
        self.interval_seconds = max(0, int(interval_minutes * 60))
        self.min_pnl_usd = float(min_pnl_usd)
        self.on_update = on_update
        # position_id -> last update ts (in-memory; reset on bot restart)
        self._last_update: Dict[str, float] = {}
        # position_id -> first ever update ts (used for "duration_hours" math
        # even if the bot restarts mid-position)
        self._first_seen: Dict[str, float] = {}

    def tick(self, current_prices: Dict[str, float]) -> int:
        """
        Run one pass of the scheduler.

        Args:
            current_prices: {asset: price} from the most recent market data.

        Returns:
            Number of POSITION_UPDATE events emitted this tick.
        """
        if self.interval_seconds <= 0:
            return 0  # disabled

        now = time.time()
        emitted = 0

        for pos in self.repo.open():
            pid = pos.position_id
            price = current_prices.get(pos.asset)
            if price is None:
                # Try common symbol variants
                for variant in self._symbol_variants(pos.asset):
                    if variant in current_prices:
                        price = current_prices[variant]
                        break
            if price is None:
                continue  # no price this cycle; skip silently

            last_ts = self._last_update.get(pid, 0.0)
            if (now - last_ts) < self.interval_seconds:
                continue  # not yet time for this position's update

            upnl = pos.unrealized_pnl(price)
            if abs(upnl) < self.min_pnl_usd:
                # Below the dust threshold — skip the publish but DON'T
                # advance the cadence clock. We want the next tick to
                # re-evaluate (e.g. if the price recovers enough to
                # cross the threshold, we should emit immediately rather
                # than wait `interval_minutes`). The check itself is O(1)
                # so re-evaluating every cycle is fine.
                continue

            notional = pos.notional_usd
            pnl_pct = (upnl / notional * 100.0) if notional > 0 else 0.0
            duration_h = (now - pos.entry_ts) / 3600.0

            payload = {
                "position_id": pid,
                "asset": pos.asset,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "current_price": price,
                "qty": pos.qty,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "unrealized_pnl_usd": round(upnl, 4),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "duration_hours": round(duration_h, 1),
                "notional_usd": notional,
            }

            if self.event_bus is not None:
                self.event_bus.publish("POSITION_UPDATE", payload)
            if self.on_update is not None:
                self.on_update(payload)

            self._last_update[pid] = now
            self._first_seen.setdefault(pid, now)
            emitted += 1

        return emitted

    def clear_position(self, position_id: str) -> None:
        """Call when a position closes so the entry is freed."""
        self._last_update.pop(position_id, None)
        self._first_seen.pop(position_id, None)

    def clear_all(self) -> None:
        """Reset all per-position cadence state (e.g. on mode switch)."""
        self._last_update.clear()
        self._first_seen.clear()

    @staticmethod
    def _symbol_variants(asset: str) -> list:
        """Return likely alt-symbol keys for a given asset string.

        Examples:
          "BTC-USD"  -> ["BTC/USD", "BTCUSD", "BTCUSDT"]
          "BTC/USDT" -> ["BTC-USDT", "BTCUSDT", "BTCUSD"]
          "BTCUSD"   -> ["BTC/USD", "BTC-USDT", "BTC-USDT"]
        """
        variants = []
        if "/" in asset:
            variants.append(asset.replace("/", "-"))
            variants.append(asset.replace("/", ""))
        if "-" in asset:
            variants.append(asset.replace("-", "/"))
            variants.append(asset.replace("-", ""))
        # Common USD/USDT suffix variations
        for v in list(variants):
            if v.endswith("USD") and not v.endswith("USDT"):
                variants.append(v + "T")
            if v.endswith("USDT") and not v.endswith("USD"):
                variants.append(v[:-1])
        # De-dup while preserving order
        seen = set()
        out = []
        for v in variants:
            if v not in seen and v != asset:
                seen.add(v)
                out.append(v)
        return out
