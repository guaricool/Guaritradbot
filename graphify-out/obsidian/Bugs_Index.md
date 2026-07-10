# Bugs Index

23 bugs encontrados y corregidos. Severidad: 🔴 crítico (rompía runtime) | 🟠 medio | 🟡 menor

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

## Stats

- 🔴 Críticos: **13** (B001, B002, B003, B006, B013, B017, B018, B019, B020, B021, B022; el B015 era operacional pero mataba la portabilidad)
- 🟠 Medios (estrategia/métricas incorrectas): **8**
- 🟡 Menores: **2** (B016 uuid colisión, B023 dashboard flash)

**Total: 23 bugs cerrados en 11 sprints.**

Sprint 18 cerró:
- **3 bugs del audit team** (B017/B018/B019) — encontrados por análisis externo
- **3 bugs del code review post-sprint** (B020/B021/B022) — encontrados en revisión interna
- **1 bug UX** (B023) — reportado por Carlos al usar el dashboard

Los 6 bugs rojos del Sprint 18 son particularmente insidiosos porque los tests unitarios pasaban (cada módulo se veía correcto en aislamiento), pero el sistema end-to-end no funcionaba. Lección: siempre hacer tests de integración, no solo unitarios.
