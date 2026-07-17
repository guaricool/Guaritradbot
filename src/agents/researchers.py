"""
Sprint 3 / Sprint 47B — Hypothesis Scoring (formerly "Debate Multi-Agente").

Originally named after Tauric Research's TradingAgents paper, the
implementation is sequential deterministic scoring — not a real
multi-agent debate. The audit's M1 finding: the docstring framing
("debate", "researcher", "team") is theater; the code is if-elif
scoring with hard-coded weights (0.4 / 0.4 / 0.2) and a magic
threshold (50; 40 for technical setups). The audit recommended
either recalibrating with backtest data or renaming to honest
names. Sprint 47B took the second route:

  BullResearcher  ->  BullScorer
  BearResearcher  ->  BearScorer
  RiskTeam        ->  RiskScorer
  PortfolioManager ->  ScoreSynthesizer
  DebateAgent     ->  HypothesisScorer

Each scorer still does what it always did (assign a 0-100 score
or penalty from a hypothesis + a list of human-readable
reasons). The orchestrator is still called as a step in the
workflow YAML. What changed: the names now match the
implementation, so a future contributor reading the code
doesn't expect a real LLM-backed debate to happen.

The scoring formula is unchanged:
    final = 0.4 * bull + 0.4 * (100 - bear) - 0.2 * risk_penalty

The 40/50 threshold split is unchanged (technical setups are
more permissive). The audit's complaint that these numbers
"chosen by feel, not data" remains valid — recalibration is
a separate piece of work (would need a backtest of historical
hypotheses against realized P&L, which doesn't exist yet).
The renamed classes document the limitation in their own
docstrings.

Sprint 46S (audit M1 follow-up) added the crypto-short prefilter
in StrategyAgent that already removes the worst offender
(crypto short hypotheses) before this scorer runs, so the
"crypto shorts pass by construction" finding from the audit
is no longer reproducible. The recalibration task, if pursued,
should focus on the 0.4/0.4/0.2 weights rather than threshold
hunting.

Every step still gets recorded in the audit ledger for
forensics — the event type is still `DEBATE_APPROVED` /
`DEBATE_REJECTED` (preserved for backward compat with the
dashboard's audit feed).
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional

from src.core.logging_setup import get_logger
logger = get_logger(__name__)


class BullScorer:
    """Busca evidencia técnica a favor de cada hipótesis."""

    @staticmethod
    def score(hyp: dict) -> tuple[int, List[str]]:
        """Devuelve (score 0-100, lista de razones a favor)."""
        reasons = []
        score = 50  # baseline neutral

        asset = hyp.get("asset", "")
        direction = hyp.get("direction", "long")
        strategy = hyp.get("strategy", "")
        rsi = float(hyp.get("rsi_at_signal", 50))
        macd = float(hyp.get("macd_at_signal", 0))
        atr = float(hyp.get("atr_at_signal", 0))

        # Mean reversion signals
        if "MeanReversion" in strategy:
            if direction == "long" and rsi < 35:
                score += 25
                reasons.append(f"RSI {rsi:.1f} < 35 — oversold extremo, favorece rebote long")
            elif direction == "short" and rsi > 65:
                score += 25
                reasons.append(f"RSI {rsi:.1f} > 65 — overbought extremo, favorece short")

        # MACD momentum
        if "MACD" in strategy:
            if direction == "long" and macd > 0:
                score += 20
                reasons.append("MACD positivo — momentum alcista confirmado")
            elif direction == "short" and macd < 0:
                score += 20
                reasons.append("MACD negativo — momentum bajista confirmado")

        # Volatilidad razonable (ATR > 0 implica trade tiene aire para correr)
        if atr > 0:
            score += 5
            reasons.append(f"ATR ${atr:.4f} — volatilidad presente")

        return min(max(score, 0), 100), reasons


class BearScorer:
    """Busca evidencia técnica en contra de cada hipótesis."""

    @staticmethod
    def score(hyp: dict) -> tuple[int, List[str]]:
        """Devuelve (score 0-100, lista de razones en contra)."""
        reasons = []
        score = 50  # baseline neutral (50 = mismo peso que bull)

        asset = hyp.get("asset", "")
        direction = hyp.get("direction", "")
        strategy = hyp.get("strategy", "")
        rsi = float(hyp.get("rsi_at_signal", 50))
        macd = float(hyp.get("macd_at_signal", 0))
        atr = float(hyp.get("atr_at_signal", 0))
        price = float(hyp.get("price", 0))

        # Si la dirección va CONTRA el momentum
        if "MACD" in strategy:
            if direction == "long" and macd < 0:
                score += 20
                reasons.append("MACD negativo — risk-on short en contra del momentum")
            elif direction == "short" and macd > 0:
                score += 20
                reasons.append("MACD positivo — short contra momentum")

        # Volatilidad excesiva
        if atr > 0 and price > 0:
            atr_pct = (atr / price) * 100
            if atr_pct > 3:
                score += 20
                reasons.append(f"ATR {atr_pct:.2f}% del precio — alta volatilidad, whipsaw probable")

        # RSI en zona neutra (sin edge claro) — solo MeanReversion
        # Sprint 11: las señales técnicas (BB bounce, Support/Res) son
        # válidas aunque RSI esté en zona neutra — el edge viene del
        # setup técnico, no del RSI.
        if "MeanReversion" in strategy:
            if 35 <= rsi <= 65:
                score += 15
                reasons.append(f"RSI {rsi:.1f} en zona neutra — sin edge claro")
            elif rsi < 30 and direction == "long":
                # Fuerte confirmación: RSI oversold + dirección long
                score -= 10
                reasons.append(f"RSI {rsi:.1f} oversold — confirma long")
            elif rsi > 70 and direction == "short":
                score -= 10
                reasons.append(f"RSI {rsi:.1f} overbought — confirma short")

        # EMA cruz — si muerte cruzada y signal long
        if "EMA" in strategy or "GoldenCross" in strategy or "DeathCross" in strategy:
            if direction == "long" and "DeathCross" in strategy:
                score += 25
                reasons.append("Death cross (EMA20<EMA50) — operando en contra del trend")
            elif direction == "short" and "GoldenCross" in strategy:
                score += 25
                reasons.append("Golden cross (EMA20>EMA50) — short contra trend alcista")

        # Sprint 11: señales técnicas estructurales (BB/Support/Resistance)
        # tienen edge por sí solas. Bajamos el bear score para que el
        # debate no las rechace automáticamente.
        if any(s in strategy for s in ("BB_", "Support_", "Resistance_", "Stoch_")):
            score -= 5  # ligero descuento por setup técnico válido
            reasons.append("Setup técnico estructural (BB/S/R/Stoch) — edge independiente del RSI")

        return min(max(score, 0), 100), reasons


class RiskScorer:
    """Chequeos duros: correlación, concentración, volatilidad."""

    @staticmethod
    def score(hyp: dict, open_positions: list) -> tuple[int, List[str]]:
        """
        Devuelve (penalty 0-100, lista de razones). 0 = sin riesgo extra, 100 = bloqueado.
        """
        reasons = []
        penalty = 0

        asset = hyp.get("asset", "")
        direction = hyp.get("direction", "long")

        # 1. Ya tenemos posición abierta en el mismo asset y dirección opuesta
        for pos in open_positions:
            if pos.asset == asset and pos.direction != direction:
                penalty += 80
                reasons.append(f"Posición abierta {pos.direction} en {asset} — opuesta a esta propuesta")
            elif pos.asset == asset and pos.direction == direction:
                penalty += 90
                reasons.append(f"Ya hay {direction} abierto en {asset} — duplicar exposición")

        # 2. Concentración: demasiados símbolos del mismo sector
        # Mapa simple de sectores
        sectors = {
            "BTC-USD": "crypto", "BTCUSDT": "crypto",
            "GLD": "metals", "USO": "energy",
            "SPY": "equity_index", "QQQ": "equity_index",
        }
        target_sector = sectors.get(asset, "other")
        sector_open = sum(1 for p in open_positions if sectors.get(p.asset, "other") == target_sector)
        if sector_open >= 2:
            penalty += 30
            reasons.append(f"{sector_open} posiciones ya abiertas en sector '{target_sector}'")

        return min(penalty, 100), reasons


class ScoreSynthesizer:
    """
    Sintetiza los tres debates y toma decisión final.

    final_score = 0.4*bull + 0.4*(100 - bear) - 0.2*risk_penalty

    Si final_score >= 50, aprueba; si < 50, rechaza con razón.
    """

    def __init__(self, audit=None):
        self.audit = audit

    def decide(self, hypothesis: dict, open_positions: list,
               news_context: Optional[Dict[str, Dict[str, Any]]] = None,
               social_context: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Score a hypothesis into a final approve/reject decision.

        Sprint 49: added `news_context` parameter (optional dict
        keyed by asset, with `news_sentiment` in [-1, +1] per
        asset).

        Sprint 50: added `social_context` parameter (same shape,
        from SentimentAnalyst / Reddit). When BOTH are present,
        the two signals are combined into a single sentiment
        in [-1, +1] (agreement amplifies, disagreement cancels)
        and applied as a small TIE-BREAKER:
          - Combined positive: +5 * combined to bull_score
          - Combined negative: -5 * combined to bear_score
        The COMBINED magnitude is clamped to [-1, +1] before
        scaling, so even if both signals fire at +1, the
        bull_score adjustment is capped at +5.

        Why combine (not stack): the audit's intent is that
        sentiment is a tie-breaker, not a primary signal. If
        we stacked them (news +5 + social +5 = +10), the
        sentiment would dominate the technicals, which is
        the opposite of the design. Combining keeps the
        signal bounded and well-defined.
        """
        bull_score, bull_reasons = BullScorer.score(hypothesis)
        bear_score, bear_reasons = BearScorer.score(hypothesis)
        risk_penalty, risk_reasons = RiskScorer.score(hypothesis, open_positions)

        # Sprint 49+50: combined news + social sentiment
        # tie-breaker. Look up the hypothesis's asset in both
        # contexts (if any) and combine the per-asset sentiment.
        # Clamp the combined value to [-1, +1] so even if
        # both sources fire at +1 we cap at +5 adjustment.
        asset = hypothesis.get("asset", "")
        news_sent = 0.0
        social_sent = 0.0
        news_count = 0
        social_count = 0
        if news_context and asset:
            ctx = news_context.get(asset) or {}
            news_sent = float(ctx.get("news_sentiment", 0.0) or 0.0)
            news_count = int(ctx.get("news_count", 0) or 0)
        if social_context and asset:
            ctx = social_context.get(asset) or {}
            social_sent = float(ctx.get("social_sentiment", 0.0) or 0.0)
            social_count = int(ctx.get("post_count", 0) or 0)
        # Combined: simple average of the two, then clamp.
        combined_sent = (news_sent + social_sent) / 2.0
        combined_sent = max(-1.0, min(1.0, combined_sent))
        # Bug fix: news/social sentiment describes whether news is
        # bullish or bearish for the ASSET'S PRICE, not for this
        # specific hypothesis. bull_score/bear_score mean "evidence
        # FOR / AGAINST this hypothesis" (see BullScorer/BearScorer
        # above, which both branch on direction) — so for a SHORT
        # hypothesis, positive (price-bullish) sentiment is evidence
        # AGAINST the short and must go to bear_score, not bull_score.
        # Previously this block applied combined_sent to bull/bear
        # with no direction check at all, which was correct for longs
        # by coincidence but backwards for shorts.
        direction = hypothesis.get("direction", "long")
        aligned_sent = -combined_sent if direction == "short" else combined_sent
        if aligned_sent != 0.0:
            bull_adj = 5.0 * max(0.0, aligned_sent)
            bear_adj = 5.0 * max(0.0, -aligned_sent)
            if bull_adj > 0:
                bull_score += bull_adj
                bull_reasons = list(bull_reasons) + [
                    f"combined_sentiment {combined_sent:+.2f} "
                    f"(news {news_sent:+.2f}/{news_count}h + "
                    f"social {social_sent:+.2f}/{social_count}p), aligned with "
                    f"{direction} → bull +{bull_adj:.1f}"
                ]
            if bear_adj > 0:
                bear_score += bear_adj
                bear_reasons = list(bear_reasons) + [
                    f"combined_sentiment {combined_sent:+.2f} "
                    f"(news {news_sent:+.2f}/{news_count}h + "
                    f"social {social_sent:+.2f}/{social_count}p), against "
                    f"{direction} → bear +{bear_adj:.1f}"
                ]

        final = 0.4 * bull_score + 0.4 * (100 - bear_score) - 0.2 * risk_penalty

        # Sprint 11: threshold dinámico según tipo de setup.
        # Setups técnicos estructurales (BB/S/R/Stoch) tienen edge
        # independiente — más permisivos (threshold 40).
        # Cruces puros (RSI/MACD/EMA cross) son más ruidosos — threshold 50.
        strategy = hypothesis.get("strategy", "")
        is_technical = any(s in strategy for s in ("BB_", "Support_", "Resistance_", "Stoch_"))
        threshold = 40 if is_technical else 50

        decision = "APPROVED" if final >= threshold else "REJECTED"
        if risk_penalty >= 80:
            decision = "REJECTED"
            reason = risk_reasons[0] if risk_reasons else "risk_too_high"
        else:
            reason = bull_reasons[0] if decision == "APPROVED" and bull_reasons else \
                     bear_reasons[0] if bear_reasons else \
                     risk_reasons[0] if risk_reasons else "no_clear_edge"

        result = {
            "asset": hypothesis.get("asset"),
            "direction": hypothesis.get("direction"),
            "bull_score": bull_score,
            "bear_score": bear_score,
            "risk_penalty": risk_penalty,
            "final_score": round(final, 2),
            "decision": decision,
            "reason": reason,
            "bull_reasons": bull_reasons,
            "bear_reasons": bear_reasons,
            "risk_reasons": risk_reasons,
        }

        if self.audit:
            self.audit.append(f"DEBATE_{decision}", result)

        # Sprint 48 (decision log): persist the verdict for
        # future-lesson injection AND for post-hoc analysis.
        # We look up the last N lessons for this asset BEFORE
        # recording -- that way the `considered_lessons` field
        # captures exactly what context the scorer had access
        # to at decision time. Recording the hypothesis with
        # the lessons it considered lets a future backtest
        # correlate "the bot knew about lesson X and still
        # took the trade" -- a signal that the lesson wasn't
        # weighted heavily enough.
        try:
            from src.safety.decision_log import get_decision_log
            log = get_decision_log()
            considered = log.recent_lessons_for(
                hypothesis.get("asset", ""), n=3
            )
            log.record_hypothesis(
                asset=hypothesis.get("asset", ""),
                direction=hypothesis.get("direction", "long"),
                strategy=strategy,
                score=round(final, 2),
                bull_score=bull_score,
                bear_score=bear_score,
                risk_penalty=risk_penalty,
                decision=decision,
                reason=reason,
                considered_lessons=considered,
            )
        except Exception as _e:
            # Decision log failure must NEVER block the trade.
            # Best-effort: log the error and move on. (Same
            # fail-open pattern as the file write in
            # DecisionLog._append itself.)
            logger.info(f"[DecisionLog] could not record hypothesis: {_e}")

        return result

    def decide_all(self, hypotheses: list, open_positions: list,
                   news_context: Optional[Dict[str, Dict[str, Any]]] = None,
                   social_context: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        verdicts = []
        for h in hypotheses:
            verdicts.append(self.decide(
                h, open_positions,
                news_context=news_context,
                social_context=social_context,
            ))
        return verdicts

    def filter_approved(self, hypotheses: list, verdicts: list) -> list:
        """Devuelve solo las hipótesis aprobadas."""
        approved = []
        for h, v in zip(hypotheses, verdicts):
            if v["decision"] == "APPROVED":
                approved.append(h)
        return approved


class HypothesisScorer:
    """
    Agente orquestador del debate. Diseñado para ser invocado como paso
    del workflow YAML entre StrategyAgent y RiskManagerAgent.
    """

    def __init__(self, position_repo=None, audit=None):
        self.position_repo = position_repo
        self.manager = ScoreSynthesizer(audit=audit)

    def run_debate(self, inputs: dict, state: dict) -> Dict[str, Any]:
        # Method name kept as `run_debate` for workflow YAML
        # compatibility (the YAML step action: `run_debate`).
        # Internally this is just running the scorers, not a
        # real debate -- see the module docstring.
        # Sprint 49+50: pull the news + social context from
        # the upstream steps. The HypothesisScorer combines
        # them into a single sentiment tie-breaker (magnitude
        # capped at +/- 5). If either step is absent or failed,
        # its context is {} and the combined_sent just uses
        # the other source's signal.
        hypotheses = state.get("generate_hypotheses", {}).get("hypotheses", [])
        open_positions = self.position_repo.open() if self.position_repo else []
        news_context = state.get("scan_news", {}) or {}
        social_context = state.get("scan_social_sentiment", {}) or {}

        if not hypotheses:
            logger.info('[HypothesisScorer] sin hipótesis, debate vacío')
            return {"hypotheses": [], "verdicts": [], "approved_hypotheses": []}

        logger.info(f'[HypothesisScorer] {len(hypotheses)} hipótesis × {len(open_positions)} posiciones abiertas')
        if news_context or social_context:
            n_news = len(news_context)
            n_social = len(social_context)
            avg_sent = sum(
                ctx.get("news_sentiment", 0.0) for ctx in news_context.values()
            ) / max(n_news, 1)
            avg_social = sum(
                ctx.get("social_sentiment", 0.0) for ctx in social_context.values()
            ) / max(n_social, 1)
            logger.info(
                f'[HypothesisScorer] context: news={n_news}assets/{avg_sent:+.2f} '
                f'social={n_social}assets/{avg_social:+.2f} '
                f'(combined +/- 5 tie-breaker)'
            )

        verdicts = self.manager.decide_all(
            hypotheses, open_positions,
            news_context=news_context, social_context=social_context,
        )
        approved = self.manager.filter_approved(hypotheses, verdicts)

        for v in verdicts:
            icon = "✅" if v["decision"] == "APPROVED" else "❌"
            logger.info(f"  {icon} {v['asset']:8} {v['direction']:5} | final={v['final_score']:5.1f} (bull={v['bull_score']} bear={v['bear_score']} risk={v['risk_penalty']}) | {v['reason']}")

        logger.info(f'[HypothesisScorer] → {len(approved)}/{len(hypotheses)} hipótesis aprobadas por el debate')

        return {
            "hypotheses": hypotheses,          # todas (audit)
            "verdicts": verdicts,
            "approved_hypotheses": approved,  # las que pasan al RiskManager
        }
