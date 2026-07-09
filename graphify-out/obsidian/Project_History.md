# Project History

## Origen

Guaritradbot empezó en algún momento de 2026-05 cuando Carlos (usuario)
le pidió a Gemini (otra IA) que construyera un bot de trading
algorítmico. Gemini implementó las fases 1-11 (motor de workflows,
agentes, backtester, hyperopt, paper trading, dashboard, notificaciones,
re-optimización, Docker).

## Fase Gemini (2026-05 a 2026-07-08)

**Lo bueno**: estableció la arquitectura multi-agente, el event bus, el
optimizador con grid search, el dashboard Streamlit, las notificaciones
Telegram, y el deployment Docker. Eso fue mucha base útil.

**Lo malo**: cometió errores sistemáticos que rompieron el bot en cada
corrida. El más grave: `event_bus.emit()` siendo llamado cuando
`EventBus` solo tenía `publish()`. Eso significa que el bot
**nunca corrió realmente** desde su deploy en Coolify — solo dejó
el contenedor `exited:unhealthy`.

## Fase Mavis (Sprint 0-7) — refactor épico

Carlos me pidió continuar. Le hice auditoría completa y encontré
**16 bugs**. Decidimos hacer un rebuild sistemático en 7 sprints:

1. **Sprint 0 — Fix bugs críticos**: emit/publish, env keys, Docker input,
   RSI Wilder, MACD cruces, ATR stops. Resultado: el bot corrió
   completo sin errores por primera vez.
2. **Sprint 1 — Safety Layer**: Mandate Gate + Kill Switch + Audit
   Ledger. Inspirado en Vibe-Trading.
3. **Sprint 2 — Position Tracking**: PositionRepository persistente +
   Take Profit ATR-based + PositionMonitor. Inspirado en NautilusTrader
   "crash-only design".
4. **Sprint 3 — Debate Multi-Agente**: Bull/Bear/Risk/PortfolioManager.
   Inspirado en TradingAgents.
5. **Sprint 4 — Backtester Fix**: métricas correctas (Profit Factor,
   Expectancy, real win rate) + Walk-Forward. Inspirado en freqtrade.
6. **Sprint 5 — Real Re-Optimization**: cerró el placeholder
   `run_reoptimization()`. Inspirado en intelligent-trading-bot.
7. **Sprint 6 — State Machine + Fail-Fast**: Component lifecycle +
   data validator NaN/Inf. Inspirado en NautilusTrader.
8. **Sprint 7 — PDF Indicators**: aplicó el Manual del Buen Trader:
   DM/ADX, Estocástico, Bollinger, S/R.

## Fuentes de inspiración

Cada sprint vino de un repo externo del PDF2 (cinco repos para conocer)
o de NautilusTrader (la pieza arquitectónica más profunda):

| Inspiración | Usado en |
|---|---|
| **Vibe-Trading** (HKUDS) | Mandate Gate, Kill Switch, audit ledger style |
| **TradingAgents** (Tauric) | Debate multi-agente Bull/Bear/PM |
| **freqtrade** | Métricas gold-standard backtester + walk-forward |
| **claude-trading-skills** | Trade journal mindset (no transferido al código) |
| **intelligent-trading-bot** | Periodic re-train pattern (Sprint 5) |
| **NautilusTrader** | EventBus, State Machine, fail-fast, crash-only |

Ver [[Inspirations]] para detalle completo.

## Decisiones difíciles

- **NinjaTrader (del PDF2) NO se adoptó** — Carlos ya tenía su propio
  bot en Python. La guía NinjaTrader es para traders manuales que
  quieren robotizar. Aquí ya éramos Python-first.
- **No LLM debate todavía** — el debate actual es determinístico
  (basado en indicadores). Hacerlo con Claude/GPT sería un
  sprint 8 con requerimientos de API keys y costos.
- **No multi-broker** — solo Binance. El pattern de Nautilus sería
  `ExecutionClient` interface. Para $100 de capital, Binance es
  suficiente.

## Hoy (2026-07-09)

- Bot corre localmente con `--once` sin excepciones
- 8 commits locales (push al remoto esperando decisión de Carlos)
- Coolify tiene el contenedor pero está exited (muerto por bug
  original)
- 0 claves API reales configuradas (todo en paper mode)

---

Ver [[Architecture]] para el diagrama del sistema actual.
Ver [[Deployment]] para el path a producción.
