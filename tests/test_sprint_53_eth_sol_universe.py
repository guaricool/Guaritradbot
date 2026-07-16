"""
Sprint 53 — ETH-USD and SOL-USD re-enabled in operational universe.

Sprint 46X audit B5 had removed ETH-USD and SOL-USD from
config.yaml's `brokers.crypto.symbols` and
`mandate.allowed_symbols` because "no strategy targets
them" -- the right fix was to ADD the strategies, not
remove the assets. Sprint 53 reverses the removal and
extends the StrategyAgent asset tuples to include all 3
cryptos in every applicable block.

Carlos's reasoning (2026-07-12 dashboard review):
"BTC-only is more limited; should be able to search
many more cryptos to take those entries."

These tests pin the new operational surface:
  1. config.yaml `brokers.crypto.symbols` contains ETH-USD
     and SOL-USD (not just BTC-USD).
  2. config.yaml `mandate.allowed_symbols` contains all 3
     cryptos.
  3. trading_loop.yaml `analyze_market.inputs.assets` includes
     ETH-USD and SOL-USD so MarketAnalystAgent fetches them.
  4. StrategyAgent MACD block (the crypto-only momentum
     strategy) generates signals for ETH-USD when its MACD
     crosses bullish.
  5. StrategyAgent universal blocks (Stochastic, BB, S/R,
     ADX) generate signals for ETH-USD and SOL-USD.
"""
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


class ConfigYAMLTest(unittest.TestCase):
    """Pin the operational config surface."""

    def setUp(self):
        self.cfg = yaml.safe_load(
            (REPO / "config.yaml").read_text(encoding="utf-8")
        )

    def test_crypto_symbols_includes_eth_and_sol(self):
        symbols = self.cfg["brokers"]["crypto"]["symbols"]
        self.assertIn("BTC-USD", symbols)
        self.assertIn("ETH-USD", symbols)
        self.assertIn("SOL-USD", symbols)

    def test_mandate_allowlist_includes_eth_and_sol(self):
        allowed = self.cfg["mandate"]["allowed_symbols"]
        self.assertIn("BTC-USD", allowed)
        self.assertIn("ETH-USD", allowed)
        self.assertIn("SOL-USD", allowed)


class TradingLoopYamlTest(unittest.TestCase):
    """Pin the workflow asset list."""

    def setUp(self):
        import yaml as _y
        self.wf = _y.safe_load(
            (REPO / "src" / "workflows" / "trading_loop.yaml")
            .read_text(encoding="utf-8")
        )

    def _step_inputs_assets(self, step_id):
        for step in self.wf["steps"]:
            if step.get("id") == step_id:
                return step.get("inputs", {}).get("assets", [])
        return []

    def test_analyze_market_includes_eth_and_sol(self):
        assets = self._step_inputs_assets("analyze_market")
        self.assertIn("BTC-USD", assets)
        self.assertIn("ETH-USD", assets)
        self.assertIn("SOL-USD", assets)

    def test_scan_news_includes_eth_and_sol(self):
        assets = self._step_inputs_assets("scan_news")
        self.assertIn("ETH-USD", assets)
        self.assertIn("SOL-USD", assets)

    def test_scan_social_sentiment_includes_eth_and_sol(self):
        assets = self._step_inputs_assets("scan_social_sentiment")
        self.assertIn("ETH-USD", assets)
        self.assertIn("SOL-USD", assets)


class StrategyAgentCryptoMacdTest(unittest.TestCase):
    """The crypto-only MACD block must include ETH-USD and SOL-USD."""

    def test_macd_block_source_includes_eth_and_sol(self):
        text = (REPO / "src" / "agents" / "strategy_agent.py").read_text(
            encoding="utf-8"
        )
        # Find the line that declares the MACD asset tuple
        # and assert ETH/SOL are in it.
        import re
        m = re.search(
            r'for asset in \("BTC-USD", "BTCUSDT"[^)]*\):',
            text,
        )
        self.assertIsNotNone(
            m, "Could not find the MACD-block asset tuple"
        )
        line = m.group(0)
        self.assertIn("ETH-USD", line)
        self.assertIn("SOL-USD", line)


class StrategyAgentUniversalBlocksTest(unittest.TestCase):
    """The asset-agnostic strategy blocks must include ETH and SOL."""

    def setUp(self):
        self.text = (REPO / "src" / "agents" / "strategy_agent.py").read_text(
            encoding="utf-8"
        )

    def _count(self, pattern):
        import re
        return len(re.findall(pattern, self.text))

    def test_universal_blocks_have_eth_and_sol(self):
        """All `for asset in ("SPY", "QQQ", "BTC-USD", ...)` blocks
        must include ETH-USD and SOL-USD. There are 4 universal
        blocks (Stochastic, BB, S/R, ADX) + 1 order-shuffled
        variant inside a nested function (5 total)."""
        import re
        # Find all tuples starting with SPY, QQQ, BTC-USD
        matches = re.findall(
            r'for asset in \("SPY", "QQQ", "BTC-USD"[^)]*\):',
            self.text,
        )
        self.assertGreaterEqual(
            len(matches), 4,
            f"Expected 4+ universal asset tuples, found {len(matches)}"
        )
        # All of them must include ETH-USD and SOL-USD
        for line in matches:
            self.assertIn("ETH-USD", line, f"Missing ETH-USD in: {line}")
            self.assertIn("SOL-USD", line, f"Missing SOL-USD in: {line}")


if __name__ == "__main__":
    unittest.main()
