import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path

import m4


ROOT = Path(__file__).resolve().parents[1]


class M4Test(unittest.TestCase):
    def setUp(self):
        self.config = m4.load_config()

    def candidate(self):
        return {
            "wallet": "0x" + "1" * 40,
            "user_name": "macro",
            "x_username": "macro_x",
            "verified_badge": True,
            "leaderboard_appearance_count": 3,
            "leaderboard_appearances": [],
            "categories": ["ECONOMICS"],
            "periods": ["WEEK", "MONTH", "ALL"],
            "best_rank": 1,
        }

    def test_configuration_is_public_paper_only_and_excludes_sports(self):
        self.assertEqual(m4.validate_config(self.config), self.config)
        self.assertEqual(self.config["version"], 2)
        self.assertEqual(self.config["maximum_candidates_to_audit"], 50)
        self.assertTrue(self.config["paper_only"])
        self.assertEqual(
            set(self.config["categories"]), {"POLITICS", "ECONOMICS", "FINANCE"}
        )
        self.assertNotIn("SPORTS", self.config["categories"])
        self.assertTrue(str(m4.RUNTIME).endswith("runtime/m2/research/m4"))

    def test_candidate_discovery_deduplicates_and_prefers_recurrence(self):
        wallet_a, wallet_b = "0x" + "a" * 40, "0x" + "b" * 40
        leaderboards = {
            "ECONOMICS:WEEK": {
                "rows": [
                    {"proxyWallet": wallet_a, "rank": "2", "userName": "A"},
                    {"proxyWallet": wallet_b, "rank": "1", "userName": "B"},
                ]
            },
            "ECONOMICS:MONTH": {
                "rows": [{"proxyWallet": wallet_a, "rank": "5", "userName": "A"}]
            },
            "FINANCE:ALL": {
                "rows": [{"proxyWallet": wallet_a, "rank": "8", "userName": "A"}]
            },
        }
        result = m4.discover_candidates(leaderboards, self.config)
        self.assertEqual([item["wallet"] for item in result], [wallet_a])
        self.assertEqual(result[0]["leaderboard_appearance_count"], 3)
        self.assertEqual(result[0]["categories"], ["ECONOMICS", "FINANCE"])

    def test_excluded_title_wins_over_target_keyword(self):
        self.assertEqual(
            m4.classify_title("Will Trump attend an NBA game?", self.config),
            "excluded",
        )
        self.assertEqual(
            m4.classify_title("Will the Fed cut interest rates?", self.config),
            "target",
        )
        self.assertEqual(
            m4.classify_title("Will annual inflation be 3.4%?", self.config),
            "target",
        )
        self.assertEqual(
            m4.classify_title("Will Nigel Farage win a UK by-election?", self.config),
            "unknown",
        )
        self.assertEqual(m4.classify_title("Unclassified event", self.config), "unknown")

    def test_trade_metrics_aggregate_fragments_before_materiality(self):
        trades = [
            {
                "conditionId": "market-1",
                "side": "BUY",
                "outcome": "Yes",
                "price": 0.5,
                "size": 80,
                "timestamp": 1_790,
                "title": "Will the Fed cut interest rates?",
            },
            {
                "conditionId": "market-1",
                "side": "BUY",
                "outcome": "Yes",
                "price": 0.5,
                "size": 160,
                "timestamp": 1_810,
                "title": "Will the Fed cut interest rates?",
            },
            {
                "conditionId": "market-2",
                "side": "BUY",
                "outcome": "Yes",
                "price": 0.95,
                "size": 200,
                "timestamp": 4_000,
                "title": "Will Bitcoin exceed $100k?",
            },
        ]
        metrics = m4.trade_metrics(trades, self.config)
        self.assertEqual(metrics["aggregated_action_count"], 2)
        self.assertEqual(metrics["material_aggregated_action_count"], 1)
        self.assertAlmostEqual(metrics["target_trade_notional_fraction"], 120 / 310, places=6)
        self.assertAlmostEqual(metrics["excluded_trade_notional_fraction"], 190 / 310, places=6)
        self.assertAlmostEqual(metrics["extreme_price_fraction"], 1 / 3, places=6)
        self.assertGreater(metrics["material_aggregated_actions_per_day"], 10)

    def test_candidate_fails_closed_on_missing_evidence(self):
        result = m4.audit_candidate(
            self.candidate(),
            {
                "trades": None,
                "closed_positions": None,
                "profile": None,
                "errors": {"trades": "timeout"},
            },
            self.config,
        )
        self.assertEqual(result["screen_status"], "insufficient_evidence")
        self.assertFalse(result["screen_checks"]["required_sources_complete"])

    def test_closed_pnl_concentration_groups_contracts_by_event(self):
        positions = [
            {"eventSlug": "event-a", "realizedPnl": 10, "title": "Fed rate cut A"},
            {"eventSlug": "event-a", "realizedPnl": 20, "title": "Fed rate cut B"},
            {"eventSlug": "event-b", "realizedPnl": 30, "title": "Fed rate cut C"},
        ]
        metrics = m4.closed_position_metrics(positions, self.config)
        self.assertEqual(metrics["distinct_event_count"], 2)
        self.assertEqual(metrics["single_event_abs_pnl_fraction"], 0.5)

    def test_current_winners_never_become_experts_automatically(self):
        config = copy.deepcopy(self.config)
        config["gates"]["minimum_closed_positions"] = 1
        config["gates"]["maximum_single_closed_event_abs_pnl_fraction"] = 1
        candidate = self.candidate()
        trades = [
            {
                "conditionId": "fed",
                "side": "BUY",
                "outcome": "Yes",
                "price": 0.5,
                "size": 400,
                "timestamp": 1_000,
                "title": "Will the Fed cut interest rates?",
            }
        ]
        closed = [
            {
                "realizedPnl": 10,
                "title": "Will the Fed cut interest rates?",
            }
        ]
        result = m4.audit_candidate(
            candidate,
            {"trades": trades, "closed_positions": closed, "profile": {}, "errors": {}},
            config,
        )
        self.assertEqual(result["screen_status"], "observation_review")
        self.assertIn("Candidate only", result["note"])
        self.assertNotIn("expert", result["screen_status"])

    def test_report_pre_registers_prospective_use_without_alpha_claim(self):
        report = m4.build_report(
            {},
            {"POLITICS:WEEK": "timeout"},
            {},
            self.config,
            datetime(2026, 8, 14, tzinfo=timezone.utc),
        )
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["observation_pool"], [])
        self.assertEqual(report["method"]["profitability_claim"], "none")
        self.assertIn("prospective", report["method"]["future_use"])

    def test_source_contains_no_order_or_credential_capability(self):
        source = (ROOT / "m4.py").read_text(encoding="utf-8").lower()
        self.assertIn("https://data-api.polymarket.com", source)
        self.assertIn("https://gamma-api.polymarket.com/public-profile", source)
        self.assertNotIn("private_key", source)
        self.assertNotIn("api_secret", source)
        self.assertNotIn("post_order", source)


if __name__ == "__main__":
    unittest.main()
