# B026 — NameError: `positions` undefined in sidebar block

**Severidad**: 🟠 Media (crash al abrir el dashboard)
**Sprint**: 26 patch (2026-07-09)
**Componente**: `dashboard.py` (sidebar)
**Reportado por**: Carlos (2026-07-09)

## Síntomas

Al abrir el dashboard:
```
NameError: name 'positions' is not defined
File "/app/dashboard.py", line 1735, in <module>
    open_count_now = len([p for p in positions if p.get("closed_ts") is None])
                                     ^^^^^^^^^
```

El dashboard crashea completamente al renderizar el sidebar.

## Causa raíz

El Sprint 25 agregó el botón "🧹 Clean Paper Positions" en el sidebar. Ese botón referencia `positions`, pero `positions` se carga **DESPUÉS** del sidebar en el render flow del dashboard.

```python
# Sidebar (línea ~1700) — se renderiza PRIMERO
with st.sidebar:
    ...
    if st.button("🧹 Clean Paper Positions"):
        open_count_now = len([p for p in positions ...])  # ← NameError!

# Main area (línea ~1795) — `positions` se define AQUÍ
positions = _load_positions_cached()
```

## Fix (Sprint 26 patch)

Cargar las positions directamente desde el JSON dentro del bloque del sidebar, en una variable local:

```python
# B026 fix: load positions from disk directly to avoid NameError
# when `positions` variable hasn't been declared yet (this sidebar
# block runs BEFORE the main render).
_sidebar_pp = _load_json("data_store/positions.json")
_sidebar_open = (
    len([p for p in _sidebar_pp.get("positions", []) if p.get("closed_ts") is None])
    if _sidebar_pp else 0
)
if _sidebar_open > 0:
    if st.button(f"🧹 Clean Paper Positions ({_sidebar_open})", ...):
        ...
```

Y actualizar las referencias (`open_count_now` → `_sidebar_open` en el resto del bloque).

## Tests

No agregué tests específicos (es un fix de placement). Los tests existentes (83/83) siguen pasando.

Verificación manual:
1. Recargar el dashboard
2. El sidebar debe renderizar sin error
3. Si hay paper positions, el botón "🧹 Clean Paper Positions" debe aparecer con el count correcto

## Lección de diseño

Cuando agregas UI a un sidebar en Streamlit:
- Las variables globales (como `positions`) se cargan en el render principal, no en el sidebar.
- El sidebar se renderiza **antes** que el main area.
- Si necesitas data en el sidebar, **léela directamente** desde su source (JSON, cache, etc.) en una variable local.

Alternativa (más limpia pero más invasiva): refactor para que `positions` se cargue al inicio del archivo, antes de cualquier `with st.sidebar:`. Pero eso requiere mover código que afecta muchos otros lugares.

## Status

✅ Cerrado. El dashboard ahora renderiza sin NameError.