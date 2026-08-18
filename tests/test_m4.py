import copy
import hashlib
import json
import plistlib
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
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
        self.assertEqual(self.config["version"], 4)
        self.assertEqual(self.config["maximum_candidates_to_audit"], 50)
        self.assertNotIn("price", self.config["peer_discovery"])
        self.assertNotIn("minimum_hours_to_resolution", self.config["peer_discovery"])
        tracking = self.config["prospective_tracking"]
        self.assertEqual(len(tracking["panel"]), 5)
        self.assertEqual(
            {row["role"] for row in tracking["panel"]},
            {
                "reference",
                "primary_challenger",
                "high_activity_comparator",
                "identity_comparator",
                "follower_control",
            },
        )
        self.assertNotIn("price", tracking)
        self.assertNotIn("minimum_hours_to_resolution", tracking)
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

    def test_tracking_launch_agent_is_independent_and_uses_python_311(self):
        with (ROOT / "ops" / "com.williamniu.polymarket-m4.plist").open("rb") as handle:
            agent = plistlib.load(handle)
        tracking = self.config["prospective_tracking"]
        self.assertEqual(agent["ProgramArguments"][0], "/opt/homebrew/bin/python3.11")
        self.assertEqual(agent["ProgramArguments"][2], "tracking-cycle")
        self.assertEqual(agent["StartInterval"], tracking["service_interval_seconds"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertNotIn("KeepAlive", agent)

    def test_tracking_log_is_append_only_hash_chained_and_fails_closed(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            records = []
            m4.append_tracking_record("one", {"value": 1}, now, path, records)
            m4.append_tracking_record(
                "two", {"value": 2}, now + timedelta(seconds=1), path, records
            )
            self.assertEqual(m4.read_tracking_records(path), records)
            lines = path.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(lines[0])
            tampered["payload"]["value"] = 9
            lines[0] = json.dumps(tampered)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "broken M4 tracking evidence chain"):
                m4.read_tracking_records(path)

    def test_tracking_configuration_is_frozen_after_clock_start(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            start, created = m4.initialize_tracking(self.config, now, path)
            self.assertTrue(created)
            again, created = m4.initialize_tracking(self.config, now, path)
            self.assertFalse(created)
            self.assertEqual(again["record_hash"], start["record_hash"])
            changed = copy.deepcopy(self.config)
            changed["prospective_tracking"]["delayed_quote_seconds"] = 600
            with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                m4.initialize_tracking(changed, now, path)

    def test_order_book_requires_exact_identity_and_real_executable_sides(self):
        condition, token = "0x" + "a" * 64, "123"
        book = {
            "market": condition,
            "asset_id": token,
            "bids": [
                {"price": "0.40", "size": "10"},
                {"price": "0.45", "size": "20"},
            ],
            "asks": [
                {"price": "0.60", "size": "30"},
                {"price": "0.55", "size": "40"},
            ],
            "tick_size": "0.01",
            "min_order_size": "5",
        }
        result = m4.normalized_book(book, condition, token)
        self.assertEqual(result["best_bid"], 0.45)
        self.assertEqual(result["best_ask"], 0.55)
        self.assertEqual(result["spread"], 0.1)
        with self.assertRaisesRegex(ValueError, "identity"):
            m4.normalized_book({**book, "asset_id": "456"}, condition, token)
        with self.assertRaisesRegex(ValueError, "crossed"):
            m4.normalized_book(
                {
                    **book,
                    "bids": [{"price": "0.70", "size": "10"}],
                    "asks": [{"price": "0.60", "size": "10"}],
                },
                condition,
                token,
            )

    def test_tracking_cycle_reconstructs_then_delays_without_a_signal(self):
        start = datetime(2026, 8, 18, tzinfo=timezone.utc)
        panel = self.config["prospective_tracking"]["panel"]
        wallet = panel[0]["wallet"]
        condition = "0x" + "a" * 64
        trade = {
            "proxyWallet": wallet,
            "conditionId": condition,
            "transactionHash": "0x" + "b" * 64,
            "side": "BUY",
            "asset": "123",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "price": 0.5,
            "size": 300,
            "timestamp": int((start + timedelta(seconds=60)).timestamp()),
            "title": "Will the Fed cut interest rates?",
            "slug": "fed-cut",
            "eventSlug": "fed-cut-event",
        }

        def sources(config, observed_at):
            rows = {
                row["wallet"]: {
                    "url": f"https://example.invalid/{row['wallet']}",
                    "trades": [trade] if row["wallet"] == wallet else [],
                    "error": None,
                }
                for row in panel
            }
            return 0, int(observed_at.timestamp()), rows

        def quote(condition_id, outcome_index, config):
            self.assertEqual(condition_id, condition)
            self.assertEqual(outcome_index, 0)
            return {
                "status": "recorded",
                "captured_at": start.isoformat(),
                "condition_id": condition_id,
                "direction_outcome_index": outcome_index,
                "end_date": (start + timedelta(days=60)).isoformat(),
                "executable": {"best_bid": 0.49, "best_ask": 0.51},
                "price_admission_gate": False,
                "time_to_resolution_admission_gate": False,
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, raw = root / "evidence.jsonl", root / "raw"
            m4.initialize_tracking(self.config, start, path)
            with mock.patch.object(m4, "fetch_tracking_sources", side_effect=sources), mock.patch.object(
                m4, "capture_executable_quote", side_effect=quote
            ):
                first = m4.tracking_cycle(
                    self.config, start + timedelta(seconds=900), path, raw
                )
                second = m4.tracking_cycle(
                    self.config, start + timedelta(seconds=2800), path, raw
                )
                third = m4.tracking_cycle(
                    self.config, start + timedelta(seconds=3100), path, raw
                )
            records = m4.read_tracking_records(path)
            self.assertEqual(first["cycle_status"], "complete")
            self.assertEqual(second["cycle_status"], "complete")
            self.assertEqual(third["cycle_status"], "idle")
            self.assertEqual(sum(row["record_type"] == "action" for row in records), 1)
            self.assertEqual(
                sum(row["record_type"] == "delayed_quote" for row in records), 1
            )
            self.assertEqual(len(list(raw.glob("trades-*.json.gz"))), 2)
            action = next(row for row in records if row["record_type"] == "action")
            self.assertFalse(action["payload"]["signal_authorized"])
            self.assertFalse(action["payload"]["paper_position_authorized"])
            self.assertFalse(third["review_gate"]["ready"])
            self.assertEqual(third["evidence_path"], str(path))

    def test_source_contains_no_order_or_credential_capability(self):
        source = (ROOT / "m4.py").read_text(encoding="utf-8").lower()
        self.assertIn("https://data-api.polymarket.com", source)
        self.assertIn("https://gamma-api.polymarket.com/public-profile", source)
        self.assertNotIn("private_key", source)
        self.assertNotIn("api_secret", source)
        self.assertNotIn("post_order", source)


if __name__ == "__main__":
    unittest.main()
