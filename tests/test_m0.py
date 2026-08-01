import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "risk-policy.json"


class M0BaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def test_approved_limits_are_exact(self):
        self.assertEqual(
            self.policy["limits_pct"],
            {
                "max_loss_per_trade": 2.0,
                "max_event_risk": 5.0,
                "max_theme_risk": 10.0,
                "max_total_worst_case_loss": 20.0,
                "daily_hard_stop": 5.0,
                "rolling_3d_hard_stop": 10.0,
                "high_watermark_drawdown_freeze": 20.0,
            },
        )

    def test_limit_hierarchy_is_consistent(self):
        limits = self.policy["limits_pct"]
        self.assertLessEqual(limits["max_loss_per_trade"], limits["max_event_risk"])
        self.assertLessEqual(limits["max_event_risk"], limits["max_theme_risk"])
        self.assertLessEqual(limits["max_theme_risk"], limits["max_total_worst_case_loss"])
        self.assertLessEqual(limits["daily_hard_stop"], limits["rolling_3d_hard_stop"])
        self.assertLessEqual(
            limits["rolling_3d_hard_stop"],
            limits["high_watermark_drawdown_freeze"],
        )

    def test_m0_is_fail_closed_and_paper_only(self):
        controls = self.policy["controls"]
        self.assertEqual(self.policy["mode"], "paper")
        self.assertFalse(controls["live_trading_enabled"])
        self.assertTrue(controls["live_requires_fresh_user_approval"])
        self.assertFalse(controls["withdrawal_automation_enabled"])
        self.assertFalse(controls["risk_limits_self_modifiable"])
        self.assertFalse(controls["credentials_visible_to_llm"])
        self.assertTrue(controls["freeze_requires_user_unlock"])
        self.assertTrue(controls["fail_closed_on_missing_or_stale_state"])

    def test_account_sizes_match_approved_stages(self):
        accounts = self.policy["accounts"]
        self.assertEqual(accounts["paper"]["starting_capital"], 5000.0)
        self.assertEqual(accounts["live_tier_1"]["capital_min"], 200.0)
        self.assertEqual(accounts["live_tier_1"]["capital_max"], 300.0)

    def test_measurement_closes_common_aggregation_gaps(self):
        measurement = self.policy["measurement"]
        self.assertTrue(measurement["include_realized_and_unrealized_pnl"])
        self.assertTrue(measurement["include_open_orders_in_worst_case_loss"])
        self.assertTrue(measurement["include_fees_and_slippage_in_worst_case_loss"])
        self.assertTrue(measurement["aggregate_correlated_positions"])

    def test_market_making_is_not_a_primary_edge(self):
        constraints = self.policy["strategy_constraints"]
        self.assertFalse(constraints["market_making_as_primary_edge"])
        self.assertFalse(constraints["latency_arbitrage"])
        self.assertFalse(constraints["maker_rebate_capture"])
        self.assertTrue(constraints["passive_limit_orders_for_signal_execution"])


if __name__ == "__main__":
    unittest.main()
