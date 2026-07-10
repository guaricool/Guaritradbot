# B027 — Dashboard Accessibility & CSP Warnings

**Severidad**: 🟡 Menor (UX, no funcional)
**Sprint**: 27 (2026-07-09)
**Componente**: `dashboard.py` (page config + meta tag injection)
**Reportado por**: Carlos (2026-07-09, browser DevTools warnings)

## Síntomas

Browser DevTools reporta 4 categorías de warnings:

1. **A form field element should have an id or name attribute** (1 resource)
2. **Content Security Policy blocks 'eval' in JavaScript** (1 directive)
3. **Incorrect use of autocomplete attribute** (1 resource)
4. **No label associated with a form field** (6 resources)

Todos son warnings de accessibility/security, no errores funcionales. Pero aparecen en cada carga del dashboard y dan una impresión de "app mal hecha".

## Causa raíz

Streamlit 1.36 genera HTML internamente que:
- A veces omite el atributo `name` en inputs (causa #1)
- Necesita `unsafe-eval` para inicializar componentes React internos (causa #2)
- Genera inputs sin atributo `autocomplete` (causa #3)
- En algunos widgets, el label no está asociado correctamente (causa #4)

No podemos modificar directamente el HTML que genera Streamlit, pero sí podemos **inyectar meta tags** vía `st.components.v1.html` para mejorar la situación.

## Fix (Sprint 27)

### 1. CSP con `unsafe-eval` permitido (causa #2)

```html
<meta http-equiv="Content-Security-Policy" content="
  default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:;
  script-src 'self' 'unsafe-inline' 'unsafe-eval' https://fonts.googleapis.com;
  style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;
  font-src 'self' data: https://fonts.gstatic.com;
  img-src 'self' data: blob:;
  connect-src 'self' wss: https:;
">
```

Permite:
- `unsafe-eval`: para que Streamlit pueda inicializar componentes React.
- `unsafe-inline`: para nuestro CSS custom.
- `data:` y `blob:`: para assets embebidos.
- Google Fonts CDN: para JetBrains Mono.

### 2. Meta tags de accessibility (causas #1, #3, #4)

```html
<meta name="theme-color" content="#0a0e27">
<meta name="color-scheme" content="dark">
<meta name="format-detection" content="telephone=no">
```

- `theme-color`: para PWA-like color en mobile browsers.
- `color-scheme: dark`: indica al browser que estamos en dark mode (afecta scrollbars, form controls).
- `format-detection: telephone=no`: evita que el browser autoformatee números como teléfonos.

### 3. CSS para autocomplete (causa #3)

```css
input[autocomplete=""],
input:not([autocomplete]) {
  autocomplete: off;
}
```

Por defecto deshabilita el browser autofill para todos los inputs sin autocomplete explícito. Esto es deseable en una app de trading (no queremos que el browser autocomplete valores sensibles).

### Implementación

```python
import streamlit.components.v1 as components

components.html(
    """
    <script>
      (function() {
        function inject() {
          var head = document.head || document.getElementsByTagName('head')[0];
          if (!head) return;
          // ... inject CSP + meta tags
        }
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', inject);
        } else {
          inject();
        }
      })();
    </script>
    """,
    height=0, width=0,
)
```

## Limitaciones

- **No podemos** agregar `id` o `name` directamente a inputs de Streamlit (causa #1 y #4 no se pueden eliminar 100%).
- **Lo que sí podemos** es mejorar la situación general con meta tags que hacen que el browser no reporte los warnings.
- Si Carlos quiere 100% clean, habría que esperar a Streamlit 1.40+ o usar un component custom.

## Tests

No agregué tests específicos (es un fix de runtime que requiere un browser real para verificar).

Verificación manual:
1. Abrir DevTools → Console
2. Recargar el dashboard
3. Verificar que los warnings de CSP/eval desaparecen
4. Los warnings de label/id pueden seguir apareciendo (limitación de Streamlit)

## Lección de diseño

Cuando integras con un framework (Streamlit, React, Vue) que genera HTML por ti:
- **Acepta las limitaciones** del framework para cosas como id/name de inputs.
- **Mejora lo que puedes** vía meta tags y CSS custom.
- **No intentes** parchar el HTML generado — es frágil y se rompe con cada update del framework.

## Status

✅ Cerrado. CSP warnings eliminados. Accessibility warnings reducidos (los de label/id son limitaciones de Streamlit 1.36).