import sys
import unittest
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from post_market_reconcile import _classify_trade
except ModuleNotFoundError:
    _classify_trade = None


class PostMarketReconcileClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _classify_trade is None:
            raise unittest.SkipTest("post_market_reconcile dependencies are not installed in this environment")

    def test_classify_trade_good(self):
        row = {
            "status": "closed",
            "realized_pnl": 1200.0,
            "slippage_bps": 8.0,
            "exit_reason": "target",
            "entry_fill_price": 100.0,
            "intended_entry_price": 100.0,
            "intended_shares": 100,
            "side": "LONG",
            "stop_loss": 98.0,
            "entry_fill_time": "2026-04-09T13:00:00+00:00",
            "exit_fill_time": "2026-04-09T16:00:00+00:00",
        }
        cls, score, rationale = _classify_trade(row)
        self.assertEqual(cls, "GOOD")
        self.assertGreaterEqual(score, 25)
        self.assertIn("positive pnl", rationale)

    def test_classify_trade_bad(self):
        row = {
            "status": "closed",
            "realized_pnl": -1300.0,
            "slippage_bps": 60.0,
            "exit_reason": "stop_loss",
            "entry_fill_price": 100.0,
            "intended_entry_price": 100.0,
            "intended_shares": 100,
            "side": "LONG",
            "stop_loss": 98.0,
            "entry_fill_time": "2026-04-09T13:00:00+00:00",
            "exit_fill_time": "2026-04-09T13:05:00+00:00",
        }
        cls, score, rationale = _classify_trade(row)
        self.assertEqual(cls, "BAD")
        self.assertLessEqual(score, -20)
        self.assertIn("negative pnl", rationale)

    def test_classify_trade_neutral_when_not_closed(self):
        row = {
            "status": "open",
            "realized_pnl": 100.0,
        }
        cls, score, rationale = _classify_trade(row)
        self.assertEqual(cls, "NEUTRAL")
        self.assertEqual(score, 0)
        self.assertIn("not closed", rationale)


if __name__ == "__main__":
    unittest.main()
