# B016 — position_id con timestamp-only colisionaba

**Severidad**: 🟡 menor (corner case, descubierto en test)

## Síntomas

Test del PositionMonitor (Sprint 2) creaba 3 posiciones en el
mismo milisegundo. Las 3 tenían el mismo `position_id`. Cuando
el monitor cerraba BTC-USD, `close_position()` buscaba `position_id`
y cerraba la primera que encontraba — la de GLD o USO, no la de BTC.

## Causa

```python
position_id: str = field(
    default_factory=lambda: f"pos_{int(time.time()*1000)}"
)
```

`time.time()` tiene resolución de microsegundos pero en máquinas
rápidas + tests con fixtures, dos llamadas en el mismo proceso
pueden coincidir.

## Fix (Sprint 2, commit `a2981bd`)

```python
import uuid

position_id: str = field(
    default_factory=lambda: f"pos_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
)
```

`uuid4().hex[:8]` = 8 chars hex = 16^8 = 4 mil millones de
combinaciones por milisegundo. Imposible colisionar.

## Ver también

- [[Modules/Position_Repository]]
- [[Sprints/Sprint_2_Position_Tracking]]
- [[Bugs_Index]]
