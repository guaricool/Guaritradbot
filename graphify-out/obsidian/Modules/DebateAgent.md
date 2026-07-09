# DebateAgent (Bull/Bear/Risk/Portfolio)

`src/agents/researchers.py`

## Responsabilidad

Estructura un **debate multi-agente** antes de aprobar trades.
Inspirado en [[Inspiration3_TradingAgents|TradingAgents]].

## 4 agentes

### 1. BullResearcher
Busca evidencia técnica a favor. Score 0-100.

- Mean reversion LONG + RSI<35 → +25 (oversold extremo)
- Mean reversion SHORT + RSI>65 → +25 (overbought)
- MACD bullish + MACD>0 → +20
- MACD bearish + MACD<0 → +20
- ATR presente (volatilidad) → +5

### 2. BearResearcher
Busca evidencia en contra. Score 0-100.

- MACD en contra del direction → +20 penalty
- ATR > 3% del precio (whipsaw) → +20 penalty
- RSI en zona neutra (35-65) → +15 penalty
- Death cross con signal long → +25 penalty (contra trend)
- Golden cross con signal short → +25 penalty

### 3. RiskTeam
Chequeos duros. Penalty 0-100.

- Posición opuesta en mismo asset → **80** (casi bloqueante)
- Posición misma dirección duplicada → **90** (bloqueante)
- Concentración sectorial >= 2 → 30

### 4. PortfolioManager
Síntesis final:
```
final = 0.4 · bull
      + 0.4 · (100 - bear)
      - 0.2 · risk_penalty

APPROVED si final >= 50 (override por risk_penalty ≥ 80)
REJECTED en caso contrario
```

## Output del debate

```python
{
    "hypotheses": [...],          # todas (para audit)
    "verdicts": [
        {"asset": ..., "direction": ...,
         "bull_score": ..., "bear_score": ..., "risk_penalty": ...,
         "final_score": ...,
         "decision": "APPROVED" | "REJECTED",
         "reason": "...",
         "bull_reasons": [...], "bear_reasons": [...], "risk_reasons": [...]}
    ],
    "approved_hypotheses": [...],  # solo las que pasan
}
```

`[[Modules/RiskManagerAgent]]` consume `approved_hypotheses`.

## Audit logging

Cada debate se registra como `DEBATE_APPROVED` o `DEBATE_REJECTED`
con todos los scores y razones.

## Test verificado (Sprint 3)

| Hipótesis | Score | Decisión | Razón |
|-----------|-------|---------|-------|
| BTC RSI<35 + MACD>0 long | 50 | APPROVED | MACD positivo |
| SPY RSI=50 long | 36 | REJECTED | RSI neutro sin edge |
| USO death cross + long | 32 | REJECTED | Contra trend |
| GLD ATR=500% short | 24 | REJECTED | Whipsaw probable |
| BTC long duplicada | penalty 90 | REJECTED | Pos duplicada |

## Limitación

El debate es **determinístico** (indicadores técnicos solamente).
Para hacerlo con LLM-powered sentiment de news se necesitaría Sprint 8+
con API keys.

## Conecta con

- [[Modules/MarketAnalystAgent]] / [[Modules/StrategyAgent]] — recibe market_data + hipótesis
- [[Modules/RiskManagerAgent]] — entrega approved_hypotheses
- [[Modules/AuditLedger]] — DEBATE_APPROVED / DEBATE_REJECTED
- [[Modules/Position_Repository]] — el RiskTeam lo consulta
- [[Sprints/Sprint_3_Multi_Agent_Debate]] — sprint principal
