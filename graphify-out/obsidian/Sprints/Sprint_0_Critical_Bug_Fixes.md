# Sprint 0 — Critical Bug Fixes

## Objetivo

Hacer que el bot **corra** por primera vez. Antes de este sprint,
`python main.py --once` explotaba silenciosamente en cada corrida.

## Bugs encontrados (16 totales)

Ver [[Bugs_Index]] para la lista completa. Los críticos
corregidos en este sprint:

- **B001**: `execution_agent.py` usaba `self.event_bus.emit()` cuando
  `EventBus` solo tiene `publish()` → `AttributeError` en cada fill.
- **B002**: `broker.py` leía `EXCHANGE_API_KEY` pero `.env.example`
  declaraba `BINANCE_API_KEY` → nunca se cargaban las keys.
- **B003**: `execution_node.py` usaba `input()` que rompe el daemon
  en Docker (no hay stdin interactivo) → colgaba el contenedor.
- **B004**: RSI calculado con SMA en vez de Wilder (EMA con α=1/14) →
  señales más lentas que el estándar TradingView/TA-Lib.
- **B005**: MACD comparaba estado actual (`MACD > Signal`) en vez de
  detectar cruces → durante una tendencia alcista entera, el bot
  estaba long indefinidamente.
- **B006**: Stop loss hardcoded a $5 fijos → para BTC eran 0.008%
  (insignificante), para USO eran 4.5% (violento). Sin ATR.
- **B008**: `tf_map["4h"] = "1h"` silenciaba el resampleo → las
  estrategias GLD/USO "4h" recibían velas de 1h.
- **B009**: `generate_vectorized_signals` devolvía siempre 1 o -1 →
  backtest siempre invertido (sin cash). Sin estado "flat".
- **B015**: venv sin `yaml`, `schedule`, `streamlit`, `dotenv` →
  instalación limpia del repo siempre falla.

## Cambios principales

- `market_analyst.py`: RSI Wilder, ATR(14), resample 4h, period
  adaptativo por timeframe
- `strategy_agent.py`: detección de cruces (NO estado), generador de
  señales mantiene FLAT por defecto
- `risk_agent.py`: stop loss ATR-based, qty = risk / distance,
  min order check
- `execution_node.py`: try/except EOFError en lugar de `input()`,
  publica ORDER_PENDING_APPROVAL en lugar de bloquear
- `broker.py`: keys alineadas con `.env.example`
- `.gitignore`: excluye `__pycache__`, `audit/`, `latest_state.json`

## Commit

`10d144c` — fix critical bugs + Sprint 1

## Test

`python main.py --once` corrió sin errores por primera vez. El
último commit previo (Gemini) había dejado el contenedor
`exited:unhealthy`; ahora el flujo completo se ejecuta de principio
a fin.

## Ver también

- [[Sprints_Index]]
- [[Architecture]] — diagrama post-Sprint 0
- [[Bugs_Index]] — bugs restantes (no críticos)
