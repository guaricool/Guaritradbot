# Design System: Guaritradbot Dashboard

Este documento define la guía de diseño visual y el sistema semántico para la interfaz del dashboard de **Guaritradbot**, optimizado para el generador de interfaces Google Stitch y la consistencia del desarrollo frontend.

---

## 1. Visual Theme & Atmosphere

La interfaz de Guaritradbot está diseñada como un **cockpit de trading de alta densidad (Density: 8)**. El ambiente es clínico, de alta tecnología y bajo brillo (low-glare) para evitar la fatiga visual durante largas horas de monitoreo. 

Combina la sobriedad del minimalismo moderno con la urgencia y el dinamismo de los mercados financieros mediante acentos cromáticos de alto contraste.

---

## 2. Color Palette & Roles

El esquema de colores utiliza una base de grises oscuros azulados ("Carbon/Obsidian") junto con acentos de color calibrados para representar estados financieros críticos:

*   **Obsidian Void** (`#070a14`) — Fondo principal del canvas (body/background).
*   **Carbon Surface** (`#0c111e`) — Relleno de contenedores, tarjetas y paneles.
*   **Elevated Carbon** (`#131a2b`) — Modales, popovers y elementos sobre la superficie.
*   **Charcoal Steel** (`#1c2438`) — Bordes y líneas divisorias de 1px.
*   **Warm Cream** (`#f7f4ed`) — Texto principal y encabezados para máxima legibilidad.
*   **Steel Slate** (`#7d869e`) — Texto secundario, etiquetas, metadatos y descripciones.
*   **Warm Gold Accent** (`#e6a93b`) — Color primario de acento para CTAs, botones activos y estados destacados.
*   **Terminal Emerald (Gains)** (`#10b981`) — Indica P&L positivo (ganancias), posiciones en verde y estados activos correctos.
*   **Coral Crimson (Losses)** (`#ef6b5a`) — Indica P&L negativo (pérdidas), detenciones (`STOP_HIT`), alertas y errores críticos.

---

## 3. Typography Rules

*   **Display & Headlines:** `Geist` o `Satoshi` — Con espaciado de letras ajustado (`tracking-tight`), peso semibold/bold, y control del tamaño (no sobredimensionado). La jerarquía se define por peso y contraste de color (Cream vs. Steel Slate).
*   **Body Text:** `Geist` — Con altura de línea relajada (`leading-relaxed`) y un ancho máximo de 65 caracteres para legibilidad.
*   **Monospace (Stats & Metrics):** `JetBrains Mono` — Obligatorio para todos los valores numéricos (precios, balances, timestamps, cantidades, P&L). Previene el parpadeo visual (jitter) durante las actualizaciones de datos en tiempo real.
*   **Banned Fonts:** `Inter` y fuentes serif genéricas (`Times New Roman`, `Georgia`) están estrictamente prohibidas en el dashboard.

---

## 4. Component Stylings

*   **Buttons:**
    *   *Primary:* Relleno plano `Warm Gold` (`#e6a93b`) con texto oscuro (`#070a14`). Sin sombras ni brillos externos. Tactilidad de `-1px translateY` al hacer clic.
    *   *Ghost / Outline:* Fondo transparente con borde `Charcoal Steel` (`#1c2438`) y texto `Warm Cream` (`#f7f4ed`).
    *   *Danger:* Relleno `Coral Crimson` (`#ef6b5a`) con texto claro para botones destructivos (ej. "Panic Close").
*   **Cards:** Esquinas suavemente redondeadas (`0.5rem` / `8px`). Sin sombras de elevación, delimitadas exclusivamente por bordes finos de 1px (`#1c2438`). En áreas de alta densidad de datos, evitar las tarjetas en favor de separadores lineales.
*   **Inputs:** Relleno `Elevated Carbon` (`#131a2b`), borde `Charcoal Steel` (`#1c2438`). Al hacer foco, el borde cambia a `Warm Gold` con un anillo sutil.
*   **Loaders:** Shimmers esqueléticos que calcan las dimensiones del contenedor final. Quedan prohibidos los spinners circulares genéricos.

---

## 5. Layout & Grid Principles

*   **Bento Grid / Layout Asimétrico:** Evitar las filas de 3 tarjetas de igual tamaño. Usar distribuciones asimétricas para destacar visualmente las métricas principales (ej. balance neto y P&L total en bloques grandes) de las secundarias.
*   **Separación Espacial Limpia:** Ningún elemento debe solaparse ni usar posicionamientos absolutos que puedan colapsar. Cada componente ocupa su zona física definida.
*   **Diseño Responsivo:** En pantallas móviles (`< 768px`), las columnas se colapsan a una sola columna vertical.
*   **Tablas de Datos:** Estilo cebra sutil. Alineación a la derecha para celdas numéricas con tipografía monoespaciada para facilitar la comparación vertical de precios.

---

## 6. Motion & Interaction

*   **Micro-interacciones:** Transiciones fluidas en estados hover (`0.2s ease-out`) en botones y filas de tablas.
*   **Animación de Cambios:** Efecto flash muy sutil en las celdas de precios cuando el valor cambia: verde translúcido para subidas y rojo translúcido para bajadas, con fade-out rápido.
*   **Performance:** Animaciones limitadas a `transform` y `opacity` para aceleración por hardware.

---

## 7. Anti-Patterns (Banned AI Clichés)

*   **No Emojis:** Prohibido el uso de emojis dentro de la interfaz del dashboard (mantener un tono profesional de terminal de trading).
*   **No Pure Black:** No usar negro absoluto (`#000000`) para los fondos. Usar el tono `Obsidian Void` (`#070a14`) para mantener profundidad cromática.
*   **No Neon Shadow Glows:** Prohibidas las sombras difusas con colores de acento brillantes o morados que simulen luces de neón baratas.
*   **No Copywriting Genérico:** Evitar expresiones clichés de IA como "Elevate your trades", "Seamless automation" o "Unleash power". Usar etiquetas técnicas y directas.
