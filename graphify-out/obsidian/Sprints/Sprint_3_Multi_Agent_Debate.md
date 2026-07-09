# Sprint 3 — Multi-Agent Debate

## Objetivo

Hacer que las decisiones del bot pasen por un **debate estructurado**
antes de aprobar cualquier trade.

Inspirado en **TradingAgents** (Tauric Research, paper arxiv 2412.20138):

> "TradingAgents is a multi-agent trading framework that mirrors the dynamics
> of real-world trading firms. By deploying specialized LLM-powered agents:
> from fundamental analysts, sentiment experts, and technical analysts,
> to trader, risk management team, the platform collaboratively evaluates
> market conditions and informs trading decisions. Moreover, these agents
> engage in dynamic discussions to pinpoint the optimal strategy."

## Módulo nuevo

### [[Modules/DebateAgent]] (Sprint 3)
4 agentes que debaten cada hipótesis:

1. **BullResearcher**: evidencia a favor
   - Mean reversion LONG: RSI<35 favorece rebote → +25
   - Mean reversion SHORT: RSI>65 favorece short → +25
   - MACD bullish + MACD>0 → +20
   - Volatilidad presente (ATR>0) → +5

2. **BearResearcher**: evidencia en contra
   - MACD en contra del direction → +20
   - Alta volatilidad (ATR>3% del precio, whipsaw) → +20
   - RSI en zona neutra (35-65, sin edge) → +15
   - Death cross con signal long (contra trend) → +25

3. **RiskTeam**: chequeos duros
   - Posición opuesta en mismo asset → penalty 80 (bloqueante)
   - Posición misma dirección duplicada → penalty 90
   - Concentración sectorial >= 2 → penalty 30

4. **PortfolioManager**: sintetiza
   ```
   final = 0.4 * bull + 0.4 * (100 - bear) - 0.2 * risk_penalty
   APPROVED si final >= 50
   REJECTED si final < 50 o risk_penalty >= 80
   ```

## Cambios en el workflow

`src/workflows/trading_loop.yaml`:
```yaml
- id: generate_hypotheses    # antes
- id: debate_hypotheses      # NUEVO Sprint 3
    agent: DebateAgent
    action: run_debate
- id: risk_evaluation
```

`src/agents/risk_agent.py` ahora consume `state["debate_hypotheses"]["approved_hypotheses"]`
si existe, en lugar de `state["generate_hypotheses"]["hypotheses"]`.
Retrocompatible.

## Commit

`8e84390` — feat(sprint 3): multi-agent debate (Bull/Bear/Risk/Portfolio)

## Test

```
[OK] APPROVED BTC-USD  long  | final= 50.0 (bull=75 bear=50 risk=0)
[BLOCK] REJECTED SPY      long  | final= 36.0 (bull=55 bear=65 risk=0)
[BLOCK] REJECTED USO      long  | final= 32.0 (bull=55 bear=75 risk=0)
[BLOCK] REJECTED GLD      short | final= 24.0 (bull=55 bear=95 risk=0)
[BLOCK] REJECTED BTC-USD  long  | penalty=90 (pos duplicada)
✅ Debate funciona — riesgo duplicado bloqueado, RSI/MACD/EMA coherentes
```

Casos cubiertos:
1. Edge claro (BTC RSI<35 + MACD>0) → APPROVED
2. Zona neutra (SPY RSI=50) → REJECTED
3. Contra trend (USO death cross + long) → REJECTED
4. Alta volatilidad (GLD ATR=500%) → REJECTED
5. Duplicación (mismo asset+dirección) → REJECTED con penalty alto

## Limitación actual

El debate es **determinístico** (basado en indicadores técnicos). Un
debate LLM-powered (Claude/GPT-4o analizando news + sentiment) sería
un Sprint 8+ con requerimientos de API keys.

## Ver también

- [[Sprints_Index]]
- [[Architecture]] — el debate aparece entre Strategy y Risk
