# Bugs Index

16 bugs encontrados y corregidos. Severidad: 🔴 crítico (rompía runtime) | 🟠 medio | 🟡 menor

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

## Stats

- 🔴 Críticos que rompían runtime: **6** (B001, B002, B003, B006, B013; el B015 era operacional pero mataba la portabilidad)
- 🟠 Medios (estrategia/métricas incorrectas): **8**
- 🟡 Menores: **2**

Todos cerrados en los sprints 0-5.
