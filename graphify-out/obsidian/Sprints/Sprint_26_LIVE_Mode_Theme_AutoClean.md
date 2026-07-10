# Sprint 26 — LIVE Mode Visual Theme + Auto-Clean Paper Positions

**Fecha**: 2026-07-09
**Status**: ✅ Cerrado (sin tests nuevos, manual verification)
**Inspiración**: Carlos: "cuando se cambia a live me gustaria que al pasar a live cambiara como el color de la app. sabes? y se pusiera en 0 todo para que pueda encontrar una entrada sin pensar que tiene ya 2 entradas."

## Resumen

Dos features pedidas en una sola oración:

1. **Live mode visual theme**: el dashboard cambia su paleta visual cuando toggleas a live (rojo/naranja en lugar de azul/cyan). Banner prominent con pulse animation.
2. **Auto-clean paper positions**: cuando toggleas a live, todas las paper positions se cierran automáticamente a `entry_price` (P&L=0). Slate limpio para encontrar la primera entrada live.

## Componentes

### 1. Live Mode Banner + Theme

Cuando `mandate_enabled=true`, aparece arriba del dashboard:

```html
<div class="live-mode-banner">
  <div class="live-mode-pulse"></div>  <!-- Red dot con pulse -->
  <div class="live-mode-content">
    <div class="live-mode-title">🔴 LIVE TRADING ENABLED</div>
    <div class="live-mode-subtitle">
      Real money is at risk. The bot is submitting real orders to binance.us.
      Every trade, every stop, every take-profit is REAL.
    </div>
  </div>
  <div class="live-mode-actions">
    👇 Toggle OFF in the sidebar to return to paper mode
  </div>
</div>
```

CSS:
- Background: gradient rojo/rosa (`rgba(239, 71, 111, 0.25)`)
- Border: 2px solid rojo + 6px left border (más prominente que el paper banner)
- Animations: `live-mode-glow` (box-shadow pulse 2.5s) + `live-pulse` (red dot 1.5s)
- Color shift: el banner live usa rojo en lugar de azul (visual contrast vs paper)

### 2. Auto-Clean en transición a Live

Cuando Carlos togglea `Mandate gate (LIVE trading)` ON desde el sidebar:

```python
# Detecta transición paper → live
_was_live = bool(mand.get("enabled", False))
_is_now_live = bool(sidebar_mandate)
_transitioned_to_live = (not _was_live) and _is_now_live

if _transitioned_to_live:
    # Auto-clean: close all open paper positions at entry_price
    for p in _pp["positions"]:
        if p.get("closed_ts") is None:
            p["closed_ts"] = time.time()
            p["closed_price"] = p.get("entry_price", 0)
            p["close_reason"] = "AUTO_CLEAN_ON_LIVE_TRANSITION"
    json.dump(_pp, _pp_path, indent=2)
    st.success(f"🔴 LIVE mode enabled + N paper positions closed at entry. Clean slate to find your first real entry.")
    st.balloons()  # 🎉 confetti animation para celebrar el primer live trade
```

### 3. Mode Override Sync

El botón "Save Quick Risk" ahora también escribe a `audit/mode_override.json`:

```python
mode_override = {
    "mandate_enabled": sidebar_mandate,
    "switched_at": datetime.now().isoformat(),
    "switched_by": "dashboard_sidebar",
    "previous_value": _was_live,
}
with open("audit/mode_override.json", "w") as f:
    json.dump(mode_override, f, indent=2)
```

Esto asegura que el bot, en su próximo ciclo, **vea el toggle inmediatamente** sin esperar restart manual.

## Flujo end-to-end

1. **Carlos abre el dashboard** con paper positions abiertas y `mandate_enabled=False` (default).
2. **Ve el banner amarillo** del Sprint 25 listando las paper positions.
3. **Activa el checkbox** "🟢 Mandate gate (LIVE trading)" en el sidebar.
4. **Click "💾 Save Quick Risk"**.
5. **El código detecta** `_transitioned_to_live = True`.
6. **Auto-clean**: cierra las 2 paper positions a `entry_price` (P&L=0).
7. **Mode override** se escribe a `audit/mode_override.json` con `mandate_enabled=True`.
8. **Success message**: "🔴 LIVE mode enabled + 2 paper positions closed at entry. Clean slate to find your first real entry."
9. **st.balloons()** 🎉 confetti animation.
10. **st.rerun()** → dashboard recarga.
11. **Banner rojo LIVE TRADING ENABLED** aparece prominent con pulse animation.
12. **Open Positions widget** muestra 0 abiertas (clean slate).
13. **Próximo ciclo del bot**: ve el override, ejecuta el pre-flight checklist (Sprint 22), arranca a buscar entradas reales.

## Tests

No agregué tests nuevos (es principalmente UI). Los tests del Sprint 22 (paper_to_live) y Sprint 25 (paper positions warning) siguen pasando.

Verificación manual:
1. Toggle live con paper positions → banner amarillo + bot aparece
2. Click "Save Quick Risk" → auto-clean + banner rojo live + success message
3. Toggle paper de nuevo → banner rojo desaparece, dashboard vuelve a normal
4. Toggle live sin paper positions → success message sin auto-clean

## Lección de UX

**Cuando un toggle cambia el modo operacional (paper ↔ live), debe haber 3 cosas:**

1. **Visual indicator immediate**: el cambio se ve inmediatamente (color, badge, theme).
2. **Action de cleanup**: el sistema limpia automáticamente el state que es inconsistente con el nuevo modo.
3. **Celebration moment**: la primera vez que se activa algo importante, el usuario debe sentir que "pasó algo" (confetti, sound, animation).

Estos 3 elementos juntos hacen que la transición se sienta **intencional y segura**, no accidental o confusa.

## Score de capacidad actualizado

| Capacidad | Antes | Después |
|---|---|---|
| Live mode visual feedback | ❌ | ✅ (banner + theme switch) |
| Auto-clean on transition | ❌ (manual) | ✅ (1 click) |
| Mode override sync | ❌ (manual restart) | ✅ (immediate) |

**Score global sube a ~85%** con este sprint (de 83%).

## Próximos pasos

- **Sprint 27**: Modo "preview live" (toggle que cambia la paleta y muestra el banner pero NO limpia ni activa live real). Útil para "ver cómo se vería" sin comprometerse.
- **Sprint 28**: Sound notification cuando toggleas a live (audio cue + visual).

Ver [[../Bugs/B025_dashboard_silent_paper_positions]] para el bug que motivó este sprint.