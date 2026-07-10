# B025 — Dashboard Silent on Paper Positions During Live Transition

**Severidad**: 🔴 Crítica (silent failure que lleva a ghost positions)
**Sprint**: 25 (2026-07-09)
**Componente**: `dashboard.py` + `main.py`
**Reportado por**: Carlos (2026-07-09, ~21:30)

## Síntomas

Carlos: "cuando cambio a live no me dice nada de las entradas en paper. y siguen alli."

Cuando el usuario togglea de paper a live desde el dashboard:
1. El bot sigue ejecutándose con `mandate.enabled=true` (del `mode_override.json`).
2. **Las paper positions siguen en `data_store/positions.json`** — no se avisa al usuario.
3. **El dashboard no muestra ninguna advertencia** sobre las paper positions.
4. El bot loguea "Bot started" sin mencionar las ghost positions.
5. El próximo SL/TP intentará cerrar las paper positions via broker live → orden de venta de un asset que NO existe en el exchange.

## Causa raíz (3 capas)

### Capa 1: `mode_override.json` solo cambia `mandate.enabled`, no `exchange.use_testnet`

```python
# main.py (Sprint 12)
if "mandate_enabled" in mode_override:
    config["mandate"]["enabled"] = bool(mode_override["mandate_enabled"])
# ↑ No toca config["exchange"]["use_testnet"]
```

Resultado: `mandate_being_enabled=True` pero `exchange_use_testnet=True` (default).
El check `is_live_attempt = mandate_being_enabled and not exchange_use_testnet` es False.
**El checklist del Sprint 22 NUNCA corre.**

### Capa 2: El bot loguea "Bot started" sin mencionar paper positions

El startup de `main.py` no listaba las paper positions existentes. Carlos tenía que abrir `data_store/positions.json` manualmente para verlas.

### Capa 3: El dashboard no muestra el estado de las paper positions

El dashboard cargaba `open_positions` y las mostraba en "Open Positions" pero sin:
- Advertir que son **paper** (no existen en el exchange)
- Alertar si `mandate_enabled=true` (live trading)
- Ofrecer un botón para limpiarlas

## Fix (Sprint 25)

### Fix 1: Banner prominent en el dashboard

Cuando hay paper positions Y mandate está enabled, se muestra un banner amarillo con pulse animation arriba del dashboard:

```html
<div class="paper-positions-warning">
  <div class="paper-warning-header">
    ⚠️ PAPER POSITIONS OPEN — Live trading is enabled
  </div>
  <div class="paper-warning-body">
    You have <b>N open paper position(s)</b> tracked in the local repo.
    These <b>do NOT exist on the live exchange</b>. The bot may try to
    close them via the live broker (which will fail or worse — sell
    assets you don't own).
  </div>
  <ul>...</ul>
  <div class="paper-warning-action">
    👇 Use the <b>"Clean Paper Positions"</b> button in the sidebar.
  </div>
</div>
```

### Fix 2: Botón "Clean Paper Positions" en el sidebar

One-click action que cierra todas las paper positions a `entry_price` (P&L=0):

```python
if st.button(f"🧹 Clean Paper Positions ({open_count_now})", ...):
    for p in _pp["positions"]:
        if p.get("closed_ts") is None:
            p["closed_ts"] = time.time()
            p["closed_price"] = p.get("entry_price", 0)
            p["close_reason"] = "MANUAL_CLEAN_PAPER"
    json.dump(_pp, _pp_path, f, indent=2)
    st.rerun()
```

### Fix 3: El bot loguea paper positions al startup

```python
# main.py
_open_paper = position_repo.count_open()
if _open_paper > 0:
    print(f"\n⚠️  {_open_paper} paper position(s) detected in repo:")
    for p in position_repo.open():
        print(f"   • {p.asset} {p.direction.upper()} qty={p.qty} @ ${p.entry_price:.2f}")
    print("These exist in the LOCAL REPO only — they do NOT exist on the live exchange.")
```

### Fix 4: Checklist corre también cuando hay paper positions (incluso en testnet)

```python
# main.py
mandate_being_enabled = bool(config.get("mandate", {}).get("enabled", False))
has_paper_positions = position_repo.count_open() > 0
is_live_attempt = mandate_being_enabled and (
    not exchange_use_testnet or has_paper_positions  # ← FIX: trigger on either
)
```

Si el checklist aborta, también escribe `mandate_enabled=false` al `mode_override.json` para que el dashboard refleje el cambio inmediatamente.

## Tests

Tests existentes (paper_to_live) siguen válidos. No se agregaron tests nuevos para B025 específicamente porque el behavior es principalmente UI (banner en dashboard) + interacción con mode_override.

Verificación manual:
1. Abrir dashboard con paper positions abiertas
2. Toggle "Mandate gate (LIVE trading)" ON
3. **Verificar**: aparece banner amarillo con pulse animation
4. **Verificar**: aparece botón "Clean Paper Positions" en sidebar
5. Click "Clean Paper Positions" → positions cerradas, banner desaparece
6. Toggle live otra vez → sin banner (ya no hay paper positions)

## Lección de diseño

**Cuando hay un toggle de modo (paper ↔ live), el bot debe:**
1. **Listar explícitamente** qué cambia (paper positions, balance, exposure).
2. **Advertir** sobre los riesgos de la transición.
3. **Ofrecer acción inmediata** (botón para limpiar, no solo esperar al checklist).
4. **Reflejar el cambio en todas las UIs** (dashboard + bot log).

El Sprint 22 checklist era el backstop, pero el feedback inmediato debe estar en el dashboard, no solo en el log del bot.

## Status

✅ **Cerrado**. Carlos puede ahora:
- Ver el banner amarillo con sus paper positions antes de ir a live
- Limpiar con un solo click
- El bot loguea las paper positions al startup
- El checklist corre tanto en live como cuando hay paper positions + mandate enabled

Ver [[../Sprints/Sprint_22_Paper_Live_Transition]] para el sprint original del checklist.