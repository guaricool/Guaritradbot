# Sprints Index

13 sprints cerrados (0-7, 18, 19, 21, 22, 23).

| Sprint | Tema principal | Inspiración | Commit |
|--------|---------------|-------------|--------|
| [[Sprints/Sprint_0_Critical_Bug_Fixes]] | Fix bugs que tenían el bot muerto | Auditoría propia | `10d144c` |
| [[Sprints/Sprint_1_Safety_Layer]] | Mandate Gate + Kill Switch + Audit | Vibe-Trading | `10d144c` |
| [[Sprints/Sprint_2_Position_Tracking]] | Position Repository + TP ATR + Monitor | NautilusTrader "crash-only" | `a2981bd` + `49c36f4` |
| [[Sprints/Sprint_3_Multi_Agent_Debate]] | Bull/Bear/Risk/PM debate | TradingAgents | `8e84390` |
| [[Sprints/Sprint_4_Backtester_Fix]] | Walk-Forward + Profit Factor + Expectancy | freqtrade | `7a0cd26` |
| [[Sprints/Sprint_5_Real_Reoptimization]] | HyperoptManager real injection | intelligent-trading-bot | `b2e8fb8` |
| [[Sprints/Sprint_6_State_Machine_Data_Integrity]] | Component FSM + NaN/Inf fail-fast | NautilusTrader | `b3904ad` |
| [[Sprints/Sprint_7_PDF_Indicators]] | DM/ADX + Estocástico + Bollinger + S/R | Manual del Buen Trader | `51a3db4` |
| [[Sprints/Sprint_18_Audit_Fixes_Portfolio_Management]] | Bug A/B/C del audit + position replacement + smart profit-take | Audit Team + pregunta Carlos | `5dbe030` |
| [[Sprints/Sprint_19_ML_Pipeline]] | FeatureExtractor + ModelTrainer (LogisticRegression) + Predictor + integration StrategyAgent | intelligent-trading-bot + scikit-learn | `2847dff` |
| [[Sprints/Sprint_21_Alpha_Zoo]] | 48+ alpha features via `ta` library (RSI/MACD/BB/ATR/ADX/Ichimoku/etc.) | TA-Lib (pure-Python fork) | `2847dff` |
| [[Sprints/Sprint_22_Paper_Live_Transition]] | Pre-flight checklist: broker check + paper positions handling + dry-run validation | Carlos preguntó "¿qué pasa al ir a live?" | `d03cb2c` |
| [[Sprints/Sprint_23_Live_Equity_Tracker]] | EquityTracker con precision sub-dólar + drawdown + history para sparklines | Carlos: "¿ centavos o dólares ganando/perdiendo?" | (pending push) |

## Tests por sprint

| Sprint | Test command | Resultado |
|--------|--------------|-----------|
| 1 | `python /tmp/test_sprint1.py` | Mandate bloquea 3/4 trades; Kill switch arma/desarma OK |
| 2 | `python /tmp/test_sprint2.py` | PositionMonitor cierra stops/TPs correctamente |
| 3 | `python /tmp/test_sprint3.py` | Debate filtra 4/4 trades según edge + duplicación |
| 4 | `python /tmp/test_sprint4.py` | Walk-forward detecta overfit RSI(25/75) |
| 5 | `python /tmp/test_sprint5.py` | Re-opt encuentra sharpe 0.80, actualiza params |
| 6 | `python /tmp/test_sprint6.py` | Data validator rechaza NaN/Inf/high<low |
| 18 | `python -m unittest discover tests -v` | **26/26 passing** |
| 19 | `python -m unittest tests.test_ml_pipeline -v` | **11/11 passing** |
| 21 | `python -m unittest tests.test_alpha_zoo -v` | **8/8 passing** |
| 22 | `python -m unittest tests.test_paper_to_live -v` | **10/10 passing** |
| 23 | `python -m unittest tests.test_equity_tracker -v` | **16/16 passing** |
| **TOTAL** | `python -m unittest discover tests` | **71/71 passing** |
