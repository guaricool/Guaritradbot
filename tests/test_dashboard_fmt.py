"""
Sprint 37+ fix — Tests for fmt_usd / fmt_pct / color_class.

The dashboard formatters had a bug: fmt_usd always prefixed with '+'
for non-negative values, even for things like the account balance (which
isn't a gain/loss indicator). This caused the KPI cards to show
'+$20.00' for the starting balance — misleading.

The fix introduces a `signed` parameter:
  - signed=False (default, legacy): always '+' for non-negative, '' for negative
  - signed=True: '+' for positive, '-' for negative, '' for zero
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class FmtUsdTest(unittest.TestCase):
    def test_unsigned_zero_shows_plus(self):
        # Legacy behavior: +$0.00 for non-negative
        from dashboard import fmt_usd
        self.assertEqual(fmt_usd(0.0), "+$0.00")
        self.assertEqual(fmt_usd(20.0), "+$20.00")
        self.assertEqual(fmt_usd(100.5), "+$100.50")

    def test_unsigned_negative_omits_sign(self):
        # Legacy: '' for negative
        from dashboard import fmt_usd
        self.assertEqual(fmt_usd(-5.0), "$-5.00")
        self.assertEqual(fmt_usd(-0.15), "$-0.15")

    def test_signed_positive_uses_plus(self):
        from dashboard import fmt_usd
        self.assertEqual(fmt_usd(1.23, signed=True), "+$1.23")
        self.assertEqual(fmt_usd(100.0, signed=True), "+$100.00")

    def test_signed_negative_uses_minus(self):
        from dashboard import fmt_usd
        self.assertEqual(fmt_usd(-1.23, signed=True), "-$1.23")
        self.assertEqual(fmt_usd(-100.0, signed=True), "-$100.00")

    def test_signed_zero_has_no_sign(self):
        # The new behavior: zero shows as $0.00, no + prefix
        from dashboard import fmt_usd
        self.assertEqual(fmt_usd(0.0, signed=True), "$0.00")
        self.assertEqual(fmt_usd(0.0, signed=True, decimals=4), "$0.0000")

    def test_signed_with_decimals(self):
        from dashboard import fmt_usd
        # Show full precision (cents) — useful for $20 accounts
        self.assertEqual(fmt_usd(0.001234, signed=True, decimals=4), "+$0.0012")
        self.assertEqual(fmt_usd(-0.001234, signed=True, decimals=4), "-$0.0012")

    def test_none_returns_dash(self):
        from dashboard import fmt_usd
        self.assertEqual(fmt_usd(None), "—")
        self.assertEqual(fmt_usd(None, signed=True), "—")


class FmtPctTest(unittest.TestCase):
    def test_unsigned(self):
        from dashboard import fmt_pct
        self.assertEqual(fmt_pct(0.0), "+0.00%")
        self.assertEqual(fmt_pct(5.5), "+5.50%")
        self.assertEqual(fmt_pct(-3.2), "-3.20%")

    def test_signed(self):
        from dashboard import fmt_pct
        self.assertEqual(fmt_pct(0.0, signed=True), "0.00%")
        self.assertEqual(fmt_pct(5.5, signed=True), "+5.50%")
        self.assertEqual(fmt_pct(-3.2, signed=True), "-3.20%")


class ColorClassTest(unittest.TestCase):
    def test_positive_is_pos(self):
        from dashboard import color_class
        self.assertEqual(color_class(5.0), "pos")
        self.assertEqual(color_class(0.01), "pos")

    def test_negative_is_neg(self):
        from dashboard import color_class
        self.assertEqual(color_class(-5.0), "neg")
        self.assertEqual(color_class(-0.01), "neg")

    def test_zero_is_neu(self):
        from dashboard import color_class
        self.assertEqual(color_class(0.0), "neu")

    def test_none_is_neu(self):
        from dashboard import color_class
        self.assertEqual(color_class(None), "neu")


class FmtUsdRegressionTest(unittest.TestCase):
    """Regression: ensure the changes don't break existing call sites.

    The legacy `fmt_usd(x)` (no signed kwarg) MUST still produce the
    same output as before for the existing call sites that don't pass
    `signed`. The new `signed=True` is opt-in.
    """

    def test_default_is_legacy_behavior(self):
        from dashboard import fmt_usd
        # All non-negative numbers get '+' (legacy)
        self.assertTrue(fmt_usd(0).startswith("+"))
        self.assertTrue(fmt_usd(100).startswith("+"))
        # All negative numbers have no prefix
        self.assertTrue(fmt_usd(-1).startswith("$-"))
        self.assertTrue(fmt_usd(-100).startswith("$-"))


if __name__ == "__main__":
    unittest.main()
