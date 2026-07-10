# B023 — Dashboard Filter Button Dark Flash

**Severidad**: 🟡 Menor (cosmético / UX)
**Sprint**: 18 (revisión post-sprint)
**Componente**: `dashboard.py` (Smart Signals filter chips)
**Reportado por**: Carlos (usuario, 2026-07-09)

## Síntomas

Al hacer click en cualquier chip de filtro ("ALL", "LONG", "SHORT", "HIGH-CONF", "LOW-CONF") en el panel "Smart Signals":

- Toda el área de arriba (incluyendo el chip y el botón de Streamlit) se oscurece por un instante.
- Después vuelve al brillo normal.

El efecto es muy visible y se siente "buggy", aunque no rompe ninguna funcionalidad.

## Causa raíz

El dashboard tenía **DOS elementos visuales para lo mismo**:

1. **Chips HTML decorativos** (`<span class="chip">`) que muestran el filtro activo según `st.session_state.signal_filter`.
2. **5 botones `st.button()` separados** abajo en `fcol1..fcol5` que son los controles interactivos reales.

Los chips HTML NO son clickeables — son puros spans. Los botones `st.button` SÍ son clickeables pero usan los estilos default de Streamlit, los cuales aplican un fondo oscuro "active/pressed" durante el instante entre el click y el `st.rerun()`.

Ese dark flash es **el comportamiento nativo de los botones de Streamlit**. Es correcto y estándar. Pero como los chips HTML están justo arriba y se ven "clickeables", el flash crea confusión visual.

## Fix

Reemplazar los 5 `st.button()` con un único `st.radio` horizontal con `label_visibility="hidden"`:

```python
_chip_options = ["ALL", "long", "short", "high", "low"]
new_filter = st.radio(
    "Signal filter",
    options=_chip_options,
    format_func=lambda v: _chip_labels[v],
    index=_chip_options.index(st.session_state.signal_filter),
    key="signal_filter_radio",
    horizontal=True,
    label_visibility="hidden",
)
```

Y estilizar el radio con CSS para que se vea como los chips HTML existentes:

```css
div[data-testid="stRadio"][role="radiogroup"] label {
  background: rgba(26, 31, 58, 0.5);
  border: 1px solid #2a3050;
  border-radius: 999px;
  /* ...chip styling... */
}
div[data-testid="stRadio"][role="radiogroup"] label > div:first-child {
  display: none;  /* hide the radio circle */
}
```

### Por qué esto funciona

- `st.radio` NO tiene un "active state" oscuro. El click es instantáneo — no hay flash.
- Mantenemos los chips HTML arriba como indicador visual bonito del estado actual.
- El radio real está debajo pero con CSS custom se ve idéntico a los chips.
- El usuario ve dos filas de "chips" que en realidad son el mismo control.

## Por qué NO usar `st.segmented_control` o `st.pills`

Esos widgets fueron agregados en Streamlit 1.40+ (septiembre 2024). El proyecto usa 1.36.0 (julio 2024). Upgrading no es trivial porque hay cambios breaking en otros componentes.

`st.radio` con CSS custom es la solución más limpia para esta versión.

## Resultado

- ✅ Click instantáneo, sin dark flash
- ✅ Mantiene la estética de chips (border-radius, colores, font)
- ✅ Mantiene el indicador visual "active" del chip HTML de arriba
- ✅ Funciona en la versión actual de Streamlit sin upgrade

## Lección de diseño

Cuando uses widgets nativos de Streamlit con CSS custom para que se vean diferente al default, **siempre considera los estados de interacción**:
- Default state (cómo se ve cuando no hay interacción)
- Hover state (mouse encima)
- Active/pressed state (clickeando)
- Focus state (teclado)

Los botones default tienen dark active state. Los radios NO. Si tu UI espera "click silencioso", evita botones.

Otra opción futura: usar componentes custom con `streamlit.components.v1.html` para tener control total sobre los estados, pero eso requiere JavaScript y complica el ciclo de vida del componente.