# Inspirations

Los 7 sprints no salieron de la nada. Cada decisión vino de una
**fuente externa** — un repo o framework usado como inspiración.

## Índice

| # | Repositorio / framework | De dónde | Qué nos llevamos |
|---|-------------------------|---------|------------------|
| 1 | [Vibe-Trading](Inspiration1_Vibe-Trading) | HKUDS | Mandate Gate, Kill Switch, audit ledger pattern |
| 2 | Manual del Buen Trader (PDF2) | Carlos (PDF) | DM/ADX, Estocástico, Bollinger, S/R |
| 3 | [TradingAgents](Inspiration3_TradingAgents) | Tauric Research (arxiv 2412.20138) | Debate multi-agente Bull/Bear/PM |
| 4 | [freqtrade](Inspiration4_freqtrade) | GPLv3 | Gold-standard backtester metrics |
| 5 | [claude-trading-skills](Inspiration5_claude-trading-skills) | tradermonty (MIT) | Trade journal mindset (Sprint 8+) |
| 6 | [intelligent-trading-bot](Inspiration6_intelligent-trading-bot) | asavinov | Periodic re-train pattern (Sprint 5) |
| 7 | [NautilusTrader](Inspiration7_NautilusTrader) | nautechsystems | EventBus, StateMachine, fail-fast, crash-only |

## Mapeo sprints ↔ inspiraciones

```
Sprint 0 (Bug Fixes)              → Auditoría propia + B001-B016
Sprint 1 (Safety Layer)           → Vibe-Trading, TradingAgents
Sprint 2 (Position Tracking)      → NautilusTrader (crash-only design)
Sprint 3 (Multi-Agent Debate)     → TradingAgents
Sprint 4 (Backtester Fix)         → freqtrade + intelligent-trading-bot
Sprint 5 (Real Reoptimization)    → intelligent-trading-bot (predict_rolling)
Sprint 6 (State Machine + Fail)   → NautilusTrader
Sprint 7 (PDF Indicators)         → Manual del Buen Trader (PDF2)
```

## Detalles

- [[Inspiration1_Vibe-Trading]] — patrón **mandate gate** completo
- [[Inspiration2_Manual]] — 7 indicadores técnicos
- [[Inspiration3_TradingAgents]] — research team con debates
- [[Inspiration4_freqtrade]] — métricas gold-standard + walk-forward
- [[Inspiration5_claude-trading-skills]] — trade journal (no implementado)
- [[Inspiration6_intelligent-trading-bot]] — pipeline ML + re-train
- [[Inspiration7_NautilusTrader]] — event-driven architecture

## Lo que NO tomamos

- **Librerías LLM-powered de TradingAgents/Vibe-Trading** — el debate es
  determinístico por ahora. Implementar agentes Claude/GPT costaría
  tokens y rompería la autonomía del bot (depende de API keys externas
  con costo por request).
- **Alpha zoo de 460+ factors (Vibe-Trading)** — overkill para nuestro
  alcance. Con 5 activos y 7 indicadores bien entendidos, no
  necesitamos factor zoo. Si en Sprint 8+ queremos expandir,
  podemos importar los 20-30 más relevantes.
- **Multi-broker (NautilusTrader, Vibe-Trading)** — Binance solo es
  suficiente para $100. Más brokers = más superficie de bug.
- **NinjaTrader (Manual del Buen Trader)** — la guía es para traders
  manuales que quieren automatizar; nosotros ya éramos Python-first.
