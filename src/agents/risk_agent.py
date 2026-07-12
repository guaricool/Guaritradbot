"""
Sprint 0+1+2+18+44A — RiskManagerAgent.

Sprint 0 fixes: ATR-based stops, qty = risk/distance, min order check.
Sprint 1: Mandate Gate + audit ledger integration.
Sprint 2: Take profit ATR-based, max_open_trades respetado, persiste
posición abierta en el PositionRepository.
Sprint 18 (Audit fix): auto-adjust cuando notional < min_order (no solo
max_notional < min_order). Esto permite operar cuentas pequeñas donde
1% de riesgo + stop ATR produce notional < $10 (Binance min).
Sprint 18 (Portfolio mgmt): position replacement — si max_open_trades
está lleno pero aparece una señal MUY superior, cerrar la peor posición
y abrir la nueva. Score = combinación de expected value + momentum.
Sprint 44A (Bridgewater risk): asset-class concentration check. Antes
de aprobar un trade, simular la exposure post-add y rechazar si una
clase (crypto / equity_growth / commodity) supera el cap. Cierra el
gap entre "5 posiciones abiertas" y "5 bets independientes".
"""
import os
import math
from typing import Dict, Any, List, Optional, Tuple

from src.data_store.positions import Position, PositionRepository
from src.data.asset_class import get_asset_class, AssetClass
from src.execution.broker_routing import (
    build_asset_to_class_map,
    is_mandate_enabled,
    resolve_broker_for_close,
    send_close_order,
)
from src.data.asset_allocation import (
    AllocationPolicy,
    DEFAULT_POLICY,
    check_trade_against_policy,
)
# Sprint 45: wire the Sprint 44 portfolio-risk analytics (correlation,
# recession stress test, CVaR) into real pre-trade gates. Previously
# these were fully built and tested but never consulted by any live
# decision — the second audit flagged this as "shelf-ware". All three
# are best-effort: a data outage or unexpected exception ALLOWS the
# trade (with the specific reason logged) rather than blocking
# trading entirely on an infra hiccup. Only a CONFIRMED bad signal
# (high correlation, breached stress drawdown, breached CVaR) rejects.
from src.analysis.asset_correlation import analyze_assets
from src.analysis.stress_test import stress_portfolio_all_scenarios, worst_case_drawdown
from src.analysis.tail_risk import compute_portfolio_tail_risk


