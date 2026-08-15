import copy
import hashlib
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import m4


ROOT = Path(__file__).resolve().parents[1]


class M4Test(unittest.TestCase):
    def setUp(self):
        self.config = m4.load_config()

    def test_file_and_content_hashes_have_unambiguous_meaning(self):
        payload = {"b": 2, "a": 1}
        self.assertEqual(m4.content_hash(payload), m4.content_hash({"a": 1, "b": 2}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json.gz"
            path.write_bytes(b"compressed evidence bytes")
            self.assertEqual(
                m4.file_hash(path),
                hashlib.sha256(b"compressed evidence bytes").hexdigest(),
            )

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
        self.assertEqual(self.config["version"], 3)
        self.assertEqual(self.config["maximum_candidates_to_audit"], 50)
        self.assertNotIn("price", self.config["peer_discovery"])
        self.assertNotIn("minimum_hours_to_resolution", self.config["peer_discovery"])
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

    def test_peer_actions_record_prices_without_using_a_price_gate(self):
        wallet = "0x" + "2" * 40
        condition = "0x" + "a" * 64
        trades = [
            {
                "proxyWallet": wallet,
                "conditionId": condition,
                "side": "BUY",
                "outcomeIndex": 0,
                "price": 0.001,
                "size": 100_000,
                "timestamp": 1_000,
                "title": "Will the Fed cut interest rates?",
            },
            {
                "proxyWallet": wallet,
                "conditionId": condition,
                "side": "SELL",
                "outcomeIndex": 1,
                "price": 0.999,
                "size": 101,
                "timestamp": 1_010,
                "title": "Will the Fed cut interest rates?",
            },
        ]
        actions = m4.directional_actions(trades, self.config, target_only=True)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["direction_outcome_index"], 0)
        self.assertEqual(actions[0]["minimum_observed_price"], 0.001)
        self.assertEqual(actions[0]["maximum_observed_price"], 0.999)
        self.assertEqual(actions[0]["net_directional_notional_usd"], 200.899)

    def test_peer_actions_reject_gross_volume_with_immaterial_net_direction(self):
        wallet = "0x" + "2" * 40
        condition = "0x" + "b" * 64
        trades = [
            {
                "proxyWallet": wallet,
                "conditionId": condition,
                "side": side,
                "outcomeIndex": 0,
                "price": 0.5,
                "size": size,
                "timestamp": timestamp,
                "title": "Will the Fed cut interest rates?",
            }
            for side, size, timestamp in (
                ("BUY", 1000, 1000),
                ("SELL", 999, 1001),
            )
        ]
        self.assertEqual(m4.directional_actions(trades, self.config, target_only=True), [])

    def test_peer_market_fetch_is_bounded_to_the_same_lookback_window(self):
        observed_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
        condition = "0x" + "c" * 64
        queries = []

        def fake_fetch(url, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            queries.append(query)
            if "user" in query:
                return [
                    {
                        "proxyWallet": self.config["peer_discovery"]["reference_wallets"][0],
                        "conditionId": condition,
                        "side": "BUY",
                        "outcomeIndex": 0,
                        "price": 0.5,
                        "size": 200,
                        "timestamp": int(observed_at.timestamp()),
                        "title": "Will the Fed cut interest rates?",
                    }
                ]
            return []

        with mock.patch.object(m4, "fetch_json", side_effect=fake_fetch):
            start, _, _, _ = m4.fetch_peer_sources(self.config, observed_at)
        market_query = next(query for query in queries if "market" in query)
        self.assertEqual(market_query["start"], [str(start)])
        self.assertEqual(market_query["end"], [str(int(observed_at.timestamp()))])

    def test_peer_matching_requires_distinct_exact_markets_within_six_hours(self):
        reference = self.config["peer_discovery"]["reference_wallets"][0]
        peer, repeated = "0x" + "3" * 40, "0x" + "4" * 40

        def action(wallet, suffix, timestamp, direction=0):
            return {
                "wallet": wallet,
                "condition_id": "0x" + suffix * 64,
                "event_slug": f"event-{suffix}",
                "title": "Will the Fed cut interest rates?",
                "name": None,
                "pseudonym": None,
                "started_at": timestamp,
                "ended_at": timestamp,
                "direction_outcome_index": direction,
                "gross_notional_usd": 100.0,
                "net_directional_notional_usd": 100.0,
                "net_contracts": 200.0,
                "minimum_observed_price": 0.5,
                "maximum_observed_price": 0.5,
                "source_trade_count": 1,
            }

        references = [action(reference, suffix, 10_000) for suffix in ("a", "b", "c")]
        markets = [action(peer, suffix, 10_000 + 6 * 3600) for suffix in ("a", "b", "c")]
        markets += [action(repeated, "a", 10_100), action(repeated, "a", 11_000)]
        markets.append(action("0x" + "5" * 40, "b", 10_000 + 6 * 3600 + 1))
        candidates = m4.match_peer_actions(references, markets, self.config)
        self.assertEqual([candidate["wallet"] for candidate in candidates], [peer])
        self.assertEqual(candidates[0]["shared_target_market_count"], 3)
        self.assertEqual(candidates[0]["median_signed_time_delta_seconds"], 6 * 3600)
        self.assertFalse(candidates[0]["synchronous_follower_warning"])

    def test_peer_matching_flags_synchronous_followers_without_rejecting_them(self):
        reference = self.config["peer_discovery"]["reference_wallets"][0]
        peer = "0x" + "7" * 40

        def action(wallet, suffix, timestamp):
            return {
                "wallet": wallet,
                "condition_id": "0x" + suffix * 64,
                "event_slug": f"event-{suffix}",
                "title": "Will the Fed cut interest rates?",
                "name": None,
                "pseudonym": None,
                "started_at": timestamp,
                "ended_at": timestamp,
                "direction_outcome_index": 0,
                "gross_notional_usd": 100.0,
                "net_directional_notional_usd": 100.0,
                "net_contracts": 200.0,
                "minimum_observed_price": 0.5,
                "maximum_observed_price": 0.5,
                "source_trade_count": 1,
            }

        references = [action(reference, suffix, 10_000) for suffix in ("a", "b", "c")]
        markets = [action(peer, suffix, 10_022) for suffix in ("a", "b", "c")]
        candidates = m4.match_peer_actions(references, markets, self.config)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["within_one_minute_match_count"], 3)
        self.assertEqual(candidates[0]["peer_action_after_reference_count"], 3)
        self.assertTrue(candidates[0]["synchronous_follower_warning"])

    def test_peer_report_never_auto_approves_a_team(self):
        wallet = "0x" + "6" * 40
        raw = {
            "reference_sources": {"ref": {"trades": [], "error": None}},
            "reference_material_action_count": 3,
            "market_sources": {"market": {"trades": [], "error": None}},
            "candidate_evidence": {
                wallet: {
                    "profile": {"name": "Macro Peer"},
                    "trades": [],
                    "closed_positions": [],
                    "errors": {},
                }
            },
        }
        candidate = {
            "wallet": wallet,
            "user_name": "Macro Peer",
            "pseudonym": None,
            "matching_action_count": 3,
            "matched_gross_notional_usd": 300.0,
            "matched_net_directional_notional_usd": 300.0,
            "shared_target_market_count": 3,
            "median_absolute_time_delta_seconds": 60.0,
            "markets": [],
        }
        report = m4.build_peer_report(
            raw, [candidate], self.config, datetime(2026, 8, 15, tzinfo=timezone.utc)
        )
        self.assertFalse(report["team"]["team_ready"])
        self.assertEqual(report["tracking"]["status"], "not_started")
        self.assertTrue(report["eligibility"]["price_filter"].startswith("none"))

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
