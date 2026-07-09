# Sprints Index

7 sprints completos + 1 sprint post-refactor (PDF2 indicators).

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

## Tests por sprint

| Sprint | Test script | Resultado |
|--------|-------------|-----------|
| 1 | `python /tmp/test_sprint1.py` | Mandate bloquea 3/4 trades; Kill switch arma/desarma OK |
| 2 | `python /tmp/test_sprint2.py` | PositionMonitor cierra stops/TPs correctamente |
| 3 | `python /tmp/test_sprint3.py` | Debate filtra 4/4 trades según edge + duplicación |
| 4 | `python /tmp/test_sprint4.py` | Walk-forward detecta overfit RSI(25/75) |
| 5 | `python /tmp/test_sprint5.py` | Re-opt encuentra sharpe 0.80, actualiza params |
| 6 | `python /tmp/test_sprint6.py` | Data validator rechaza NaN/Inf/high<low |

Todos los scripts temporales están en `C:\Users\cpier\AppData\Local\Temp\test_sprintN.py`.
