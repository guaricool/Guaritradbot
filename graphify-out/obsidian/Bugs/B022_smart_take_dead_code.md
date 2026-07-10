# B022 — Smart Profit-Take Dead Code (HYPOTHESIS_GENERATED Never Emitted)

**Severidad**: 🔴 Crítica (feature no funcional)
**Sprint**: 18 (encontrado durante revisión post-sprint)
**Componente**: `main.py` wiring de `position_monitor.check_with_signals()` + `src/agents/strategy_agent.py`

## Síntomas

- Bot tiene posición LONG BTC en profit.
- StrategyAgent genera una señal SHORT fuerte.
- `PositionMonitor.check_with_signals()` debería cerrar el LONG preventivamente.
- NUNCA LO HACE — la posición sigue abierta hasta que el SL la cierre.

## Causa raíz

`main.py` lee las señales del audit ledger:

```python
recent_hyps = audit.read_since(_t.time() - 3600)
signals = [h for h in recent_hyps if h.get("event_type") == "HYPOTHESIS_GENERATED"]
```

PERO `StrategyAgent.evaluate_strategies()` NUNCA emite eventos `HYPOTHESIS_GENERATED` al audit ledger. Solo retorna `{"hypotheses": [...]}` al state del workflow.

Resultado: `signals = []` siempre. `check_with_signals()` no tiene señales para evaluar. SMART_PROFIT_TAKE nunca se activa. La feature completa está muerta.

Búsqueda que lo confirma:
```bash
grep -r "HYPOTHESIS_GENERATED" src/
# → solo aparece en el docstring de audit_ledger.py (lista de tipos soportados)
```

## Fix (Sprint 18 patch)

### 1. StrategyAgent ahora emite eventos

`src/agents/strategy_agent.py`:

```python
class StrategyAgent:
    def __init__(self, strategy_params=None, audit=None):
        ...
        self.audit = audit   # B022: nuevo param

    def evaluate_strategies(self, inputs, state):
        ...
        if hypotheses:
            ...
            if self.audit is not None:
                for h in hypotheses:
                    self.audit.append("HYPOTHESIS_GENERATED", {
                        "asset": h["asset"],
                        "tf": h.get("tf", ""),
                        "direction": h["direction"],
                        "strategy": h["strategy"],
                        "price": h["price"],
                        "atr_at_signal": h.get("atr_at_signal", 0),
                        "rsi_at_signal": h.get("rsi_at_signal", 0),
                        "strength": _hypothesis_strength(h),  # 0..1
                    })
```

### 2. Helper `_hypothesis_strength`

Score 0..1 que refleja qué tan fuerte es la señal:

| Estrategia | Direction | Condición | Strength |
|---|---|---|---|
| RSI mean reversion | long | RSI < 25 | 0.9 |
| RSI mean reversion | long | RSI < 30 | 0.8 |
| RSI mean reversion | long | RSI < 35 | 0.65 |
| RSI mean reversion | short | RSI > 75 | 0.9 |
| Stochastic | any | - | 0.75 |
| ADX / breakout | any | - | 0.8 |
| MACD / EMA crosses | any | - | 0.7 |
| Bollinger / S/R | any | - | 0.65 |
| Unknown | any | - | 0.5 (default) |

### 3. Wire-up en main.py

```python
"StrategyAgent": StrategyAgent(strategy_params=strategy_params, audit=audit),
```

## Tests que cubren este bug

- `tests/test_position_monitor_sprint18.py::B022StrategyAgentEmitsHypothesisEventsTest` (5 tests):
  - `test_strong_rsi_oversold_emits_with_high_strength`
  - `test_mild_oversold_emits_with_moderate_strength`
  - `test_overbought_short_emits_with_high_strength`
  - `test_default_strength_is_neutral`
  - `test_audit_receives_hypothesis_events_when_audit_set`
  - `test_end_to_end_smart_take_with_real_audit_events` ← integración completa

## Lección de diseño

Cuando construyas una feature que depende de un side-effect de otro módulo:
1. **Verifica que el side-effect realmente ocurre**. No asumas.
2. Si el módulo no emite el evento necesario, agrega la emisión o busca otra fuente.
3. **Tests de integración end-to-end** son la única forma de cazar este tipo de bug.

El test unitario de `check_with_signals` pasaba porque le pasábamos `signals` directamente. Pero en producción, `signals` siempre era `[]`, así que la feature nunca corría. Un test que verifica "los eventos llegan al audit" es lo que faltaba.

## Audit events ahora en uso (post-B022)

| Event | Quién emite | Quién consume |
|---|---|---|
| `HYPOTHESIS_GENERATED` | StrategyAgent | PositionMonitor.check_with_signals (smart profit-take) |
| `TRADE_APPROVED` | RiskManagerAgent | Mandate Gate (daily_loss legacy) |
| `TRADE_FILLED` | ExecutionNode | Mandate Gate (legacy exposure) |
| `TRADE_CLOSED` | PositionMonitor / RiskManagerAgent | Mandate Gate (daily_loss realized) |
| `POSITION_OPENED` | RiskManagerAgent | Mandate Gate (audit-fallback exposure) |
| `POSITION_REPLACED` | RiskManagerAgent | Dashboard / forensics |
| `SMART_PROFIT_TAKE` | (via TRADE_CLOSED reason) | Dashboard / forensics |
| `CAP_AUTO_ADJUSTED` | RiskManagerAgent | Dashboard / forensics |