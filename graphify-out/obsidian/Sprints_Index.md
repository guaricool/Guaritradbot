# Sprints Index

9 sprints completos + 1 sprint post-refactor (PDF2 indicators) + Sprint 18 audit/portfolio.

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
| [[Sprints/Sprint_18_Audit_Fixes_Portfolio_Management]] | Bug A/B/C del audit + position replacement + smart profit-take | Audit Team + pregunta Carlos | (pending push) |

## Tests por sprint

| Sprint | Test command | Resultado |
|--------|--------------|-----------|
| 1 | `python /tmp/test_sprint1.py` | Mandate bloquea 3/4 trades; Kill switch arma/desarma OK |
| 2 | `python /tmp/test_sprint2.py` | PositionMonitor cierra stops/TPs correctamente |
| 3 | `python /tmp/test_sprint3.py` | Debate filtra 4/4 trades según edge + duplicación |
| 4 | `python /tmp/test_sprint4.py` | Walk-forward detecta overfit RSI(25/75) |
| 5 | `python /tmp/test_sprint5.py` | Re-opt encuentra sharpe 0.80, actualiza params |
| 6 | `python /tmp/test_sprint6.py` | Data validator rechaza NaN/Inf/high<low |
| 18 | `python -m unittest discover tests -v` | **18/18 passing** (3 archivos de test nuevos en `tests/`) |

Tests del Sprint 0-7 son scripts legacy en `C:\Users\cpier\AppData\Local\Temp\test_sprintN.py`. Tests del Sprint 18 viven en `tests/` (estructura stdlib unittest, no pytest).
