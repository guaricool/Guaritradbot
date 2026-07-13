"""Sprint 46V (audit B3): legacy strategy library — DEPRECATED.

The production bot generates signals through `src/agents/strategy_agent.py`
(ADX/MACD/EMA/Bollinger/mean-reversion/Stoch-RSI + the multi-agent
debate). The modules in this package were the original strategy
implementations before the refactor; they are still tested for
regression coverage (anti-look-ahead, multi-TF signal correctness)
but the bot does NOT import from here in production — see the
audit's B3 entry and `AUDITORIA_COMPLETA_2026-07-11.md` §5.

Do not import from this package in production code. The tests
under `tests/test_sprint_37_41.py`,
`tests/test_sprint_43_h3_h4_h10_h11_l7_l9_l10_m8.py`, and
`tests/test_sprint_43_lows_batch1.py` cover the math/look-ahead
invariants and stay here deliberately.

If you need a new strategy, add it to `src/agents/strategy_agent.py`,
not here.
"""
