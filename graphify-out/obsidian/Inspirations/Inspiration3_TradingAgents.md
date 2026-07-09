---
title: "Inspiration #3 — TradingAgents (multi-LLM debate)"
tags: [inspiration, inspiration-3, multi-agent, llm-debate]
source: external_repos/TradingAgents/
sprint_origin: Sprint_3
---

# Inspiration #3 — TradingAgents

**Origen**: `external_repos/TradingAgents/` (clonado localmente el 2026-07-08).

## Qué es
Framework multi-agente donde 4 roles (Fundamentals, Sentiment, News, Technicals) emiten análisis, un Research Manager arbitra, y un Trader decide. Inspirado directamente en el paper *"TradingAgents: Multi-Agents LLM Financial Trading"*.

## Qué aporta a Guaritradbot
- **Arquitectura del debate** en `src/agents/researchers.py`: Bull/Bear/Risk/PortfolioManager.
- El **patrón Research Manager**: un agente que lee todas las tesis y decide. En Guaritradbot ese rol lo juega el `DebateAgent`.
- La idea de **3 rondas de debate**: thesis inicial → rebuttal cruzado → síntesis del PM.
- Cada researcher emite **confianza** (high/medium/low) que el PM usa para ponderar el voto final.

## Por qué NO se copió literal
- TradingAgents usa **LLMs (GPT/Claude)** para cada researcher → costo + latencia + dependency de API keys.
- En Guaritradbot el debate es **determinista** sobre indicadores técnicos. La estructura queda; el LLM no.
- Aplicable a futuro si Carlos aprueba usar OpenAI/Anthropic para análisis fundamental (sentimiento de noticias, earnings).

## Lo que tomó Guaritradbot
- ✅ Estructura Bull/Bear/Risk/PM en [[DebateAgent]]
- ✅ 3 rondas de debate (thesis → rebuttal → final)
- ✅ Confianza ponderada en voto final
- ❌ Dependencia LLM
- ❌ Análisis fundamental/sentimiento (futuro)

## Patrones reusables (cross-project)
> Cualquier bot que necesite consenso multi-agente debe separar: tesis individuales con confianza, árbitro, voto final ponderado. La verdad está en el debate, no en un agente solo.

Ver: [[Sprint_3_Multi_Agent_Debate]], [[DebateAgent]], [[Research_Evolution_Roadmap]]
