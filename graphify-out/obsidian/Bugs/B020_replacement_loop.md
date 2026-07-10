# B020 — Position Replacement Loop (One Per Cycle Required)

**Severidad**: 🔴 Crítica
**Sprint**: 18 (encontrado durante revisión post-sprint)
**Componente**: `RiskManagerAgent.validate_and_size()` en `src/agents/risk_agent.py`

## Síntomas

- Bot tiene `max_open_trades` posiciones abiertas.
- En un solo ciclo, llegan N hipótesis al RiskAgent.
- El bot reemplaza UNA posición por la primera hipótesis.
- Después reabre `slots_left=1`, aprueba la nueva posición, y `slots_left=0`.
- En la siguiente iteración (hipótesis #2), `slots_left=0` → vuelve a intentar reemplazo.
- Si hay 10 hipótesis fuertes, el bot puede hacer 10 reemplazos consecutivos.
- Cada reemplazo es un roundtrip al broker (cerrar + abrir).

## Causa raíz

En el loop principal de `validate_and_size`, después de un reemplazo exitoso:

```python
if replaced:
    slots_left = 1   # liberamos un slot
    ...
approved.append(trade)
slots_left -= 1     # slots_left vuelve a 0
```

En la siguiente iteración del `for h in hypotheses`, `slots_left` está en 0, lo cual dispara el bloque de replacement otra vez. Sin un flag que limite a UN replacement por ciclo, el bot reemplaza N veces donde N = número de hipótesis que pasan los filtros previos.

Trace con max_open=2, 5 hipótesis fuertes:

| Iter | slots_left (in) | Acción | slots_left (out) | Posiciones abiertas |
|------|-----------------|--------|------------------|---------------------|
| init | 0 | - | - | 2 |
| 1 | 0 | replace #1 (close A, open BTC) | 1→0 | 2 |
| 2 | 0 | replace #2 (close B, open SPY) | 1→0 | 2 |
| 3 | 0 | replace #3 (close BTC, open QQQ) | 1→0 | 2 |
| 4 | 0 | replace #4 (close SPY, open TSLA) | 1→0 | 2 |
| 5 | 0 | replace #5 (close QQQ, open AAPL) | 1→0 | 2 |

Resultado: 5 broker roundtrips innecesarios + audit log inflado con eventos POSITION_REPLACED.

## Fix (Sprint 18 patch)

Agregar un flag `did_replace_this_cycle` que se setea a `True` después del primer reemplazo exitoso, y se checkea antes de cada intento de reemplazo:

```python
approved = []
rejected = []
did_replace_this_cycle = False   # B020 fix
for h in hypotheses:
    ...
    if slots_left <= 0:
        replaced = False
        if (
            self.enable_position_replacement
            and self.position_repo is not None
            and not did_replace_this_cycle   # ← guard
        ):
            replaced = self._try_replace_position(...)
        if not replaced:
            ...reject...
            continue
        did_replace_this_cycle = True
        slots_left = 1
```

## Tests que cubren este bug

- `tests/test_risk_agent_sprint18.py::B020OneReplacementPerCycleTest::test_at_most_one_replacement_per_cycle`

## Lección de diseño

Cuando un loop permite "una acción X por iteración" pero la condición para X se re-evalúa cada iteración, siempre hay que llevar un flag explícito de "ya hice X". De lo contrario, en el siguiente ciclo se vuelve a disparar.

Es un anti-pattern clásico: estado implícito en una variable de loop en lugar de estado explícito en una bandera.