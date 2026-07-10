# B028 — Streamlit Console Warnings (Unfixable from App Code)

**Severidad**: 🟡 Menor (console noise, no funcional)
**Sprint**: 28 (2026-07-09)
**Componente**: `main.7994a814.js` (Streamlit's compiled JS, no nuestro código)
**Reportado por**: Carlos (2026-07-09, browser DevTools console)

## Síntomas

La consola del browser muestra 9+ warnings de Streamlit:

```
main.7994a814.js:2 Gather usage stats: true
main.7994a814.js:2 Unrecognized feature: 'ambient-light-sensor'.
main.7994a814.js:2 Unrecognized feature: 'battery'.
main.7994a814.js:2 Unrecognized feature: 'document-domain'.
main.7994a814.js:2 Unrecognized feature: 'layout-animations'.
main.7994a814.js:2 Unrecognized feature: 'legacy-image-formats'.
main.7994a814.js:2 Unrecognized feature: 'oversized-images'.
main.7994a814.js:2 Unrecognized feature: 'vr'.
main.7994a814.js:2 Unrecognized feature: 'wake-lock'.

component/streamlit_autorefresh.st_autorefresh/index.html:1
An iframe which has both allow-scripts and allow-same-origin for its sandbox
attribute can escape its sandboxing.
```

## Causa raíz

Estos warnings son **del bundle JS compilado de Streamlit 1.36** (`main.7994a814.js`), no de nuestro código. Streamlit:

- Activa `gatherUsageStats: true` por default → envía telemetría a Streamlit servers
- Hace feature detection de APIs experimentales de Chrome (ambient-light-sensor, battery, vr, wake-lock, etc.) → warnings cuando las APIs no están disponibles en el browser
- El componente `streamlit_autorefresh` usa un iframe con `sandbox="allow-scripts allow-same-origin"` → warning de escape de sandbox

**No podemos modificar** `main.7994a814.js` directamente — es un archivo compilado distribuido por Streamlit. Patchearlo sería frágil y se rompería con cada update de Streamlit.

## Fix (Sprint 28) — Parcial

### Lo que SÍ arreglé: usage stats

Crear `.streamlit/config.toml`:

```toml
[browser]
gatherUsageStats = false

[theme]
base = "dark"
primaryColor = "#4cc9f0"
backgroundColor = "#0a0e27"
secondaryBackgroundColor = "#141937"
textColor = "#e0e6ff"
```

Esto elimina el warning "Gather usage stats: true" y deshabilita el envío de telemetría a Streamlit servers (más privacidad + menos red).

### Lo que NO pude arreglar (limitaciones de Streamlit 1.36)

1. **8 "Unrecognized feature" warnings**: vienen del feature detection interno de Streamlit. Para eliminarlos, hay que:
   - Upgrade a Streamlit 1.40+ (mejoras en feature detection)
   - O parchear el bundle JS (frágil)

2. **"An iframe which has both allow-scripts and allow-same-origin"** (streamlit_autorefresh): el componente externo tiene este patrón de sandbox. Para arreglarlo:
   - Reemplazar `streamlit_autorefresh` con un custom refresh (más trabajo)
   - Upgrade a versión más reciente de `streamlit-autorefresh`

## Lección de diseño

Cuando integras con un framework externo (Streamlit, React, Vue):
- **Acepta** que algunos warnings vendrán del framework mismo.
- **Maximiza** lo que puedes controlar via config y meta tags.
- **Documenta** las limitaciones (este archivo).
- **No intentes parchar** archivos compilados — es frágil.

## Workaround futuro (si Carlos quiere 100% clean console)

Opciones:
1. Upgrade a Streamlit 1.40+ (~5 min, requiere tests de regresión)
2. Reemplazar `streamlit_autorefresh` con un custom `st.rerun()` + setTimeout (1-2 horas)
3. Parchar `main.7994a814.js` post-install (frágil, no recomendado)

## Status

✅ **Cerrado parcialmente**. Usage stats arreglado. Otros warnings son limitaciones documentadas de Streamlit 1.36.