# B024 — Dashboard Slider Dark Flash

**Severidad**: 🟡 Menor (cosmético / UX)
**Sprint**: 22 patch (2026-07-09)
**Componente**: `dashboard.py` (Risk Settings sliders)
**Reportado por**: Carlos (2026-07-09)

## Síntomas

Al hacer click o arrastrar cualquier slider de la sección "Risk Settings — Editable" abajo del dashboard:

- El área circundante se oscurece por un instante.
- El thumb (la bolita que se arrastra) muestra un focus ring oscuro.
- Después de soltar el slider, vuelve al brillo normal.

Es el mismo síntoma visual que B023 pero en otra zona del dashboard.

## Causa raíz

Streamlit renderiza `st.slider` usando el componente baseweb internamente, que a su vez usa `<input type="range">` HTML nativo (o un equivalente). Cuando el usuario interactúa con el slider:

1. El browser aplica un `:focus` ring al elemento enfocado.
2. baseweb también cambia el color del track cuando está "active".
3. Eso causa un flash oscuro transitorio en la zona.

## Fix (Sprint 22 patch)

CSS para neutralizar el efecto:

```css
div[data-testid="stSlider"] { background: transparent !important; }
div[data-testid="stSlider"] > div { background: transparent !important; }
div[data-testid="stSlider"] [role="slider"] {
  background-color: #4cc9f0 !important;
  border: none !important;
  box-shadow: 0 0 0 4px rgba(76, 201, 240, 0.15) !important;
  outline: none !important;
}
div[data-testid="stSlider"] [role="slider"]:hover,
div[data-testid="stSlider"] [role="slider"]:focus,
div[data-testid="stSlider"] [role="slider"]:active {
  background-color: #4cc9f0 !important;
  box-shadow: 0 0 0 6px rgba(76, 201, 240, 0.25) !important;
  outline: none !important;
}
div[data-testid="stSlider"] [data-baseweb="slider"] > div {
  background: transparent !important;
}
```

Lo que hace:
- Quita el background del container (no se ve nada oscuro detrás).
- Fija el color del thumb a nuestro brand color `#4cc9f0`.
- Elimina `outline` (focus ring nativo del browser).
- Mantiene el thumb con un box-shadow sutil (que sí queremos para feedback visual).

## Resultado esperado

- Click/arrastre en slider: thumb se mantiene azul con un glow sutil más grande.
- No hay flash oscuro en el área circundante.
- El resto del dashboard (incluyendo el track) se ve igual.

## Lección de diseño

`st.slider` y `st.button` ambos tienen "active states" oscuros nativos del browser/framework. Si tu UI espera "no visual artifact" durante interacción, hay que:

1. Aplicar CSS para neutralizar los pseudo-estados (`:hover`, `:focus`, `:active`).
2. O reemplazar con widgets que NO tengan active state (`st.radio` para selección, `st.number_input` para números).

Para nuestro caso: el UX de arrastrar un slider es valioso (Carlos puede ajustar riesgo visualmente), así que preferimos CSS fix sobre reemplazo.

## Nota para futuras versiones de Streamlit

En Streamlit 1.40+ se introdujo `st.pills` y `st.segmented_control` con custom theming. Si upgradeamos a esa versión, podemos usar esos widgets que tienen theming más limpio. Por ahora, con 1.36, el CSS fix es la mejor opción.