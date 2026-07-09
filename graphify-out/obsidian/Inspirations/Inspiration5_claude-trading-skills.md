---
title: "Inspiration #5 — claude-trading-skills (LLM decision loop)"
tags: [inspiration, inspiration-5, llm, claude, scaffolding]
source: external_repos/claude-trading-skills/
sprint_origin: Sprint_4, Sprint_5
---

# Inspiration #5 — claude-trading-skills

**Origen**: `external_repos/claude-trading-skills/` (clonado el 2026-07-08).

## Qué es
Conjunto de skills/prompts que envuelven a Claude como un "trading assistant". Diseñado para ser **human-in-the-loop**: el LLM propone, el humano aprueba.

## Qué aporta a Guaritradbot

### Estructura decision-loop
- **Analyst Agent** → propone tesis (`generate_hypotheses` en [[WorkflowEngine]]).
- **Claude Risk Agent** → evalúa tesis contra contexto (`risk_evaluation`).
- **Trade Executor** → ejecuta (`execute_trades`).
- → En Guaritradbot los 3 roles los juegan agentes deterministas en `src/agents/`.

### Modo human-in-the-loop
- En `claude-trading-skills`, antes de ejecutar, **Claude pregunta al humano por Telegram**.
- En Guaritradbot, el modo `human_in_the_loop` está en `config.yaml`. Cuando está activo, `[[ExecutionNode]]` envía mensaje a Telegram con botones *Approve* / *Reject*.
- **Esta es la opción recomendada** para capital real con $100.

### Prompts para contexto
- `research-prompt.md`, `risk-prompt.md`, etc. documentan **exactamente qué información recibe el LLM** antes de cada decisión. Guaritradbot usa el mismo patrón: cada workflow step tiene inputs/outputs tipados.

## Por qué NO se copió literal
- Depende de Claude API → costo $0.01-0.10 por decisión.
- Latencia de 2-5s es aceptable para swing trading pero mata HFT.
- Carlos aprobó mantener **decisiones deterministas** para el MVP.

## Lo que tomó Guaritradbot
- ✅ Modo `human_in_the_loop` con Telegram approval ([[ExecutionNode]])
- ✅ Estructura prompt-like: cada step documenta inputs/outputs
- ✅ Separación clara analyst/risk/executor
- ❌ Dependencia Claude API
- ❌ Cost-per-decision

## Patrones reusables (cross-project)
> **Cualquier flujo donde el humano no deba estar 100% atento puede resolverse con human-in-the-loop**: el bot propone, el humano aprueba vía una interfaz asíncrona (Telegram, Slack, email). Es el patrón de aprobación más simple y más infrautilizado.

Ver: [[Sprint_3_Multi_Agent_Debate]], [[ExecutionNode]], [[RiskManagerAgent]]