class RiskManagerAgent:
    def __init__(
        self,
        broker_client=None,
        risk_per_trade_pct: float = 1.0,
        max_capital_per_trade_pct: float = 10.0,
        atr_stop_multiplier: float = 2.0,
        atr_take_profit_multiplier: float = 4.0,
        risk_reward_ratio: float = 2.0,
        max_open_trades: int = 5,
        min_order_usd: float = 10.0,
        event_bus=None,
        mandate_gate=None,
        audit=None,
        position_repo=None,
        enable_position_replacement: bool = True,
        replacement_score_threshold: float = 0.20,
        current_prices: Optional[Dict[str, float]] = None,
        # Sprint 44A: asset-class concentration gate (Bridgewater risk)
        asset_concentration_check: bool = True,
        max_asset_class_concentration_pct: float = 60.0,
        # Sprint 44B: allocation policy drift gate (BlackRock #1)
        allocation_policy: Optional[AllocationPolicy] = None,
        # Sprint 45: portfolio-risk gates (previously-unwired Sprint 44
        # analytics). All default ON; each degrades to "allow" on
        # missing data/errors rather than blocking trading.
        portfolio_stress_check: bool = True,
        # Sprint 45 default chosen so a single concentrated crypto
        # position (BTC/ETH alone can show ~-64% under the 2022 rate-
        # hike scenario) doesn't get rejected outright -- that's
        # normal, expected volatility for this bot's asset mix, not
        # a portfolio-wipeout risk. 70% still catches genuinely
        # extreme concentration (e.g. all-in on commodity_energy,
        # -78% under 2008 GFC).
        max_stress_drawdown_pct: float = 70.0,
        correlation_check_enabled: bool = True,
        max_avg_correlation_pct: float = 75.0,
        tail_risk_check_enabled: bool = True,
        max_cvar_95_pct: float = 20.0,
        # Sprint 46M: binance.us SPOT has no margin/borrow, so a "short"
        # signal on crypto was never a real exchange short — it's a
        # plain sell order that only works by accident (if the account
        # happens to already hold that asset). Default OFF; see
        # config.yaml's comment for the live incident that surfaced this.
        allow_crypto_short: bool = False,
        # Sprint 46N (audit M3): Alpaca cannot open a short position
        # via notional/fractional orders (this bot's equity sizing
        # always uses notional_usd) -- an equity "short" hypothesis
        # simulates fine in paper mode and then fails outright in
        # live. Default OFF, mirroring allow_crypto_short above. See
        # config.yaml's allow_equity_short comment.
        # Sprint 46R (audit B1 + B2): the SL/TP minimum-distance
        # floor as a percent of entry price. Pre-46R this was the
        # hard-coded `entry_price * 0.005` (B1 fix). Now exposed
        # as constructor kwargs so config.yaml's
        # `min_sl_floor_pct` / `min_tp_floor_pct` flow through.
        # Defaults preserve the B1 fix behavior (0.5% on both
        # sides). The audit's B2 complaint: "numeros magicos fuera
        # de config: ... entry_price * 0.005".
        min_sl_floor_pct: float = 0.005,
        min_tp_floor_pct: float = 0.005,
        allow_equity_short: bool = False,
        # Sprint 46M: reject a new trade if an OPEN position already
        # exists on the same asset (any direction). This is what let the
        # bot open a simultaneous BTC-USD long + short every cycle,
        # committing the whole account and producing unclosable pairs.
        block_conflicting_asset_positions: bool = True,
        # Sprint 46N (audit C1/C2): route position-REPLACEMENT closes by
        # asset class (crypto → broker_client, equity → alpaca_broker)
        # instead of always calling broker_client, and never place a
        # real order while in paper mode. See broker_routing.py.
        alpaca_broker=None,
        brokers_config: Optional[dict] = None,
        mode_override_path: str = "audit/mode_override.json",
        # Sprint 46N (audit A2): the min_order_usd auto-adjust (below,
        # Sprint 18) inflates a risk-sized trade up to min_order_usd
        # whenever the raw risk/stop_distance notional would be smaller
        # — but it never checked how much that inflation multiplies the
        # EFFECTIVE risk (quantity * stop_distance) relative to what
        # risk_per_trade_pct actually intended. With a $20 account (1%
        # risk = $0.20) and a typical crypto stop of 4-10% (2x ATR),
        # bumping to the $10 min_order_usd floor makes effective risk
        # $0.40-$1.00 per trade — 2-5x the configured risk, silently,
        # with only an audit-log breadcrumb (CAP_AUTO_ADJUSTED) and no
        # enforcement. See CAP_AUTO_ADJUSTED_REJECTED below.
        max_auto_adjust_risk_multiplier: float = 2.0,
        # Sprint 46N (audit M2): position-REPLACEMENT closes
        # (_try_replace_position below) previously called
        # position_repo.close_position() with no fee_pct, recording
        # gross P&L for the closed position -- unlike PositionMonitor's
        # SL/TP and smart-profit-take closes, which have been fee-aware
        # since Sprint 46J. Optional callable, same contract as
        # PositionMonitor's `fee_pct_for_asset`: `f(asset: str) -> float`
        # (fraction, e.g. 0.001 = 0.1%). Wired in main.py from the same
        # `_fee_pct_for_asset` closure PositionMonitor already uses.
        # None (the default) preserves the old fee-free behavior.
        fee_pct_for_asset=None,
    ):
        self.broker = broker_client
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_capital_per_trade_pct = max_capital_per_trade_pct
        self.atr_stop_multiplier = atr_stop_multiplier
        self.atr_take_profit_multiplier = atr_take_profit_multiplier
        # Sprint 46R audit B2: SL/TP minimum-distance floor as
        # percent of entry (the B1 fix's 0.005). Defaults
        # preserve the pre-46R hard-coded behavior; config.yaml
        # can tighten/loosen.
        self.min_sl_floor_pct = min_sl_floor_pct
        self.min_tp_floor_pct = min_tp_floor_pct
        self.risk_reward_ratio = risk_reward_ratio
        self.max_open_trades = max_open_trades
        self.min_order_usd = min_order_usd
        self.event_bus = event_bus
        self.mandate = mandate_gate
        self.audit = audit
        self.position_repo = position_repo
        # Sprint 18: portfolio management
        self.enable_position_replacement = enable_position_replacement
        # New hypothesis must score at least +20% above the worst open position
        self.replacement_score_threshold = replacement_score_threshold
        # Used by position-replacement scoring (passed per-cycle from main loop)
        self.current_prices = current_prices or {}
        # Sprint 44A: sector concentration gate. Configurable so you can
        # dial it down (e.g. 40%) for a stricter portfolio or up (e.g.
        # 80%) to allow a more concentrated high-conviction book.
        self.asset_concentration_check = asset_concentration_check
        self.max_asset_class_concentration_pct = max_asset_class_concentration_pct
        # Sprint 44B: allocation policy drift gate. None → use the
        # default policy (or the policy disabled if explicitly set).
        # Pass `AllocationPolicy(enabled=False)` to disable.
        self.allocation_policy = allocation_policy if allocation_policy is not None else DEFAULT_POLICY
        # Sprint 45: portfolio-risk gates.
        self.portfolio_stress_check = portfolio_stress_check
        self.max_stress_drawdown_pct = max_stress_drawdown_pct
        self.correlation_check_enabled = correlation_check_enabled
        self.max_avg_correlation_pct = max_avg_correlation_pct
        self.tail_risk_check_enabled = tail_risk_check_enabled
        self.max_cvar_95_pct = max_cvar_95_pct
        # Sprint 46M.
        self.allow_crypto_short = allow_crypto_short
        # Sprint 46N (audit M3).
        self.allow_equity_short = allow_equity_short
        self.block_conflicting_asset_positions = block_conflicting_asset_positions
        # Sprint 46N.
        self.alpaca_broker = alpaca_broker
        self.brokers_config = brokers_config or {}
        self.mode_override_path = mode_override_path
        self._asset_to_class = build_asset_to_class_map(self.brokers_config)
        # Sprint 46N (audit A2).
        self.max_auto_adjust_risk_multiplier = max_auto_adjust_risk_multiplier
        # Sprint 46N (audit M2).
        self.fee_pct_for_asset = fee_pct_for_asset

    def _fee_pct(self, asset: str) -> float:
        """Same defensive contract as PositionMonitor._fee_pct: a
        missing callable or an exception inside it must never block a
        position replacement -- it just means the close is recorded
        fee-free (the pre-M2 behavior), not that the trade is aborted.
        """
        if self.fee_pct_for_asset is None:
            return 0.0
        try:
            return float(self.fee_pct_for_asset(asset) or 0.0)
        except Exception:
            return 0.0

    def get_account_balance(self, asset: Optional[str] = None) -> tuple:
        """Return (balance_usd, source) to size a trade against.

        Sprint 46N (audit A4) — two fixes bundled here:

        1. Per-asset-class balance lookup: previously this always read
           `self.broker` (the crypto/binance.us client), even when
           sizing an equity hypothesis (SPY/QQQ/GLD/USO) — so an SPY
           trade was sized against the binance.us USDT balance, not
           Alpaca's actual buying power. Now, when `asset` is given,
           the SAME asset→broker routing table used for closes/
           replacements (`broker_routing.resolve_broker_for_close`) is
           used to pick `self.alpaca_broker` (equity) vs `self.broker`
           (crypto/unknown, matching the pre-46N default). Omitting
           `asset` preserves the exact old behavior: always resolve to
           `self.broker`.
        2. The $100 simulated-balance fallback (`no_broker_sim` when no
           broker is configured, or the `GUARICO_ALLOW_SIMULATED_BALANCE`
           gated fallback when a fetch fails/returns non-finite) is now
           NEVER used while the bot is in LIVE mode
           (`is_mandate_enabled(self.mode_override_path)` True) — both
           paths raise instead. Before this fix, neither fallback path
           checked live-vs-paper at all: a broker outage in LIVE mode
           would silently size real orders against a fabricated $100
           balance. The simulated fallback remains available in PAPER
           mode only, where it exists to let the bot run in dev/demo
           setups with no broker credentials configured at all.
        """
        live = is_mandate_enabled(self.mode_override_path)
        if asset is not None:
            broker, asset_class = resolve_broker_for_close(
                asset, self._asset_to_class, self.broker, self.alpaca_broker,
            )
        else:
            broker, asset_class = self.broker, "crypto"

        if broker is None:
            if live:
                raise RuntimeError(
                    f"No hay broker configurado para asset_class={asset_class!r} "
                    f"(asset={asset!r}) mientras el bot está en modo LIVE -- "
                    f"rechazando en vez de simular un balance de $100 contra "
                    f"una cuenta real (audit A4)."
                )
            return (100.0, "no_broker_sim")

        try:
            # Sprint 46N (audit A4): equity balance comes from Alpaca's
            # USD cash (`get_usd_balance`), never from the crypto
            # broker's USDT balance.
            bal = broker.get_usd_balance() if asset_class == "equity" else broker.get_usdt_balance()
            # Sprint 43 C3 fix: defend against NaN/Inf in the broker's
            # balance response. Some ccxt error paths (e.g. transient
            # network, sandbox returning None coerced to NaN) can return
            # non-finite numbers WITHOUT raising. Treating NaN as 0 (or
            # letting it propagate) silently inflates risk calculations
            # and can fail-open downstream comparisons in mandate_gate.
            if not math.isfinite(float(bal)):
                raise ValueError(f"non_finite_balance:{bal!r}")
            sandbox = False
            if hasattr(broker, "exchange"):
                try:
                    sandbox = bool(broker.exchange.options.get("sandboxMode", False))
                except Exception:
                    sandbox = False
            return (float(bal), "testnet_sim" if sandbox else "live")
        except Exception as e:
            if live:
                raise RuntimeError(
                    f"Balance no disponible para asset_class={asset_class!r} "
                    f"({e}) mientras el bot está en modo LIVE -- simulación "
                    f"deshabilitada por seguridad, incluso si "
                    f"GUARICO_ALLOW_SIMULATED_BALANCE=1 (audit A4: nunca "
                    f"fingir un balance contra una cuenta real)."
                ) from e
            if os.getenv("GUARICO_ALLOW_SIMULATED_BALANCE", "1") == "1":
                print(f"[RiskManagerAgent] ⚠️ Balance indisponible ({e}). SIMULATED fallback $100.")
                return (100.0, "testnet_sim")
            raise RuntimeError("Balance no disponible y simulación deshabilitada") from e

    def validate_and_size(self, inputs: dict, state: dict) -> Dict[str, Any]:
        # Sprint 3: si DebateAgent corrió, usa SOLO las hipótesis que
        # pasaron el debate. Si no hay debate, fallback al total
        # (retrocompatible con versiones sin Sprint 3).
        if "debate_hypotheses" in state:
            debate_result = state["debate_hypotheses"]
            approved = debate_result.get("approved_hypotheses", [])
            all_hyps = debate_result.get("hypotheses", [])
            # Sprint 11 fix: solo usar approved si el debate realmente
            # ejecutó (no [] que es fallback falsy). Si approved es []
            # Y hay all_hyps, fue "ninguna aprobada" → usar [] también,
            # no todas (antes hacía fallback a todas).
            if approved:
                hypotheses = approved
            elif all_hyps and "verdicts" in debate_result and debate_result["verdicts"]:
                # Debate corrió y rechazó todas → respeta la decisión
                hypotheses = []
            else:
                # No hay debate (caso legacy) → usa todas
                hypotheses = all_hyps
            print(f"[RiskManagerAgent] usando {len(hypotheses)} hipótesis post-debate")
        else:
            hypotheses = state.get("generate_hypotheses", {}).get("hypotheses", [])
        # Sprint 46N (audit A4): this "headline" balance (crypto broker,
        # or the no-broker/no-mode-aware fallback) is used ONLY for the
        # per-cycle log line below and to seed the per-asset-class cache
        # so a crypto hypothesis doesn't refetch it. Actual per-trade
        # sizing below now looks up the balance for THAT hypothesis's
        # asset class (see `balance_cache` / `get_account_balance(asset=...)`),
        # not this single aggregate.
        cycle_balance, cycle_balance_source = self.get_account_balance()
        balance_cache: Dict[str, Tuple[float, str]] = {"crypto": (cycle_balance, cycle_balance_source)}

        # Sprint 46N (audit A5): per-cycle returns cache for the
        # portfolio-risk gates below (_check_portfolio_correlation,
        # _check_portfolio_tail_risk). Before this, EACH hypothesis in
        # this same validate_and_size() call re-fetched yfinance data
        # from scratch for every asset in the existing open book (N
        # hypotheses x M open-book assets = N*M yfinance calls for
        # data that can't have changed within one cycle) — the audit's
        # A5 finding, part of why a slow hourly cycle could run
        # 10-20 minutes and starve fast_monitor_tick. Two separate
        # dicts because analyze_assets (correlation, 90d window) and
        # compute_portfolio_tail_risk (CVaR, 180d window) use different
        # lookback windows — sharing one cache between them would
        # silently serve a wrong-length series to whichever ran second.
        self._cycle_correlation_returns_cache: Dict[str, Any] = {}
        self._cycle_tail_risk_returns_cache: Dict[str, Any] = {}

        # Sprint 2: cuántas posiciones abiertas ya tenemos
        open_count = self.position_repo.count_open() if self.position_repo else 0
        slots_left = max(0, self.max_open_trades - open_count)
        print(
            f"[RiskManagerAgent] {len(hypotheses)} hipótesis | "
            f"Balance ${cycle_balance:.2f} ({cycle_balance_source}) | "
            f"Posiciones abiertas {open_count}/{self.max_open_trades} | "
            f"Riesgo {self.risk_per_trade_pct}% | R:R {self.risk_reward_ratio}:1"
        )

        approved = []
        rejected = []
        # Sprint 18 fix (B020): at most ONE position replacement per cycle.
        # Without this flag, if max_open_trades is reached and we receive N
        # hypotheses, the bot would do N consecutive replacements (close +
        # open) instead of approving only the best candidate. Each replacement
        # resets slots_left=1, then the next iteration sees slots_left=0
        # again and triggers another replacement → exposure can spiral.
        did_replace_this_cycle = False
        # Sprint 46M fix (gap found on review): the live incident that
        # motivated the conflicting-position gate was TWO NEW hypotheses
        # (long + short on BTC-USD) submitted in the SAME cycle, not a new
        # hypothesis vs. an already-persisted position. Positions aren't
        # written to position_repo until ExecutionNode's later
        # execute_trades step, so checking position_repo.open() alone
        # would NOT have caught this — by the time the second (short)
        # hypothesis is evaluated here, the first (long) one approved
        # moments earlier in this same loop still isn't in the repo yet.
        # Track assets approved earlier in THIS batch too.
        assets_approved_this_cycle: set[str] = set()
        for h in hypotheses:
            entry_price = float(h.get("price", 0))
            atr = float(h.get("atr_at_signal", 0))
            direction = h.get("direction", "long")
            asset = h.get("asset", "")

            # --- Sprint 46N (audit A4): resolve THIS hypothesis's
            # balance from the broker that actually services its asset
            # class (Alpaca for equities, crypto broker otherwise),
            # cached per class so we don't refetch the same broker
            # balance for every hypothesis in the cycle. A raise here
            # (e.g. live mode + broker unreachable) intentionally
            # propagates out of validate_and_size rather than being
            # caught per-hypothesis — sizing on a guessed balance is
            # worse than aborting the cycle (the outer scheduler
            # already logs/notifies on an uncaught step exception).
            _, _asset_class_for_balance = resolve_broker_for_close(
                asset, self._asset_to_class, self.broker, self.alpaca_broker,
            )
            _balance_key = _asset_class_for_balance if _asset_class_for_balance in ("crypto", "equity") else "crypto"
            if _balance_key not in balance_cache:
                balance_cache[_balance_key] = self.get_account_balance(asset=asset)
            account_balance, balance_source = balance_cache[_balance_key]

            # --- Sprint 46M: reject crypto shorts (binance.us spot has
            # no margin/borrow — a "short" here was never a real
            # exchange short, see config.yaml's allow_crypto_short
            # comment for the live incident that surfaced this). ---
            if (
                direction == "short"
                and not self.allow_crypto_short
                and get_asset_class(asset) == AssetClass.CRYPTO
            ):
                rejected.append({"hypothesis": h, "reason": "crypto_short_not_supported"})
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {
                        "asset": asset,
                        "direction": direction,
                        "reason": "crypto_short_not_supported",
                        "detail": (
                            "binance.us spot has no margin/borrow; a short "
                            "here is not a real exchange position. Set "
                            "trading.allow_crypto_short=true only if real "
                            "margin/futures trading is wired in."
                        ),
                    })
                print(f"  🚫 {asset:8} {direction:5} — crypto_short_not_supported (binance.us spot, no margin)")
                continue

            # --- Sprint 46N (audit M3): reject equity shorts (Alpaca
            # rejects fractional/notional sell-to-open orders — you
            # cannot short via `notional_usd`, only via whole-share
            # `amount` combined with an actual borrow, which this bot
            # never sets up). Before this gate, an equity "short"
            # hypothesis passed every other gate and simulated a clean
            # fill in PAPER mode (identical to a long, just inverted
            # P&L math) — completely masking that the exact same trade
            # is a guaranteed broker rejection in LIVE mode. Mirrors
            # the Sprint 46M crypto-short gate above; see
            # config.yaml's allow_equity_short comment.
            if (
                direction == "short"
                and not self.allow_equity_short
                and self._asset_to_class.get(asset) == "equity"
            ):
                rejected.append({"hypothesis": h, "reason": "equity_short_not_supported"})
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {
                        "asset": asset,
                        "direction": direction,
                        "reason": "equity_short_not_supported",
                        "detail": (
                            "Alpaca cannot open a short position via "
                            "notional/fractional orders (this bot's sizing "
                            "always uses notional_usd for equities). This "
                            "hypothesis would pass paper-mode simulation "
                            "cleanly and then fail outright in live mode. "
                            "Set trading.allow_equity_short=true only if "
                            "whole-share order sizing + margin/short "
                            "eligibility is wired in for equities."
                        ),
                    })
                print(f"  🚫 {asset:8} {direction:5} — equity_short_not_supported (Alpaca fractional/notional can't short)")
                continue

            # --- Sprint 46M: reject if this asset already has an OPEN
            # position (any direction). This is what let the bot open a
            # simultaneous BTC-USD long + short every cycle — each pair
            # committed the whole small account and the two legs could
            # never cleanly close against each other on a spot exchange.
            if self.block_conflicting_asset_positions:
                existing_directions = set()
                if self.position_repo is not None:
                    existing_directions.update(
                        p.direction for p in self.position_repo.open() if p.asset == asset
                    )
                already_approved_this_cycle = asset in assets_approved_this_cycle
                if existing_directions or already_approved_this_cycle:
                    reason = (
                        "asset_already_has_open_position"
                        if existing_directions
                        else "asset_already_approved_this_cycle"
                    )
                    rejected.append({"hypothesis": h, "reason": reason})
                    if self.audit:
                        self.audit.append("TRADE_REJECTED", {
                            "asset": asset,
                            "direction": direction,
                            "reason": reason,
                            "existing_directions": sorted(existing_directions),
                            "already_approved_this_cycle": already_approved_this_cycle,
                        })
                    print(
                        f"  🚫 {asset:8} {direction:5} — asset already has an "
                        f"open or just-approved position "
                        f"({', '.join(sorted(existing_directions)) or 'this cycle'}); "
                        f"skipping to avoid conflicting/duplicate exposure"
                    )
                    continue

            if entry_price <= 0:
                rejected.append({"hypothesis": h, "reason": "invalid_entry_price"})
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {"asset": h.get("asset"), "reason": "invalid_entry_price"})
                continue

            # Sprint 43 C3 fix: reject non-finite prices/ATR.
            # Python's `NaN <= 0` returns False (IEEE 754), so the
            # check above would let NaN slip through. Then
            # `max(NaN * 2.0, entry_price * 0.005) = NaN`,
            # `quantity = risk_usd / NaN = NaN`, and every downstream
            # comparison (max_notional, mandate_gate) silently
            # fails-open (`NaN > x` is False). The bot would then
            # try to open a position with a NaN quantity. Bail early
            # with an explicit reason.
            if not (math.isfinite(entry_price) and math.isfinite(atr)):
                rejected.append({"hypothesis": h, "reason": "non_finite_price_or_atr"})
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {
                        "asset": h.get("asset"),
                        "reason": "non_finite_price_or_atr",
                        "entry_price": entry_price,
                        "atr": atr,
                    })
                continue

            # --- Stop y Take Profit (ATR-based, Sprint 0+2) ---
            # Sprint 46R (audit B1): the stop has a 0.5%-of-entry floor
            # (defends against ATR=0 / weekend / illiquid corner cases),
            # but the take_profit did NOT — so an ATR=0 hypothesis
            # produced take_profit == entry_price and the position would
            # close at fill = pure churn of fees. Mirror the same
            # 0.5% floor on the TP side. The stop_distance floor is
            # kept as the audit's contract for the SL side; the TP
            # floor uses the same percent so the R:R ratio stays
            # intact at its lowest-volatility corner.
            #
            # Sprint 46R (audit B2): the 0.005 was a hard-coded
            # magic number; now it flows from
            # config.yaml's `min_sl_floor_pct` / `min_tp_floor_pct`.
            # Defaults (0.005) preserve the B1 behavior.
            stop_distance = max(
                atr * self.atr_stop_multiplier,
                entry_price * self.min_sl_floor_pct,
            )
            tp_distance = max(
                atr * self.atr_take_profit_multiplier,
                entry_price * self.min_tp_floor_pct,
            )
            if direction == "long":
                stop_loss = entry_price - stop_distance
                take_profit = entry_price + tp_distance
            else:
                stop_loss = entry_price + stop_distance
                take_profit = entry_price - tp_distance

            # --- Position sizing: qty = risk / distance ---
            risk_amount_usd = account_balance * (self.risk_per_trade_pct / 100.0)
            quantity = risk_amount_usd / stop_distance

            # --- Cap por max_capital_per_trade_pct ---
            notional = quantity * entry_price
            max_notional = account_balance * (self.max_capital_per_trade_pct / 100.0)

            # --- Step 1: Cap to max_notional if risk-sized position exceeds it ---
            if notional > max_notional:
                quantity = max_notional / entry_price
                notional = quantity * entry_price

            # --- Step 2: Auto-adjust if notional < min_order_usd (Sprint 18 fix) ---
            # The original Sprint 12 logic only triggered when max_notional < min_order.
            # That misses the common case where:
            #   max_notional = $10 (50% of $20) ✅ above min
            #   but risk/stop_distance = $5 (1% risk, 4% stop) ❌ below min
            # Without this fix, the trade is rejected by min_order check.
            # Sprint 18: trigger auto-adjust whenever computed notional < min_order,
            # log WHY (so user knows if it's config cap or risk/distance that's the
            # bottleneck), bump to min_order.
            auto_adjust_reason = None
            if notional < self.min_order_usd:
                if max_notional < self.min_order_usd:
                    auto_adjust_reason = "max_cap_below_min_order"
                else:
                    auto_adjust_reason = "risk_below_min_order"
                adjusted_cap = self.min_order_usd
                adjusted_cap_pct = (adjusted_cap / account_balance) * 100.0 if account_balance > 0 else 0
                quantity = adjusted_cap / entry_price
                notional = quantity * entry_price
                if self.audit:
                    self.audit.append("CAP_AUTO_ADJUSTED", {
                        "asset": h.get("asset"),
                        "reason": auto_adjust_reason,
                        "config_pct": self.max_capital_per_trade_pct,
                        "config_notional": round(max_notional, 2),
                        "raw_risk_notional": round(risk_amount_usd / stop_distance * entry_price, 2) if stop_distance > 0 else 0.0,
                        "min_order_usd": self.min_order_usd,
                        "adjusted_to_pct": round(adjusted_cap_pct, 2),
                        "adjusted_to_notional": round(notional, 2),
                    })
                print(f"  ⚠️ {h['asset']:8} {direction:5} — "
                      f"notional ${notional:.2f} < min ${self.min_order_usd:.2f} "
                      f"({auto_adjust_reason}). "
                      f"Bumping to ${notional:.2f} ({adjusted_cap_pct:.1f}% effective). "
                      f"⚠️ FIX config.yaml: increase max_capital_per_trade_pct or risk_per_trade_pct, "
                      f"or lower min_order_usd.")

            # --- Sprint 46N (audit A3): quantize to the exchange's real
            # lot step-size and re-buffer above its true min-notional,
            # crypto only (Alpaca equities trade fractional/notional
            # shares — no step-size/min-notional problem there).
            #
            # Without this, a trade sized to EXACTLY $10.00 (the stable-
            # state config: 50% of a $20 balance) can get truncated by
            # binance.us's lot step-size down to a notional just under
            # its real MIN_NOTIONAL -> the exchange rejects the order
            # outright at send time, after all the sizing work above
            # already happened. The native-OCO protection path already
            # quantizes via `exchange.amount_to_precision` (broker.py);
            # the entry-order path never did.
            #
            # Fix (matches the audit's suggested correction exactly):
            # 1. Look up the exchange's real min-notional for this
            #    symbol (if the exchange publishes one) and re-size to
            #    max(min_order_usd, min_notional_exchange) x 1.05 if the
            #    current notional is below that buffered floor — the 5%
            #    buffer is headroom so step-size truncation in step 2
            #    can't push us back under the exchange's true minimum.
            # 2. Quantize the quantity with `exchange.amount_to_precision`
            #    (the same call the OCO path already trusts) and re-
            #    verify the notional AFTER quantization.
            # 3. If, even after all that, the quantized notional is
            #    still below the exchange's real minimum, reject the
            #    trade rather than send an order we already know the
            #    exchange will bounce.
            #
            # Best-effort: any failure here (broker not configured,
            # markets not loaded, network hiccup, symbol not found) logs
            # a warning and falls back to the pre-existing 8-decimal
            # rounding — a quantization problem must never silently
            # block an otherwise-valid trade, same fail-open philosophy
            # as every other best-effort gate in this file.
            if self.broker is not None and get_asset_class(h.get("asset", "")) == AssetClass.CRYPTO:
                try:
                    ccxt_symbol = asset_symbol = h.get("asset", "")
                    if "-" in ccxt_symbol:
                        ccxt_symbol = ccxt_symbol.replace("-", "/")
                    elif "/" not in ccxt_symbol:
                        ccxt_symbol = f"{ccxt_symbol}/USDT"
                    exch = self.broker.exchange
                    if not getattr(exch, "markets", None):
                        exch.load_markets()
                    market = exch.market(ccxt_symbol)
                    min_notional_exchange = (
                        ((market or {}).get("limits", {}) or {}).get("cost", {}) or {}
                    ).get("min")
                    buffered_floor = max(self.min_order_usd, float(min_notional_exchange or 0.0)) * 1.05
                    if notional < buffered_floor:
                        quantity = buffered_floor / entry_price
                        notional = quantity * entry_price
                    quantized_amount = float(exch.amount_to_precision(ccxt_symbol, quantity))
                    if quantized_amount <= 0:
                        raise ValueError(
                            f"amount_to_precision produced non-positive qty "
                            f"({quantized_amount}) for {ccxt_symbol}"
                        )
                    quantity = quantized_amount
                    notional = quantity * entry_price
                    exchange_min_floor = max(self.min_order_usd, float(min_notional_exchange or 0.0))
                    if notional < exchange_min_floor:
                        rejected.append({
                            "hypothesis": h,
                            "reason": "below_exchange_min_notional_after_quantize",
                        })
                        if self.audit:
                            self.audit.append("TRADE_REJECTED", {
                                "asset": h.get("asset"),
                                "reason": "below_exchange_min_notional_after_quantize",
                                "notional_after_quantize": round(notional, 4),
                                "exchange_min_notional": min_notional_exchange,
                                "min_order_usd": self.min_order_usd,
                            })
                        print(
                            f"  🚫 {h['asset']:8} {direction:5} — notional "
                            f"${notional:.4f} still below the exchange's real "
                            f"minimum ${exchange_min_floor:.2f} AFTER step-size "
                            f"quantization. Rejecting — sending this would just "
                            f"bounce off the exchange."
                        )
                        continue
                except Exception as _quantize_err:
                    print(
                        f"  ⚠️ {h.get('asset', '?'):8} {direction:5} — lot-size/"
                        f"min-notional quantization skipped ({_quantize_err}); "
                        f"using unquantized sizing (8-decimal rounding at order "
                        f"time only)."
                    )
                    if self.audit:
                        self.audit.append("QUANTIZE_SKIPPED", {
                            "asset": h.get("asset"),
                            "error": str(_quantize_err)[:200],
                        })

            risk_amount_usd_eff = quantity * stop_distance

            # --- Sprint 46N (audit A2): cap risk multiplication from the
            # min_order_usd auto-adjust above. Bumping notional up to
            # min_order_usd is necessary (binance.us/exchange minimums
            # exist regardless of what risk_per_trade_pct would size),
            # but it must not be allowed to silently turn a 1%-risk
            # config into a 5%-risk trade. If the auto-adjust fired AND
            # the resulting effective risk exceeds
            # max_auto_adjust_risk_multiplier x the originally-intended
            # risk_amount_usd, reject the trade instead of opening it —
            # better to skip a trade this account is too small for than
            # to size it at a multiple of the risk the operator
            # configured. Only applies when auto-adjust actually fired;
            # a normally-sized trade (no adjustment) is never touched by
            # this gate.
            if auto_adjust_reason is not None and risk_amount_usd > 0:
                risk_multiplier = risk_amount_usd_eff / risk_amount_usd
                if risk_multiplier > self.max_auto_adjust_risk_multiplier:
                    rejected.append({
                        "hypothesis": h,
                        "reason": "auto_adjust_risk_multiplier_exceeded",
                    })
                    if self.audit:
                        self.audit.append("CAP_AUTO_ADJUSTED_REJECTED", {
                            "asset": h.get("asset"),
                            "auto_adjust_reason": auto_adjust_reason,
                            "intended_risk_usd": round(risk_amount_usd, 4),
                            "effective_risk_usd": round(risk_amount_usd_eff, 4),
                            "risk_multiplier": round(risk_multiplier, 2),
                            "max_allowed_multiplier": self.max_auto_adjust_risk_multiplier,
                            "min_order_usd": self.min_order_usd,
                        })
                    print(
                        f"  🚫 {h['asset']:8} {direction:5} — auto-adjust to "
                        f"min_order_usd would risk ${risk_amount_usd_eff:.4f} vs "
                        f"${risk_amount_usd:.4f} intended "
                        f"({risk_multiplier:.1f}x > {self.max_auto_adjust_risk_multiplier}x cap). "
                        f"Rejecting — account too small for this stop distance at "
                        f"the configured risk_per_trade_pct."
                    )
                    continue

            # --- Min order check (post-adjustment) ---
            if notional < self.min_order_usd:
                # Shouldn't happen after the auto-adjust above, but defensive
                rejected.append(
                    {"hypothesis": h, "reason": f"notional_${notional:.2f} < ${self.min_order_usd}"}
                )
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {"asset": h.get("asset"), "reason": f"min_order_${notional:.2f}"})
                print(f"  ❌ {h['asset']:8} {direction:5} — ${notional:.2f} < min ${self.min_order_usd}")
                continue

            # --- Balance check ---
            # If the account balance is so small that even min_order doesn't
            # fit, skip this trade cleanly. Log a clear reason.
            if account_balance < self.min_order_usd:
                rejected.append({
                    "hypothesis": h,
                    "reason": f"balance_${account_balance:.2f} < min_${self.min_order_usd}",
                })
                if self.audit:
                    self.audit.append("TRADE_REJECTED", {
                        "asset": h.get("asset"),
                        "reason": "balance_below_min_order",
                        "balance": account_balance,
                        "min_order_usd": self.min_order_usd,
                    })
                print(f"  ❌ {h['asset']:8} {direction:5} — balance ${account_balance:.2f} < min ${self.min_order_usd} "
                      f"(deposit more funds or lower min_order_usd in config)")
                continue

            trade = {
                "asset": h["asset"],
                "strategy": h["strategy"],
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "position_size": round(float(quantity), 8),
                "notional_usd": round(float(notional), 2),
                "risk_usd": round(float(risk_amount_usd_eff), 2),
                "atr_at_signal": atr,
                "balance_source": balance_source,
            }

            # Sprint 46O (audit M2): include the entry-side exchange fee
            # in the trade proposal so the mandate and the audit trail
            # can account for it. Before this fix, notional_usd
            # described what the bot INTENDED to spend, but the
            # realized capital outlay on binance.us is notional + fee
            # (the fee is debited from the asset bought, so for a $10
            # BTC buy at 0.02% taker the actual cash leaving the USD
            # balance is $10.002, and the BTC received is worth $9.998
            # at the same price). On a small account the difference is
            # noise per-trade, but it makes the per-trade exposure
            # reported by the mandate systematically undercount the
            # real cash tied up, and the cap on max_daily_trades
            # effectively allows 1 extra trade per 50x fee-pct — not
            # catastrophic, but easy to fix and a step the audit
            # explicitly asked for ("el fee de entrada tampoco está en
            # el sizing ni en el mandato").
            try:
                entry_fee_pct = self._fee_pct(h["asset"])
            except Exception:
                entry_fee_pct = 0.0
            entry_fee_usd = round(float(notional) * entry_fee_pct, 6)
            trade["entry_fee_usd"] = entry_fee_usd
            trade["entry_fee_pct"] = entry_fee_pct
            # All-in cost the account actually has to cover to enter
            # this position (notional + entry fee). The mandate uses
            # this for caps so the limits reflect real cash, not the
            # idealised notional.
            trade["notional_with_fees_usd"] = round(float(notional) + entry_fee_usd, 4)

            # --- Sprint 1: Mandate gate ---
            if self.mandate is not None:
                verdict = self.mandate.validate(trade)
                if not verdict.ok:
                    rejected.append({"trade": trade, "reason": verdict.reason})
                    if self.audit:
                        self.audit.append("MANDATE_BLOCKED", {"asset": trade["asset"], "reason": verdict.reason})
                    print(f"  🛡️  {trade['asset']:8} {direction:5} — blocked by mandate: {verdict.reason}")
                    continue
                if self.audit:
                    self.audit.append("MANDATE_OK", {"asset": trade["asset"], "notional": trade["notional_usd"], "risk": trade["risk_usd"]})

            # --- Sprint 44B: allocation policy drift gate (BlackRock #1) ---
            # Stricter than the 44A concentration cap. Rejects if this trade
            # would push a class above its target + drift_tolerance. Runs
            # BEFORE the 44A backstop so the policy is the primary signal.
            if self.position_repo is not None:
                policy_ok, policy_reason = self._check_allocation(
                    asset=h["asset"],
                    proposed_notional_usd=trade["notional_usd"],
                )
                if not policy_ok:
                    rejected.append({"trade": trade, "reason": policy_reason})
                    if self.audit:
                        self.audit.append("ALLOCATION_POLICY_BLOCKED", {
                            "asset": trade["asset"],
                            "reason": policy_reason,
                            "proposed_notional_usd": trade["notional_usd"],
                        })
                    print(
                        f"  📊 {trade['asset']:8} {direction:5} — "
                        f"blocked by allocation policy: {policy_reason}"
                    )
                    continue

            # --- Sprint 44A: asset-class concentration check (Bridgewater risk) ---
            # Without this, the bot can fill `max_open_trades=5` slots with
            # BTC + ETH + SOL + SPY + QQQ and call it "diversified" when it's
            # really 2 correlated bets (crypto bucket + equity_growth bucket).
            # We compute the *projected* exposure per class after this trade
            # would be added, and reject if any class would cross the cap.
            if self.asset_concentration_check and self.position_repo is not None:
                conc_ok, conc_reason = self._check_concentration(
                    asset=h["asset"],
                    proposed_notional_usd=trade["notional_usd"],
                )
                if not conc_ok:
                    rejected.append({"trade": trade, "reason": conc_reason})
                    if self.audit:
                        self.audit.append(
                            "CONCENTRATION_BLOCKED",
                            {
                                "asset": trade["asset"],
                                "reason": conc_reason,
                                "proposed_notional_usd": trade["notional_usd"],
                                "max_pct": self.max_asset_class_concentration_pct,
                            },
                        )
                    print(
                        f"  🪙  {trade['asset']:8} {direction:5} — "
                        f"blocked by concentration: {conc_reason}"
                    )
                    continue

            # --- Sprint 45 (N4): portfolio-risk gates ---
            # Wires the previously-unused Sprint 44 analytics
            # (asset_correlation.py, stress_test.py, tail_risk.py) into
            # real pre-trade gates. Philosophy for all three: missing
            # data or an internal error ALWAYS allows the trade (never
            # halt trading because a data provider is flaky) — only a
            # *confirmed* breach rejects it. See N5 fix in
            # asset_correlation.py for why `well_diversified` had to
            # become Optional[bool] first.
            if self.portfolio_stress_check and self.position_repo is not None:
                stress_ok, stress_reason = self._check_portfolio_stress(
                    asset=h["asset"],
                    proposed_notional_usd=trade["notional_usd"],
                )
                if not stress_ok:
                    rejected.append({"trade": trade, "reason": stress_reason})
                    if self.audit:
                        self.audit.append(
                            "STRESS_TEST_BLOCKED",
                            {
                                "asset": trade["asset"],
                                "reason": stress_reason,
                                "proposed_notional_usd": trade["notional_usd"],
                                "max_stress_drawdown_pct": self.max_stress_drawdown_pct,
                            },
                        )
                    print(
                        f"  📉 {trade['asset']:8} {direction:5} — "
                        f"blocked by stress test: {stress_reason}"
                    )
                    continue

            if self.correlation_check_enabled and self.position_repo is not None:
                corr_ok, corr_reason = self._check_portfolio_correlation(
                    asset=h["asset"],
                    proposed_notional_usd=trade["notional_usd"],
                )
                if not corr_ok:
                    rejected.append({"trade": trade, "reason": corr_reason})
                    if self.audit:
                        self.audit.append(
                            "CORRELATION_BLOCKED",
                            {
                                "asset": trade["asset"],
                                "reason": corr_reason,
                                "proposed_notional_usd": trade["notional_usd"],
                                "max_avg_correlation_pct": self.max_avg_correlation_pct,
                            },
                        )
                    print(
                        f"  🔗 {trade['asset']:8} {direction:5} — "
                        f"blocked by correlation: {corr_reason}"
                    )
                    continue

            if self.tail_risk_check_enabled and self.position_repo is not None:
                cvar_ok, cvar_reason = self._check_portfolio_tail_risk(
                    asset=h["asset"],
                    proposed_notional_usd=trade["notional_usd"],
                )
                if not cvar_ok:
                    rejected.append({"trade": trade, "reason": cvar_reason})
                    if self.audit:
                        self.audit.append(
                            "TAIL_RISK_BLOCKED",
                            {
                                "asset": trade["asset"],
                                "reason": cvar_reason,
                                "proposed_notional_usd": trade["notional_usd"],
                                "max_cvar_95_pct": self.max_cvar_95_pct,
                            },
                        )
                    print(
                        f"  ⚠️  {trade['asset']:8} {direction:5} — "
                        f"blocked by tail risk: {cvar_reason}"
                    )
                    continue

            # --- Sprint 2: max_open_trades ---
            if slots_left <= 0:
                # Sprint 18: try position replacement before outright rejection.
                # If the new hypothesis scores higher than the worst open position
                # (by replacement_score_threshold), close the worst and free a slot.
                #
                # B020 fix: at most ONE replacement per validate_and_size() call.
                # Subsequent hypotheses in the same cycle that hit max_open_trades
                # are rejected normally (no more churning the portfolio).
                replaced = False
                if (
                    self.enable_position_replacement
                    and self.position_repo is not None
                    and not did_replace_this_cycle
                ):
                    replaced = self._try_replace_position(
                        new_hyp=h,
                        new_trade=trade,
                        new_score_inputs={
                            "expected_move_pct": float(h.get("expected_move_pct", atr * 4 / max(entry_price, 1e-9) * 100)),
                            "atr_at_signal": atr,
                            "entry_price": entry_price,
                            "stop_loss": stop_loss,
                            "take_profit": take_profit,
                            "direction": direction,
                            "strategy": h.get("strategy", ""),
                        },
                    )
                if not replaced:
                    rejected.append({"trade": trade, "reason": f"max_open_trades:{self.max_open_trades}"})
                    if self.audit:
                        self.audit.append("TRADE_REJECTED", {"asset": trade["asset"], "reason": "max_open_trades_reached"})
                    print(f"  🚫 {trade['asset']:8} {direction:5} — slots llenos ({open_count}/{self.max_open_trades})")
                    continue
                # Replacement happened — slot freed, fall through to approval.
                did_replace_this_cycle = True
                slots_left = 1  # we just freed one slot by closing worst

            approved.append(trade)
            assets_approved_this_cycle.add(asset)
            slots_left -= 1

            if self.audit:
                self.audit.append(
                    "TRADE_APPROVED",
                    {
                        "asset": trade["asset"],
                        "direction": direction,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "qty": trade["position_size"],
                        "notional_usd": trade["notional_usd"],
                        "risk_usd": trade["risk_usd"],
                    },
                )

            # --- Sprint 2: registrar posición abierta ---
            # Sprint 43 C5 fix: removed `position_repo.add_open()` +
            # `audit.append(POSITION_OPENED)` + `event_bus.publish(TRADE_OPENED)`.
            # These used to run here (in the `risk_evaluation` step), but
            # the actual broker call happens in `execute_trades` AFTER
            # risk_evaluation. If the broker call failed (timeout,
            # ALPACA_NOT_CONFIGURED, SYMBOL_NOT_TRADEABLE, insufficient
            # balance, etc.), we were leaving a "ghost" position in the
            # repo that didn't exist on the broker — counting toward
            # max_open_trades, toward mandate exposure, and getting
            # watched by PositionMonitor for a non-existent SL/TP.
            #
            # The old comment claimed this happened "AFTER the broker
            # fill" but that was a lie — the workflow has risk_evaluation
            # BEFORE execute_trades. The persistence now happens in
            # `ExecutionNode._persist_filled_position()` on the success
            # path of the broker call. If the broker fails, the position
            # is never added to the repo. No more ghosts.

            print(
                f"  ✅ {trade['asset']:8} {direction:5} @ ${entry_price:>9.2f} | "
                f"qty={quantity:>10.6f} | "
                f"SL=${stop_loss:>9.2f} | TP=${take_profit:>9.2f} | "
                f"risk=${risk_amount_usd_eff:.2f} | notional=${notional:.2f}"
            )

        return {
            "approved_trades": approved,
            "rejected_trades": rejected,
            # Sprint 46N (audit A4): this is the cycle-level "headline"
            # balance (crypto broker), same as before this fix — NOT
            # the per-asset-class balance actually used to size each
            # individual trade (see each approved trade's own
            # "balance_source", and account_balance/balance_source in
            # `trade` are per-hypothesis now). Kept as the crypto figure
            # here for backward compatibility with any dashboard/log
            # consumer of this top-level field.
            "account_balance": cycle_balance,
            "balance_source": cycle_balance_source,
            "open_positions_after": self.position_repo.count_open() if self.position_repo else 0,
        }

    # ----------------------------------------------------------------------
    # Sprint 18: Portfolio Management — Position Replacement + Scoring
    # ----------------------------------------------------------------------

    # ----------------------------------------------------------------------
    # Sprint 44A: Asset-class concentration (Bridgewater risk)
    # ----------------------------------------------------------------------

    def _exposure_by_class(self) -> Dict[str, float]:
        """Return current exposure (USD notional) per asset class.

        CASH is bucketed separately and does NOT trigger the gate (it's
        parked capital, not a risky position). Symbols we don't have
        in our asset-class map fall into CASH too.
        """
        out: Dict[str, float] = {}
        if self.position_repo is None:
            return out
        for p in self.position_repo.open():
            cls = get_asset_class(p.asset).value
            out[cls] = out.get(cls, 0.0) + p.notional_usd
        return out

    def _check_concentration(
        self,
        asset: str,
        proposed_notional_usd: float,
    ) -> Tuple[bool, str]:
        """Reject a trade if its asset class would exceed the cap.

        Returns (ok, reason). `ok=True` means the trade passes the gate
        (or the gate is disabled / no open positions to compare against).
        `ok=False` carries a human-readable reason that goes to the
        audit ledger.

        Edge cases:
          - No open positions: always allow (first trade sets the
            baseline; concentration is a portfolio-level concept).
          - Unknown asset class (CASH): always allow (it's parking).
          - proposed_notional <= 0: allow (other gates will reject it).
        """
        if not self.asset_concentration_check or self.position_repo is None:
            return True, "concentration_check_disabled"
        if proposed_notional_usd <= 0:
            return True, "zero_notional_skipped"
        cls = get_asset_class(asset)
        if cls == AssetClass.CASH:
            # Unknown / parked symbol — not subject to the concentration
            # gate. Other gates (mandate allowlist) handle it.
            return True, "cash_class_skipped"

        open_positions = self.position_repo.open()
        if not open_positions:
            # First trade of an empty book — no concentration possible.
            return True, "empty_book_no_concentration"

        # Current exposure per class (only classes that have open positions).
        exposure_by_class = self._exposure_by_class()
        current_total = sum(exposure_by_class.values())
        if current_total <= 0:
            return True, "zero_existing_exposure"

        # Project the new exposure post-add.
        projected_class = exposure_by_class.get(cls.value, 0.0) + proposed_notional_usd
        projected_total = current_total + proposed_notional_usd
        if projected_total <= 0:
            return True, "zero_projected_exposure"
        projected_pct = (projected_class / projected_total) * 100.0

        if projected_pct > self.max_asset_class_concentration_pct:
            return False, (
                f"asset_class_{cls.value}_{projected_pct:.1f}pct_"
                f"exceeds_{self.max_asset_class_concentration_pct:.0f}pct_cap"
            )
        return True, "concentration_ok"

    # ----------------------------------------------------------------------
    # Sprint 45: portfolio-risk gates (wiring the Sprint 44 analytics
    # that the second audit found unused: asset_correlation.py,
    # stress_test.py, tail_risk.py).
    # ----------------------------------------------------------------------

    def _projected_positions(self, asset: str, proposed_notional_usd: float):
        """Current open positions + a synthetic candidate position for
        `asset`, as a list of lightweight objects exposing `.asset` and
        `.notional_usd` — the duck-typed shape `stress_test.py` expects.
        Does not touch the real repo."""
        import types
        positions = list(self.position_repo.open()) if self.position_repo is not None else []
        positions.append(types.SimpleNamespace(asset=asset, notional_usd=proposed_notional_usd))
        return positions

    def _check_portfolio_stress(
        self,
        asset: str,
        proposed_notional_usd: float,
    ) -> Tuple[bool, str]:
        """Reject a trade if it would push the WORST-CASE historical
        stress scenario (2008 GFC / 2020 COVID / 2022 rate hikes)
        beyond `max_stress_drawdown_pct`. Purely local computation
        (notional * historical shock table) — no network dependency,
        so this can run every cycle without any fail-open concern.
        """
        if not self.portfolio_stress_check or proposed_notional_usd <= 0:
            return True, "stress_check_disabled_or_zero_notional"
        try:
            positions = self._projected_positions(asset, proposed_notional_usd)
            results = stress_portfolio_all_scenarios(positions)
            worst = worst_case_drawdown(results)
        except Exception as e:
            # Never block a trade because the stress-test code itself
            # errored — that's a bug to fix, not a reason to halt
            # trading. Log it so it's visible.
            return True, f"stress_check_error:{str(e)[:100]}"
        worst_pct = abs(worst.drawdown_pct) * 100.0
        if worst_pct > self.max_stress_drawdown_pct:
            return False, (
                f"stress_test_{worst.scenario_name}_{worst_pct:.1f}pct_drawdown_"
                f"exceeds_{self.max_stress_drawdown_pct:.0f}pct_cap"
            )
        return True, f"stress_ok_worst_{worst.scenario_name}_{worst_pct:.1f}pct"

    def _check_portfolio_correlation(
        self,
        asset: str,
        proposed_notional_usd: float,
    ) -> Tuple[bool, str]:
        """Reject a trade if the projected book (existing + candidate)
        is confirmed highly correlated (avg pairwise correlation above
        `max_avg_correlation_pct`) — the "5 positions but 2 real bets"
        problem `asset_correlation.py` was built to detect.

        Needs live yfinance data (network). Sprint 45 fix (N5):
        `well_diversified` is now Optional[bool] — None means "not
        enough data to judge", which this gate treats as ALLOW (best
        effort), never as a rejection. Any exception also allows the
        trade rather than blocking trading on a data-provider outage.
        """
        if not self.correlation_check_enabled or proposed_notional_usd <= 0:
            return True, "correlation_check_disabled_or_zero_notional"
        if self.position_repo is None:
            return True, "no_position_repo"
        open_assets = {p.asset for p in self.position_repo.open()}
        open_assets.add(asset)
        if len(open_assets) < 3:
            # Correlation across 1-2 assets isn't a meaningful "am I
            # actually diversified" signal yet.
            return True, "too_few_assets_for_correlation_check"
        try:
            result = analyze_assets(
                sorted(open_assets),
                returns_cache=getattr(self, "_cycle_correlation_returns_cache", None),
            )
        except Exception as e:
            return True, f"correlation_check_error:{str(e)[:100]}"
        if result.well_diversified is None:
            return True, "correlation_check_no_data"
        if result.well_diversified:
            return True, f"correlation_ok_avg_{result.avg_correlation:.2f}"
        avg_pct = result.avg_correlation * 100.0
        if avg_pct > self.max_avg_correlation_pct:
            return False, (
                f"correlation_avg_{avg_pct:.1f}pct_exceeds_"
                f"{self.max_avg_correlation_pct:.0f}pct_cap"
            )
        # well_diversified=False but under our (looser) hard cap —
        # informational only, doesn't block.
        return True, f"correlation_below_hard_cap_avg_{avg_pct:.1f}pct"

    def _check_portfolio_tail_risk(
        self,
        asset: str,
        proposed_notional_usd: float,
    ) -> Tuple[bool, str]:
        """Reject a trade if the projected portfolio's daily CVaR 95%
        (expected loss in the worst 5% of days) breaches
        `max_cvar_95_pct`. Needs live yfinance data — same best-effort
        philosophy as the correlation gate: missing data or errors
        ALLOW the trade, only a confirmed breach rejects it.
        """
        if not self.tail_risk_check_enabled or proposed_notional_usd <= 0:
            return True, "tail_risk_check_disabled_or_zero_notional"
        if self.position_repo is None:
            return True, "no_position_repo"
        weights: Dict[str, float] = {}
        for p in self.position_repo.open():
            weights[p.asset] = weights.get(p.asset, 0.0) + p.notional_usd
        weights[asset] = weights.get(asset, 0.0) + proposed_notional_usd
        if len(weights) < 2:
            return True, "too_few_assets_for_tail_risk_check"
        try:
            result = compute_portfolio_tail_risk(
                weights,
                returns_cache=getattr(self, "_cycle_tail_risk_returns_cache", None),
            )
        except Exception as e:
            return True, f"tail_risk_check_error:{str(e)[:100]}"
        if result.n_observations == 0:
            return True, "tail_risk_check_no_data"
        cvar_95_pct = abs(result.cvar_95) * 100.0
        if cvar_95_pct > self.max_cvar_95_pct:
            return False, (
                f"cvar95_{cvar_95_pct:.1f}pct_exceeds_{self.max_cvar_95_pct:.0f}pct_cap"
            )
        return True, f"tail_risk_ok_cvar95_{cvar_95_pct:.1f}pct"

    def _check_allocation(
        self,
        asset: str,
        proposed_notional_usd: float,
    ) -> Tuple[bool, str]:
        """Sprint 44B: pre-trade check against the allocation policy.

        Returns (ok, reason). `ok=False` means the trade would push
        the portfolio outside its target + drift_tolerance for the
        trade's asset class.

        This is the formal, drift-aware version of the 44A
        concentration cap. If the policy is disabled, the 44A cap
        still runs as a backstop.
        """
        if self.position_repo is None or self.allocation_policy is None:
            return True, "no_position_repo_or_policy"
        return check_trade_against_policy(
            asset=asset,
            proposed_notional_usd=proposed_notional_usd,
            current_positions=self.position_repo.open(),
            policy=self.allocation_policy,
        )

    def score_position(self, pos: Position, current_price: Optional[float] = None) -> float:
        """
        Score an OPEN position by expected remaining value.

        Lower score = worse candidate to replace. Combines:
          - unrealized P&L % (losing positions score lower)
          - time held (positions held long with no progress score lower)
          - distance to TP vs SL (closer to SL = lower)
          - momentum alignment (if current price is moving against direction)

        Range: roughly [-1.0, +1.0] but unbounded. Higher is better.
        """
        price = current_price if current_price is not None else self.current_prices.get(pos.asset)
        score = 0.0

        # --- 1. Unrealized P&L % of notional (-1 to +1) ---
        if price is not None and pos.notional_usd > 0:
            upnl_pct = pos.unrealized_pnl(price) / pos.notional_usd
            score += upnl_pct  # losing = negative, winning = positive

        # --- 2. Distance from current price to SL (closer = worse) ---
        if price is not None and pos.stop_loss > 0:
            if pos.direction == "long":
                dist_to_sl = (price - pos.stop_loss) / max(price, 1e-9)
            else:
                dist_to_sl = (pos.stop_loss - price) / max(price, 1e-9)
            # dist_to_sl in [0, 1+]; > 0.05 = "comfortable"
            score += min(dist_to_sl * 2.0, 0.5) - 0.1  # up to +0.4, -0.1 if at SL

        # --- 3. Time decay (positions held > 24h without progress get penalized) ---
        import time as _t
        age_h = (pos.entry_ts and (_t.time() - pos.entry_ts) / 3600.0) or 0.0
        if age_h > 24:
            score -= 0.15  # stale positions lose points
        if age_h > 72:
            score -= 0.15  # even more stale

        # --- 4. Reward-to-risk remaining ---
        if price is not None and pos.entry_price > 0:
            if pos.direction == "long":
                remaining_tp = max(pos.take_profit - price, 0.0)
                remaining_sl = max(price - pos.stop_loss, 0.0)
            else:
                remaining_tp = max(price - pos.take_profit, 0.0)
                remaining_sl = max(pos.stop_loss - price, 0.0)
            if remaining_sl > 0:
                rr_remaining = remaining_tp / remaining_sl
                # rr_remaining > 1 = good, < 1 = bad
                score += min(rr_remaining * 0.2, 0.3) - 0.1
            else:
                # already past stop, score very low
                score -= 0.5

        return score

    def score_new_hypothesis(self, inputs: dict) -> float:
        """
        Score a NEW hypothesis (proposed trade) by expected edge.

        Higher = better candidate to enter. Combines:
          - expected_move_pct (theoretical edge before fees)
          - risk/reward ratio of the proposed trade
          - atr_at_signal (lower ATR relative to entry = cleaner setup)

        Range: roughly [-0.5, +1.0]. Higher is better.
        """
        score = 0.0

        # --- 1. Expected move (% of entry) ---
        expected_move_pct = float(inputs.get("expected_move_pct", 0.0))
        # 5% expected move is "very good", 1% is "okay", 0 = neutral
        score += min(expected_move_pct * 5.0, 0.6)  # up to +0.6

        # --- 2. R:R of the proposed trade ---
        entry = float(inputs.get("entry_price", 0.0))
        sl = float(inputs.get("stop_loss", 0.0))
        tp = float(inputs.get("take_profit", 0.0))
        if entry > 0 and sl > 0 and tp > 0:
            if inputs.get("direction", "long") == "long":
                risk = max(entry - sl, 1e-9)
                reward = max(tp - entry, 1e-9)
            else:
                risk = max(sl - entry, 1e-9)
                reward = max(entry - tp, 1e-9)
            rr = reward / risk if risk > 0 else 0
            # rr >= 2 = good (matches config default 4:2 ATR)
            score += min(rr * 0.2, 0.4)  # up to +0.4

        # --- 3. ATR relative to entry (tight stops = cleaner) ---
        atr = float(inputs.get("atr_at_signal", 0.0))
        if entry > 0 and atr > 0:
            atr_pct = atr / entry
            # 2% ATR = neutral, > 5% = noisy (penalty), < 1% = clean (bonus)
            if atr_pct > 0.05:
                score -= 0.2
            elif atr_pct < 0.01:
                score += 0.1

        return score

    def _try_replace_position(
        self,
        new_hyp: dict,
        new_trade: dict,
        new_score_inputs: dict,
    ) -> bool:
        """
        Try to close the worst-scoring open position to free a slot for `new_trade`.

        Returns True if a position was closed (slot freed) and `new_trade` should
        be approved. Returns False if no open position scored worse than
        `new_trade` minus the threshold (no replacement happens).
        """
        if self.position_repo is None:
            return False

        opens = self.position_repo.open()
        if not opens:
            return False

        new_score = self.score_new_hypothesis(new_score_inputs)
        # Find worst open position
        scored = []
        for p in opens:
            current_price = self.current_prices.get(p.asset)
            s = self.score_position(p, current_price=current_price)
            scored.append((p, s))
        scored.sort(key=lambda x: x[1])
        worst_pos, worst_score = scored[0]

        threshold = self.replacement_score_threshold
        if new_score <= worst_score + threshold:
            # New signal not enough better than the worst open. Don't replace.
            if self.audit:
                self.audit.append("REPLACEMENT_SKIPPED", {
                    "new_asset": new_trade["asset"],
                    "new_score": round(new_score, 3),
                    "worst_asset": worst_pos.asset,
                    "worst_score": round(worst_score, 3),
                    "threshold": threshold,
                    "delta": round(new_score - worst_score, 3),
                    "reason": "insufficient_edge",
                })
            return False

        # --- Replace: close worst at current price, free slot ---
        # B021 fix: if we don't have a fresh price for this asset, ABORT the
        # replacement rather than fall back to entry_price. Closing at
        # entry_price would record realized_pnl=0 even if the position is
        # actually in profit or loss — that corrupts the audit log and
        # daily_loss accounting. Better to skip this cycle and let the
        # PositionMonitor close it next tick when we have real prices.
        close_price = self.current_prices.get(worst_pos.asset)
        if close_price is None or close_price <= 0:
            if self.audit:
                self.audit.append("REPLACEMENT_SKIPPED", {
                    "new_asset": new_trade["asset"],
                    "new_score": round(new_score, 3),
                    "worst_asset": worst_pos.asset,
                    "worst_score": round(worst_score, 3),
                    "threshold": threshold,
                    "delta": round(new_score - worst_score, 3),
                    "reason": "no_current_price",
                })
            print(
                f"  ⏸️  REPLACEMENT_ABORTED {worst_pos.asset:8} — "
                f"no current price available; will retry next cycle"
            )
            return False

        # Sprint 43 C4 fix: send the broker order FIRST. If the broker
        # rejects/throws, the position stays open in the repo (it will
        # be retried next cycle). Previously, `close_position` ran
        # first and the broker failure was a silent no-op — the repo
        # thought the position was closed and stopped watching it,
        # while the exchange had no idea we wanted to close.
        # Sprint 46N (audit C1/C2): resolve the broker for THIS asset's
        # class instead of always calling self.broker (the crypto
        # client) — equity replacement-closes were silently failing
        # forever before this — and skip the real order entirely in
        # paper mode (previously there was no paper/live check here at
        # all, unlike the entry side).
        close_broker, asset_class = resolve_broker_for_close(
            worst_pos.asset, self._asset_to_class, self.broker, self.alpaca_broker
        )
        is_paper = not is_mandate_enabled(self.mode_override_path)
        if close_broker is not None and not is_paper:
            try:
                side = "sell" if worst_pos.direction == "long" else "buy"
                symbol = (
                    worst_pos.asset.replace("-", "/")
                    if "-" in worst_pos.asset and asset_class == "crypto"
                    else worst_pos.asset
                )
                broker_order = send_close_order(close_broker, asset_class, symbol, side, worst_pos.qty)
                if isinstance(broker_order, dict) and broker_order.get("status") == "failed":
                    raise RuntimeError(
                        f"broker_rejected:{broker_order.get('error', 'unknown')}"
                    )
            except Exception as e:
                msg = (f"[RiskManagerAgent] ⚠️ Broker FAILED cerrando {worst_pos.asset} "
                       f"para replacement: {e}. Replacement aborted; position stays open.")
                print(msg)
                if self.audit:
                    self.audit.append("REPLACEMENT_FAILED", {
                        "worst_asset": worst_pos.asset,
                        "new_asset": new_trade["asset"],
                        "broker_error": str(e),
                        "action": "worst_position_remains_open",
                    })
                if self.event_bus is not None:
                    self.event_bus.publish("SYSTEM_ERROR", {
                        "kind": "REPLACEMENT_FAILED",
                        "worst_asset": worst_pos.asset,
                        "new_asset": new_trade["asset"],
                        "broker_error": str(e),
                    })
                return False  # do NOT close in repo, do NOT claim success

        closed = self.position_repo.close_position(
            worst_pos.position_id,
            close_price=close_price,
            reason="REPLACED_BY_BETTER_SIGNAL",
            # Sprint 46N (audit M2): previously fee-free (gross P&L),
            # unlike PositionMonitor's SL/TP and smart-profit-take
            # closes (fee-aware since Sprint 46J).
            fee_pct=self._fee_pct(worst_pos.asset),
        )
        if closed is None:
            return False

        realized = closed.realized_pnl or 0.0
        if self.audit:
            self.audit.append("POSITION_REPLACED", {
                "closed_position_id": closed.position_id,
                "closed_asset": closed.asset,
                "closed_direction": closed.direction,
                "closed_entry": closed.entry_price,
                "closed_price": close_price,
                "closed_pnl_usd": round(realized, 4),
                "closed_score": round(worst_score, 3),
                "new_asset": new_trade["asset"],
                "new_direction": new_trade["direction"],
                "new_score": round(new_score, 3),
                "delta_score": round(new_score - worst_score, 3),
                "threshold": threshold,
            })

        print(
            f"  🔄 REPLACED {closed.asset:8} {closed.direction:5} "
            f"(score {worst_score:.2f}, pnl ${realized:+.2f}) → "
            f"opened {new_trade['asset']:8} {new_trade['direction']:5} "
            f"(score {new_score:.2f}, Δ {new_score - worst_score:+.2f})"
        )
        return True
