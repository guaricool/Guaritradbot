# B018 — Phantom Exposure Lockup

**Severidad**: 🔴 Crítico
**Sprint**: 18
**Reportado por**: Audit Team (Finance & Risk)
**Componente**: `MandateGate._open_exposure_usd()` en `src/safety/mandate_gate.py`

## Síntomas

- Después de 5 trades round-trip (cada uno abierto y cerrado), el bot queda bloqueado permanentemente.
- `Mandate Gate` rechaza todos los trades nuevos con `exposure_cap:$100>$100`.
- Realmente NO hay exposición abierta, pero el bot cree que sí.

## Causa raíz

El método `_open_exposure_usd()` original iteraba sobre TODOS los eventos del audit ledger:

```python
def _open_exposure_usd(self) -> float:
    rows = self.audit.read_all()
    exposure = 0.0
    for r in rows:
        if r.get("event_type") == "TRADE_FILLED":     # ← solo suma
            qty = float(r.get("filled_qty", 0))
            price = float(r.get("fill_price", 0))
            notional = qty * price
            exposure += notional if side == "long" else -notional
    return abs(exposure)
```

NUNCA restaba cuando encontraba `TRADE_CLOSED`. Resultado: `exposure` crece monotónicamente.

Trace:
1. Trade 1 LONG $20 → TRADE_FILLED → exposure += $20 = $20
2. Trade 1 cierra TP → TRADE_CLOSED → (ignorado)
3. Trade 2 LONG $20 → TRADE_FILLED → exposure += $20 = $40
4. Trade 2 cierra SL → TRADE_CLOSED → (ignorado)
5. ...
6. Trade 5 → exposure = $100 = `max_total_exposure_usd`
7. Trade 6 → Mandate Gate: `$100 + $20 > $100` → BLOQUEADO

Y así para siempre. El bot se vuelve zombie.

## Fix (Sprint 18)

Opción A (preferida): usar `PositionRepository` directamente. Es la fuente de verdad — posiciones realmente abiertas en disco.

```python
def _open_exposure_usd(self) -> float:
    if self.position_repo is not None:
        return self.position_repo.total_exposure_usd()
    # ... audit fallback (corregido abajo)
```

Opción B (fallback sin position_repo): corregir el scan del audit ledger para que `TRADE_CLOSED` REMUEVA la posición del map:

```python
for r in rows:
    et = r.get("event_type")
    pid = r.get("position_id")
    if not pid:
        continue
    if et == "POSITION_OPENED":
        open_notional[pid] = float(r.get("notional_usd", 0))
    elif et == "TRADE_CLOSED":
        open_notional.pop(pid, None)   # ← removía correctamente
return sum(open_notional.values())
```

## Tests que cubren este bug

- `tests/test_mandate_gate_sprint18.py::PhantomExposureTest::test_exposure_zero_after_round_trip_trades`
- `tests/test_mandate_gate_sprint18.py::PhantomExposureTest::test_exposure_with_open_positions`
- `tests/test_mandate_gate_sprint18.py::PhantomExposureTest::test_legacy_audit_only_path_correctly_subtracts`

## Wiring en main.py

`MandateGate` ahora se construye con `position_repo`:

```python
def _build_mandate(config, audit, position_repo=None):
    ...
    return (MandateGate(mc, audit_ledger=audit, position_repo=position_repo), mc)
```

Y se llama desde main.py como:
```python
mandate_gate, mandate_cfg = _build_mandate(config, audit, position_repo=position_repo)
```