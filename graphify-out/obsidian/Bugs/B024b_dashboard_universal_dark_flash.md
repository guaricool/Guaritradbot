# B024b — Universal Dark Flash on Streamlit Interactive Widgets

**Severidad**: 🟡 Menor (cosmético / UX)
**Sprint**: 22 patch (2026-07-09)
**Componente**: `dashboard.py` (CSS global)
**Reportado por**: Carlos (2026-07-09, segundo reporte)

## Síntomas (continuación de B024)

Después del fix B024 (CSS específico para sliders), Carlos reportó:
> "sigue cambiando lo oscuro y brillo"

El flash oscuro continúa en:
- **`st.button`** widgets (sidebar, "Save risk settings", "Save Quick Risk")
- **`st.checkbox`** widgets (Mandate gate toggle, Show audit feed, etc.)
- Otros elementos interactivos con focus state

## Causa raíz ampliada

B024 atacaba solo `[data-testid="stSlider"]`. Pero el problema es **genérico**:
- `st.button` → tiene `background-color` oscuro en `:active`
- `st.checkbox` → tiene focus ring oscuro del browser
- Cualquier elemento `:focus-visible` → outline nativo del browser

El CSS de Streamlit default (en `data-testid` específicos) define dark focus/active states que NO matcheaban los selectores que usé en B024.

## Fix (Sprint 22 patch)

CSS universal que ataca TODOS los widgets interactivos:

```css
/* === ALL st.button: remove dark active state === */
div[data-testid="stButton"] button {
  transition: all 0.15s ease !important;
}
div[data-testid="stButton"] button:focus,
div[data-testid="stButton"] button:active,
div[data-testid="stButton"] button:focus-visible {
  outline: none !important;
  box-shadow: 0 0 0 2px rgba(76, 201, 240, 0.4) !important;
}
div[data-testid="stButton"] button:hover:not(:disabled) {
  filter: brightness(1.05);
}

/* === ALL st.checkbox: remove dark focus ring === */
div[data-testid="stCheckbox"] label:focus-within {
  outline: none !important;
}
div[data-testid="stCheckbox"] input[type="checkbox"]:focus {
  outline: none !important;
  box-shadow: 0 0 0 2px rgba(76, 201, 240, 0.4) !important;
}

/* === Universal: prevent any focus flash on any interactive element === */
button:focus-visible,
[role="button"]:focus-visible,
[tabindex]:focus-visible {
  outline: none !important;
}
```

Lo que hace:
1. **Buttons**: outline → none, box-shadow sutil con brand color (en vez del default oscuro).
2. **Checkboxes**: focus ring → sutil box-shadow azul en vez del outline nativo.
3. **Universal**: cualquier elemento con focus-visible → sin outline.

## Resultado esperado

- Click en cualquier botón: leve highlight azul (sutil), sin flash oscuro.
- Toggle de checkbox: leve glow azul, sin ring oscuro.
- Tab navigation en dashboard: sin outlines distractores.

## Lección aprendida

Cuando arreglas un bug visual en Streamlit/Dashboards:
1. **El primer fix suele ser insuficiente** porque hay múltiples widgets con el mismo problema.
2. **Usar selectores universales** (`button:focus-visible`, `[role="button"]`) es más robusto que ir widget por widget.
3. **Testear visualmente después de cada fix**, no solo confiar en que el código "debería" funcionar.

## B024 vs B024b

| Aspecto | B024 | B024b |
|---|---|---|
| Scope | Solo sliders | TODOS los widgets |
| Selectores | `[data-testid="stSlider"]` | `button:focus-visible` (universal) |
| Resultado | Incompleto | Completo (esperado) |

Ver [[../Sprints/Sprint_22_Paper_Live_Transition]] para el contexto del sprint.