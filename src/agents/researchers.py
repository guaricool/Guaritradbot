"""
Sprint 3 — Debate Multi-Agente.

Inspirado en TradingAgents (Tauric Research). Antes de aprobar una
hipótesis, cuatro "agentes" debaten:

1. Bull Researcher — busca evidencia técnica a favor del trade.
2. Bear Researcher — busca evidencia técnica en contra.
3. Risk Team — chequeos duros: correlación entre posiciones abiertas,
   concentración sectorial, volatilidad agregada.
4. Portfolio Manager — sintetiza los scores y aprueba/rechaza.

El score combinado es:
    final = 0.4 * bull_score + 0.4 * (100 - bear_score)
            - 0.2 * risk_penalty

Si final >= 50, se aprueba (filtrado del filtro de RiskManager después).
Si final < 50, se rechaza con la razón del agente que más penalizó.

Cada paso debate se registra en el audit ledger para forensics.
"""
from __future__ import annotations
from typing import List, Dict, Any


class BullResearcher:
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


class BearResearcher:
    """Busca evidencia técnica en contra de cada hipótesis."""

    @staticmethod
    def score(hyp: dict) -> tuple[int, List[str]]:
        """Devuelve (score 0-100, lista de razones en contra)."""
        reasons = []
        score = 50  # baseline neutral (50 = mismo peso que bull)

        asset = hyp.get("asset", "")
        direction = hyp.get("direction", "long")
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

        # RSI en zona neutra (sin edge claro)
        if "MeanReversion" in strategy:
            if 35 <= rsi <= 65:
                score += 15
                reasons.append(f"RSI {rsi:.1f} en zona neutra — sin edge claro")

        # EMA cruz — si muerte cruzada y signal long
        if "EMA" in strategy or "GoldenCross" in strategy or "DeathCross" in strategy:
            if direction == "long" and "DeathCross" in strategy:
                score += 25
                reasons.append("Death cross (EMA20<EMA50) — operando en contra del trend")
            elif direction == "short" and "GoldenCross" in strategy:
                score += 25
                reasons.append("Golden cross (EMA20>EMA50) — short contra trend alcista")

        return min(max(score, 0), 100), reasons


class RiskTeam:
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


class PortfolioManager:
    """
    Sintetiza los tres debates y toma decisión final.

    final_score = 0.4*bull + 0.4*(100 - bear) - 0.2*risk_penalty

    Si final_score >= 50, aprueba; si < 50, rechaza con razón.
    """

    def __init__(self, audit=None):
        self.audit = audit

    def decide(self, hypothesis: dict, open_positions: list) -> Dict[str, Any]:
        bull_score, bull_reasons = BullResearcher.score(hypothesis)
        bear_score, bear_reasons = BearResearcher.score(hypothesis)
        risk_penalty, risk_reasons = RiskTeam.score(hypothesis, open_positions)

        final = 0.4 * bull_score + 0.4 * (100 - bear_score) - 0.2 * risk_penalty

        decision = "APPROVED" if final >= 50 else "REJECTED"
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

        return result

    def decide_all(self, hypotheses: list, open_positions: list) -> List[Dict[str, Any]]:
        verdicts = []
        for h in hypotheses:
            verdicts.append(self.decide(h, open_positions))
        return verdicts

    def filter_approved(self, hypotheses: list, verdicts: list) -> list:
        """Devuelve solo las hipótesis aprobadas."""
        approved = []
        for h, v in zip(hypotheses, verdicts):
            if v["decision"] == "APPROVED":
                approved.append(h)
        return approved


class DebateAgent:
    """
    Agente orquestador del debate. Diseñado para ser invocado como paso
    del workflow YAML entre StrategyAgent y RiskManagerAgent.
    """

    def __init__(self, position_repo=None, audit=None):
        self.position_repo = position_repo
        self.manager = PortfolioManager(audit=audit)

    def run_debate(self, inputs: dict, state: dict) -> Dict[str, Any]:
        hypotheses = state.get("generate_hypotheses", {}).get("hypotheses", [])
        open_positions = self.position_repo.open() if self.position_repo else []

        if not hypotheses:
            print("[DebateAgent] sin hipótesis, debate vacío")
            return {"hypotheses": [], "verdicts": [], "approved_hypotheses": []}

        print(f"[DebateAgent] {len(hypotheses)} hipótesis × {len(open_positions)} posiciones abiertas")

        verdicts = self.manager.decide_all(hypotheses, open_positions)
        approved = self.manager.filter_approved(hypotheses, verdicts)

        for v in verdicts:
            icon = "✅" if v["decision"] == "APPROVED" else "❌"
            print(f"  {icon} {v['asset']:8} {v['direction']:5} | final={v['final_score']:5.1f} "
                  f"(bull={v['bull_score']} bear={v['bear_score']} risk={v['risk_penalty']}) | {v['reason']}")

        print(f"[DebateAgent] → {len(approved)}/{len(hypotheses)} hipótesis aprobadas por el debate")

        return {
            "hypotheses": hypotheses,          # todas (audit)
            "verdicts": verdicts,
            "approved_hypotheses": approved,  # las que pasan al RiskManager
        }
