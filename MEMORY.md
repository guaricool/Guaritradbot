# Guaritradbot - Project Memory

## 1. Visión General
Guaritradbot es un sistema de trading automatizado, de grado institucional, construido bajo una arquitectura de **Inteligencia Artificial Multi-Agente**. Su objetivo es operar activos múltiples (SPY, QQQ, BTC-USD, GLD, USO) utilizando un flujo de trabajo auditable que imita a una firma de trading real.

## 2. Decisiones Arquitectónicas (ADRs)
- **Workflow YAML**: El comportamiento de los agentes se orquesta a través de archivos `.yaml` (`src/workflows/trading_loop.yaml`).
- **Agentes Desacoplados**:
  - `MarketAnalystAgent`: Extracción y normalización de datos (vía `yfinance`).
  - `StrategyAgent`: Generador de hipótesis (Reversión, Tendencia, Breakout).
  - `RiskManagerAgent`: Guardián matemático (Filtros de correlación, stop loss dinámico por ATR).
  - `ExecutionAgent`: Módulo de Paper Trading (y eventualmente ejecución en vivo).
- **Inspiración Repositorios Externos**: El diseño incorpora los mejores patrones de `claude-trading-skills` (Workflows YAML), `freqtrade` (Hyperopt/Backtesting), y `TradingAgents` (Debate Multi-Agente).

## 3. Estado Actual
- **Completado**: Fase 1 (Motor de Workflows) y Fase 2 (Agentes Especializados) y Fase 4 (Ejecución en simulación).
- **En Progreso / Próximo**: 
  - Subida a GitHub (`guaricool/Guaritradbot`).
  - Auditoría de código, arquitectura y seguridad por parte de los agentes especializados.
  - Implementación de la Fase 3: Backtesting riguroso y Optimización de Hiperparámetros (Hyperopt).

## 4. Reglas de Desarrollo
1. **Evitar Errores Comunes**: Nunca operar sin backtest, nunca sobre-optimizar (curve-fitting), mantener gestión estricta de riesgo, no confiar ciegamente en señales de IA sin validación cruzada.
2. **Paper-First**: Todas las estrategias nuevas se prueban en paper-trading primero.
3. **Auditoría Multi-Agente**: Todo código debe ser revisado por el rol de Architect y Code Reviewer antes de considerarse de producción.
