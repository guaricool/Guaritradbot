# Bugs Index

31 bugs encontrados y corregidos. Severidad: 🔴 crítico (rompía runtime) | 🟠 medio | 🟡 menor

| ID | Sev | Sprint | Bug | Fix |
|----|-----|--------|-----|-----|
| [[Bugs/B001_emit_vs_publish]] | 🔴 | 0 | `EventBus.emit()` no existía → crash en cada fill | Cambio `emit` → `publish` |
| [[Bugs/B002_env_keys_mismatch]] | 🔴 | 0 | `broker.py` leía `EXCHANGE_API_KEY` pero `.env.example` declaraba `BINANCE_*` | Renombrar keys en broker.py |
| [[Bugs/B003_input_blocking_docker]] | 🔴 | 0 | `input()` bloqueante en `ExecutionNode` rompía daemon en Docker | try/except `EOFError` + publish `ORDER_PENDING_APPROVAL` |
| [[Bugs/B004_rsi_sma_instead_of_wilder]] | 🔴 | 0 | RSI usaba SMA en vez de Wilder (EMA con α=1/14) | Reemplazo con `_wilder_rsi()` |
| [[Bugs/B005_macd_state_vs_cross]] | 🟠 | 0 | MACD comparaba estado (siempre long en tendencias) en vez de cruce | Detección de cruce explícita |
| [[Bugs/B006_stop_loss_hardcoded_5]] | 🔴 | 0 | Stop loss = $5 hardcoded (BTC: 0.008%, USO: 4.5%) | ATR-based: `entry ± k*ATR` |
| [[Bugs/B007_atr_22x_wilder_missing]] | 🟠 | 0 | No se calculaba ATR (era $5 fijo) | `_atr()` con Wilder smoothing |
| [[Bugs/B008_tf_map_4h_60m]] | 🟠 | 0 | `tf_map["4h"] = "1h"` silenciaba resample | Resample 60m → 4h vía `_resample_ohlcv()` |
| [[Bugs/B009_signal_generate_never_flat]] | 🟠 | 0 | `generate_vectorized_signals` siempre 1/-1 (sin FLAT) | FLAT por default + forward-fill |
| [[Bugs/B010_win_rate_misleading]] | 🟠 | 4 | `win_rate = bars_positivas / bars_totales` (confundía días/trades) | `winning_trades / total_trades` (Sprint 4) |
| [[Bugs/B011_num_trades_counted_bars]] | 🟠 | 4 | `num_trades = barras_con_retorno ≠ 0` | Trade detection real (entry/exit pairs) |
| [[Bugs/B012_run_reoptimization_placeholder]] | 🟠 | 5 | `run_reoptimization()` era `log.info("complete")` | HyperoptManager.optimize + inject params (Sprint 5) |
| [[Bugs/B013_execution_node_disconnected]] | 🔴 | 0 | `ExecutionNode` suscrito a `ORDER_APPROVED` pero nadie emitía | `ExecutionAgent.simulate_execution` publica ahora |
| [[Bugs/B014_market_data_bool_dataframe]] | 🟠 | 0 | `df = df_4h or df_1h` → `bool(df)` ambiguo para DataFrames | `if df is None or len(df)==0: df = df_1h` |
| [[Bugs/B015_venv_deps_missing]] | 🟠 | 0 | venv sin `yaml`, `schedule`, `streamlit`, `dotenv` | Documentar workaround (system python) |
| [[Bugs/B016_pos_id_uuid_collision]] | 🟡 | 2 | `position_id` con timestamp-only → colisiones simultáneas | Añadir uuid suffix |
| [[Bugs/B017_micro_account_death_loop]] | 🔴 | 18 | Auto-adjust solo disparaba con `max_notional < min_order`, no con `notional < min_order` → cuenta $20 muerta | Auto-adjust ahora dispara con cualquier `notional < min_order`, log reason (`max_cap_below_min_order` vs `risk_below_min_order`) |
| [[Bugs/B018_phantom_exposure_lockup]] | 🔴 | 18 | `_open_exposure_usd()` sumaba TRADE_FILLED sin restar TRADE_CLOSED → exposición crecía sin bound → Mandate Gate bloqueaba todo después de 5 trades round-trip | Usa `PositionRepository.total_exposure_usd()`; audit-fallback ahora sí resta closes |
| [[Bugs/B019_punished_for_trying]] | 🔴 | 18 | `_daily_loss_usd()` sumaba `risk_usd` teórico de TRADE_APPROVED → 5 trades winners ($1 risk c/u) disparaban kill switch 24h | Suma `realized_pnl` real de TRADE_CLOSED; solo cuenta pérdidas realizadas |
| [[Bugs/B020_replacement_loop]] | 🔴 | 18 patch | `validate_and_size` podía hacer N replacements consecutivos en un ciclo (close+open N veces si N hipótesis) → broker roundtrips innecesarios, audit inflado | Flag `did_replace_this_cycle` limita a 1 replacement por `validate_and_size()` call |
| [[Bugs/B021_phantom_pnl_replacement]] | 🔴 | 18 patch | `_try_replace_position` usaba `entry_price` como fallback cuando no había precio fresco → cerraba a breakeven falso, audit corrupto | Abortar el replacement si no hay precio fresco; dejar que PositionMonitor cierre después |
| [[Bugs/B022_smart_take_dead_code]] | 🔴 | 18 patch | `StrategyAgent` nunca emitía eventos `HYPOTHESIS_GENERATED` al audit → `check_with_signals` siempre recibía `signals=[]` → SMART_PROFIT_TAKE nunca se activaba | StrategyAgent ahora acepta `audit` y emite eventos con strength derivado |
| [[Bugs/B023_dashboard_filter_button_flash]] | 🟡 | 18 patch | 5 botones `st.button()` separados para los filtros de Smart Signals causaban un dark flash nativo de Streamlit al hacer click | Reemplazados por `st.radio` horizontal con CSS custom para verse como chips |
| [[Bugs/B024_dashboard_slider_dark_flash]] | 🟡 | 22 patch | Click/arrastre en los 6 sliders de Risk Settings causaba un dark flash del browser/Streamlit (focus ring + track color change) | CSS neutraliza `:focus` y `:active` states, mantiene thumb con brand color y box-shadow |
| [[Bugs/B024b_dashboard_universal_dark_flash]] | 🟡 | 22 patch | B024 solo atacaba sliders pero el flash oscuro seguía en `st.button` (Save Settings) y `st.checkbox` (Mandate gate toggle) | CSS universal: `button:focus-visible`, `[role="button"]`, todo `stButton` y `stCheckbox` con brand color sutil |
| [[Bugs/B025_dashboard_silent_paper_positions]] | 🔴 | 25 | Carlos: "cuando cambio a live no me dice nada de las entradas en paper". El checklist de Sprint 22 no corría porque `mode_override` solo cambiaba `mandate_enabled` pero no `use_testnet` → checklist se saltaba. Dashboard no mostraba las paper positions. | Sprint 25 fix: dashboard muestra banner prominent con paper positions + botón "Clean Paper Positions" en sidebar; bot loguea paper positions al startup; checklist corre cuando hay paper positions + mandate enabled (no solo live); override se actualiza si el checklist aborta |
| [[Bugs/B026_dashboard_positions_nameerror]] | 🟠 | 26 patch | El botón "Clean Paper Positions" en el sidebar referenciaba `positions` que se carga DESPUÉS del sidebar → `NameError: name 'positions' is not defined` al abrir el dashboard | Cargar positions desde JSON directamente en variable local `_sidebar_pp` dentro del bloque del sidebar |
| [[Bugs/B027_dashboard_accessibility_csp_warnings]] | 🟡 | 27 | Browser DevTools mostraba 4 categorías de warnings: CSP bloquea `eval`, form field sin id/name, autocomplete vacío, sin label asociado. Algunos son limitaciones de Streamlit 1.36. | Inyectar `<meta>` tags via `st.components.v1.html` con CSP permisivo (`unsafe-eval` para Streamlit, `unsafe-inline` para CSS) + meta tags de accessibility (`theme-color`, `color-scheme`, `format-detection`) + CSS para `autocomplete=off` |
| [[Bugs/B028_streamlit_console_warnings]] | 🟡 | 28 | Console mostraba 9+ warnings: "Gather usage stats: true" + 8 "Unrecognized feature" (ambient-light-sensor, battery, document-domain, layout-animations, etc.) + iframe sandbox warning del streamlit_autorefresh component. Todos vienen del bundle JS compilado de Streamlit 1.36. | `gatherUsageStats = false` en `.streamlit/config.toml` elimina el telemetry warning. Los 8 "Unrecognized feature" + iframe sandbox son limitaciones de Streamlit 1.36 (no se pueden parchear sin upgrade). |
| [[Bugs/B028v2_coolify_dashboard_crashloop]] | 🔴 | 31 | Carlos: "en el coolify esta Exited (14x restarts)". El dashboard container estaba en crash loop (`Restarting (1)`), pero el bot engine estaba sano. Causa raíz: mi fix original de B028 (`e185d61`) duplicó la sección `[browser]` en `.streamlit/config.toml` — TOML no permite headers duplicados, el parser falla con `TomlDecodeError: What? browser already exists?` y streamlit nunca llega a bootear. Además, el deploy #334 falló con `network wyn2ah6rflg6ufwzpvzk436f declared as external, but could not be found` porque Coolify perdió la red per-recurso (estado inconsistente tras los containers removidos). | Consolidar en una sola sección `[browser]` con `gatherUsageStats`, `serverAddress`, `serverPort`. Crear la red manualmente con `docker network create --driver bridge <uuid>`. Forzar redeploy con un commit vacío (`git commit --allow-empty`). Commit `6aee4ff` (consolidación) + `d73924d` (redeploy trigger). Lección: cuando se agrega una config key a una sección existente, **siempre consolidar** — nunca duplicar headers TOML aunque parezca inofensivo. **Validar con `toml.loads()` antes de commitear**. |
| [[Bugs/B029_dashboard_sidebar_scope_escape]] | 🔴 | 31 patch | Después de fix B028v2 el dashboard arrancó, pero al primer render Streamlit crasheó con `NameError: name '_sidebar_open' is not defined` at `dashboard.py:1881` (preflight checklist). El preflight widget referenciaba `_sidebar_open` en el MAIN area, pero esa variable solo se asignaba DENTRO del `with st.sidebar:` block (línea 2082). Streamlit ejecuta el sidebar antes pero las vars no salen del `with` context manager — son scope-locales al bloque del sidebar, no al módulo. B026 solo había arreglado el caso de `positions`, no previno el mismo patrón en código posterior. | Calcular `_open_paper_positions_count` a nivel de módulo (línea 1684, ANTES del `with st.sidebar:`), leer `data_store/positions.json` directamente con `try/except`. Reemplazar las 10 ocurrencias de `_sidebar_open` por `_open_paper_positions_count`. Patrón: **toda variable que se use tanto en sidebar como en main area debe calcularse a nivel de módulo**, nunca dentro de `with st.sidebar:`. Verificar con `py_compile` antes de pushear. Commit `cd9fc4b`. |

## Stats

- 🔴 Críticos: **15** (B001, B002, B003, B006, B013, B017, B018, B019, B020, B021, B022, B028v2, B029; el B015 era operacional pero mataba la portabilidad)
- 🟠 Medios (estrategia/métricas incorrectas): **10** (incluyendo B015, B026)
- 🟡 Menores: **4** (B016 uuid colisión, B023 filter flash, B024b universal dark flash, B028 streamlit console)

**Total: 31 bugs cerrados en 16 sprints.**

Distribución por origen:
- **Audit team externo** (Sprint 18): B017, B018, B019
- **Code review post-sprint**: B020, B021, B022
- **Reportados por Carlos**: B023, B024, B024b
- **Encontrados durante construcción**: B001-B016

Lección principal: **los tests unitarios pasan pero el sistema end-to-end puede fallar** (B017-B022 son todos bugs de integración). Siempre hacer tests de integración + manuales.
