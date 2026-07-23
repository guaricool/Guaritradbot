"""
Unit tests for OpportunityScanner.
"""
import unittest
import numpy as np
import pandas as pd

from src.analysis.opportunity_scanner import OpportunityScanner


class TestOpportunityScanner(unittest.TestCase):
    def test_opportunity_scanner_squeeze(self):
        scanner = OpportunityScanner()

        np.random.seed(42)
        bars = 50
        close = np.linspace(100.0, 115.0, bars)

        df = pd.DataFrame(
            {
                "close": close,
                "high": close + 0.5,
                "low": close - 0.5,
                "open": close - 0.2,
                "volume": 1000.0,
            }
        )

        signals = scanner.scan_asset("BTC-USD", df)
        self.assertIsInstance(signals, list)
        self.assertGreater(len(signals), 0)
        self.assertEqual(signals[0].asset, "BTC-USD")


if __name__ == "__main__":
    unittest.main()
