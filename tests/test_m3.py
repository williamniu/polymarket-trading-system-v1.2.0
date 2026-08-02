import copy
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import m3


ROOT = Path(__file__).resolve().parents[1]


class M3Test(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config" / "m3.json").read_text(encoding="utf-8"))
        self.policy = json.loads(
            (ROOT / "config" / "risk-policy.json").read_text(encoding="utf-8")
        )
        self.now = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        self.connection = sqlite3.connect(":memory:")
        m3.init_database(self.connection, now=self.now)

    def tearDown(self):
        self.connection.close()

    def order(self, **changes):
        order = {
            "order_id": "order-1",
            "venue": "kalshi",
            "market_id": "KX-TEST",
            "event_id": "event-1",
            "theme_id": "theme-1",
            "outcome": "yes",
            "action": "buy",
            "order_type": "marketable_limit",
            "quantity": "10",
            "limit_price": "0.56",
            "tick_size": "0.01",
            "fee_rule_id": "kalshi-general-2026-02-05",
            "decision_at": self.now.isoformat(),
            "latency_ms": 1000,
        }
        order.update(changes)
        return order

    def book(self, offset_ms=1100, **changes):
        values = {
            "venue": "kalshi",
            "market_id": "KX-TEST",
            "event_id": "event-1",
            "theme_id": "theme-1",
            "outcome": "yes",
            "tick_size": "0.01",
            "fee_rule_id": "kalshi-general-2026-02-05",
            "observed_at": (self.now + timedelta(milliseconds=offset_ms)).isoformat(),
            "bids": [["0.50", "100"]],
            "asks": [["0.55", "100"]],
            "state": "open",
        }
        values.update(changes)
        return m3.make_book(**values)

    def test_binary_book_normalization_is_complementary(self):
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.55", "8"]],
            }
        }
        instrument = {
            "market_id": "KX",
            "event_id": "event",
            "theme_id": "theme",
            "tick_size": "0.01",
            "fee_rule_id": "kalshi-general-2026-02-05",
        }
        yes = m3.normalize_kalshi_book(payload, instrument, "yes", self.now)
        no = m3.normalize_kalshi_book(payload, instrument, "no", self.now)
        self.assertEqual(yes["bids"], [["0.40", "10"]])
        self.assertEqual(yes["asks"], [["0.45", "8"]])
        self.assertEqual(no["bids"], [["0.55", "8"]])
        self.assertEqual(no["asks"], [["0.60", "10"]])

    def test_polymarket_no_book_complements_yes_book(self):
        payload = {
            "marketData": {
                "state": "MARKET_STATE_OPEN",
                "bids": [{"px": {"value": "0.40"}, "qty": "10"}],
                "offers": [{"px": {"value": "0.45"}, "qty": "8"}],
            }
        }
        instrument = {
            "market_id": "slug",
            "event_id": "event",
            "theme_id": "theme",
            "tick_size": "0.01",
            "fee_rule_id": "polymarket-us-general-2026-04-03",
        }
        no = m3.normalize_polymarket_book(payload, instrument, "no", self.now)
        self.assertEqual(no["bids"], [["0.55", "8"]])
        self.assertEqual(no["asks"], [["0.60", "10"]])
        self.assertEqual(no["state"], "open")

    def test_fee_rules_round_by_venue_and_ignore_rebate(self):
        self.assertEqual(
            m3.fee_for_fill("polymarket_us", "taker", 100, "0.50", self.config),
            Decimal("1.25"),
        )
        self.assertEqual(
            m3.fee_for_fill("kalshi", "taker", 1, "0.50", self.config),
            Decimal("0.02"),
        )
        self.assertEqual(
            m3.fee_for_fill("polymarket_us", "maker", 100, "0.50", self.config),
            Decimal("0"),
        )

    def test_first_post_latency_book_wins_not_later_favorable_book(self):
        books = [
            self.book(500, asks=[["0.52", "100"]]),
            self.book(1100, asks=[["0.55", "100"]]),
            self.book(1200, asks=[["0.53", "100"]]),
        ]
        result = m3.simulate_immediate(self.order(quantity="2"), books, self.config)
        self.assertEqual(result["average_price"], "0.55")
        self.assertEqual(result["fills"][0]["evidence_hash"], books[1]["evidence_hash"])

    def test_immediate_order_uses_haircut_depth_and_never_midpoint(self):
        book = self.book(asks=[["0.55", "10"], ["0.56", "4"]])
        result = m3.simulate_immediate(self.order(quantity="8"), [book], self.config)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["filled_quantity"], "7")
        self.assertEqual([fill["price"] for fill in result["fills"]], ["0.55", "0.56"])
        self.assertNotIn("0.525", [fill["price"] for fill in result["fills"]])

    def test_same_inputs_replay_to_the_same_result(self):
        order = self.order(quantity="8")
        books = [self.book(asks=[["0.55", "10"], ["0.56", "4"]])]
        self.assertEqual(
            m3.simulate_immediate(order, books, self.config),
            m3.simulate_immediate(order, books, self.config),
        )

    def test_stale_halted_reordered_or_tampered_books_fail_closed(self):
        with self.assertRaisesRegex(m3.EvidenceError, "too late"):
            m3.simulate_immediate(self.order(), [self.book(4000)], self.config)
        with self.assertRaisesRegex(m3.EvidenceError, "not open"):
            m3.simulate_immediate(self.order(), [self.book(state="halted")], self.config)
        reordered = [self.book(1200), self.book(1100)]
        with self.assertRaisesRegex(m3.EvidenceError, "reordered"):
            m3.simulate_immediate(self.order(), reordered, self.config)
        tampered = self.book()
        tampered["asks"][0][1] = "1000000"
        with self.assertRaisesRegex(m3.EvidenceError, "hash mismatch"):
            m3.simulate_immediate(self.order(), [tampered], self.config)

    def test_resealed_crossed_book_still_fails_validation(self):
        crossed = self.book()
        crossed["asks"] = [["0.49", "10"]]
        crossed = m3.seal(crossed)
        with self.assertRaisesRegex(m3.EvidenceError, "locked or crossed"):
            m3.simulate_immediate(self.order(), [crossed], self.config)
        off_tick = self.book()
        off_tick["asks"] = [["0.551", "10"]]
        off_tick = m3.seal(off_tick)
        with self.assertRaisesRegex(m3.EvidenceError, "instrument tick"):
            m3.simulate_immediate(self.order(), [off_tick], self.config)

    def test_book_metadata_prevents_risk_bucket_or_tick_spoofing(self):
        with self.assertRaisesRegex(m3.EvidenceError, "instrument metadata"):
            m3.simulate_immediate(
                self.order(event_id="fake-event"), [self.book()], self.config
            )
        with self.assertRaisesRegex(ValueError, "tick is invalid"):
            m3.simulate_immediate(
                self.order(tick_size="0.005"), [self.book()], self.config
            )
        with self.assertRaisesRegex(ValueError, "fee rule"):
            m3.simulate_immediate(
                self.order(fee_rule_id="invented-free-fee"), [self.book()], self.config
            )
        with self.assertRaisesRegex(ValueError, "processing buffer"):
            m3.simulate_immediate(self.order(latency_ms=0), [self.book()], self.config)

    def test_ledger_revalidates_fills_instead_of_trusting_a_sealed_result(self):
        order = self.order(quantity="10", limit_price="0.55")
        result = m3.simulate_immediate(order, [self.book()], self.config)
        forged = copy.deepcopy(result)
        forged["fills"][0]["fee"] = "0"
        forged["fees"] = "0"
        forged = m3.seal(forged)
        with self.assertRaisesRegex(m3.EvidenceError, "fee does not match"):
            m3.record_result(self.connection, order, forged, self.config, self.policy)

    def test_resting_touch_does_not_jump_queue(self):
        order = self.order(
            order_type="resting_limit",
            limit_price="0.50",
            quantity="3",
            expires_at=(self.now + timedelta(minutes=15)).isoformat(),
        )
        book = self.book(bids=[["0.50", "10"]], asks=[["0.60", "20"]])
        first = m3.make_trade(
            "kalshi", "KX-TEST", "yes", self.now + timedelta(seconds=2), "0.50", "10", "sell"
        )
        result = m3.simulate_resting(
            order, [book], [first], self.now + timedelta(minutes=1), self.config
        )
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["filled_quantity"], "0")
        second = m3.make_trade(
            "kalshi", "KX-TEST", "yes", self.now + timedelta(seconds=3), "0.50", "3", "sell"
        )
        result = m3.simulate_resting(
            order, [book], [first, second], self.now + timedelta(minutes=1), self.config
        )
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["filled_quantity"], "3")
        self.assertEqual(result["fills"][0]["fee"], "0.02")

    def test_resting_order_without_complete_evidence_is_not_called_unfilled(self):
        order = self.order(
            order_type="resting_limit",
            limit_price="0.50",
            quantity="3",
            expires_at=(self.now + timedelta(minutes=15)).isoformat(),
        )
        result = m3.simulate_resting(
            order, [self.book(asks=[["0.60", "20"]])], [], self.now + timedelta(minutes=1), self.config
        )
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["reserved_quantity"], "3")
        result = m3.simulate_resting(
            order, [self.book(asks=[["0.60", "20"]])], [], self.now + timedelta(minutes=16), self.config
        )
        self.assertEqual(result["status"], "expired")

    def test_reordered_or_tampered_trades_fail_closed(self):
        order = self.order(
            order_type="resting_limit",
            limit_price="0.50",
            expires_at=(self.now + timedelta(minutes=15)).isoformat(),
        )
        first = m3.make_trade(
            "kalshi", "KX-TEST", "yes", self.now + timedelta(seconds=3), "0.50", "2", "sell"
        )
        second = m3.make_trade(
            "kalshi", "KX-TEST", "yes", self.now + timedelta(seconds=2), "0.50", "2", "sell"
        )
        with self.assertRaisesRegex(m3.EvidenceError, "reordered"):
            m3.simulate_resting(
                order, [self.book()], [first, second], self.now + timedelta(minutes=1), self.config
            )
        first["quantity"] = "200"
        with self.assertRaisesRegex(m3.EvidenceError, "hash mismatch"):
            m3.simulate_resting(
                order, [self.book()], [first], self.now + timedelta(minutes=1), self.config
            )

    def test_ledger_records_fill_position_cash_and_reconciles(self):
        order = self.order(quantity="10", limit_price="0.55")
        result = m3.simulate_immediate(order, [self.book()], self.config)
        recorded = m3.record_result(self.connection, order, result, self.config, self.policy)
        self.assertEqual(recorded["execution_status"], "filled")
        account = m3.account_row(self.connection)
        self.assertEqual(Decimal(str(account["cash"])), Decimal("4994.32"))
        self.assertEqual(Decimal(str(account["equity"])), Decimal("4999.82"))
        position = m3.position_row(self.connection, order)
        self.assertEqual(position["quantity"], "10")
        self.assertEqual(position["cost_basis"], "5.68")
        self.assertEqual(m3.reconcile(self.connection)["status"], "ok")

    def test_duplicate_order_and_oversell_are_rejected(self):
        order = self.order(quantity="10", limit_price="0.55")
        result = m3.simulate_immediate(order, [self.book()], self.config)
        m3.record_result(self.connection, order, result, self.config, self.policy)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "duplicate"):
            m3.record_result(self.connection, order, result, self.config, self.policy)
        sell = self.order(
            order_id="sell-too-much",
            action="sell",
            quantity="11",
            limit_price="0.50",
        )
        sell_result = m3.simulate_immediate(sell, [self.book()], self.config)
        with self.assertRaisesRegex(m3.RiskError, "exceeds"):
            m3.record_result(self.connection, sell, sell_result, self.config, self.policy)

    def test_valid_partial_exit_updates_basis_and_realized_pnl(self):
        buy = self.order(quantity="10", limit_price="0.55")
        m3.record_result(
            self.connection,
            buy,
            m3.simulate_immediate(buy, [self.book()], self.config),
            self.config,
            self.policy,
        )
        sell = self.order(
            order_id="sell-5",
            action="sell",
            quantity="5",
            limit_price="0.50",
        )
        recorded = m3.record_result(
            self.connection,
            sell,
            m3.simulate_immediate(sell, [self.book()], self.config),
            self.config,
            self.policy,
        )
        self.assertEqual(recorded["execution_status"], "filled")
        position = m3.position_row(self.connection, sell)
        self.assertEqual(position["quantity"], "5")
        self.assertEqual(position["cost_basis"], "2.84")
        order_row = self.connection.execute(
            "SELECT realized_pnl FROM paper_orders WHERE order_id = 'sell-5'"
        ).fetchone()
        self.assertEqual(order_row["realized_pnl"], "-0.43")
        self.assertEqual(m3.reconcile(self.connection)["status"], "ok")

    def test_per_trade_and_event_risk_limits_are_enforced(self):
        oversized = self.order(quantity="200", limit_price="0.55")
        oversized_result = m3.simulate_immediate(
            oversized, [self.book(asks=[["0.55", "1000"]])], self.config
        )
        with self.assertRaisesRegex(m3.RiskError, "maximum loss per trade"):
            m3.record_result(
                self.connection, oversized, oversized_result, self.config, self.policy
            )
        for index in range(5):
            order = self.order(
                order_id=f"event-{index}",
                market_id=f"KX-{index}",
                quantity="80",
                limit_price="0.55",
            )
            book = self.book(
                market_id=f"KX-{index}", asks=[["0.55", "1000"]]
            )
            result = m3.simulate_immediate(order, [book], self.config)
            m3.record_result(self.connection, order, result, self.config, self.policy)
        sixth = self.order(
            order_id="event-sixth", market_id="KX-6", quantity="80", limit_price="0.55"
        )
        sixth_result = m3.simulate_immediate(
            sixth, [self.book(market_id="KX-6", asks=[["0.55", "1000"]])], self.config
        )
        with self.assertRaisesRegex(m3.RiskError, "event risk"):
            m3.record_result(self.connection, sixth, sixth_result, self.config, self.policy)
        with self.assertRaisesRegex(m3.RiskError, "maximum loss per trade"):
            m3.preflight_risk(
                self.connection,
                self.order(order_id="actual-fee-floor", quantity="1", limit_price="0.55"),
                self.config,
                self.policy,
                candidate_floor="200",
            )

    def test_final_binary_settlement_is_once_only_and_reconciles(self):
        order = self.order(quantity="10", limit_price="0.55")
        result = m3.simulate_immediate(order, [self.book()], self.config)
        m3.record_result(self.connection, order, result, self.config, self.policy)
        settlement = m3.make_settlement(
            "settlement-1", "kalshi", "KX-TEST", "yes", "1", self.now + timedelta(days=1)
        )
        recorded = m3.record_settlement(self.connection, settlement, self.config)
        self.assertEqual(recorded["payout"], "10.00")
        self.assertEqual(recorded["cash"], "5004.32")
        self.assertIsNone(m3.position_row(self.connection, order))
        with self.assertRaises(sqlite3.IntegrityError):
            m3.record_settlement(self.connection, settlement, self.config)

    def test_executable_liquidation_mark_triggers_user_risk_freeze(self):
        policy = copy.deepcopy(self.policy)
        policy["limits_pct"]["daily_hard_stop"] = 0.5
        order = self.order(quantity="80", limit_price="0.55")
        result = m3.simulate_immediate(
            order, [self.book(asks=[["0.55", "1000"]])], self.config
        )
        m3.record_result(self.connection, order, result, self.config, policy)
        mark_time = self.now + timedelta(seconds=2)
        mark = self.book(
            offset_ms=2000,
            bids=[["0.20", "1000"]],
            asks=[["0.30", "1000"]],
        )
        status = m3.mark_to_market(
            self.connection, [mark], mark_time, self.config, policy
        )
        self.assertTrue(status["frozen"])
        self.assertIn("daily_hard_stop", status["reasons"])
        self.assertEqual(Decimal(status["equity"]), Decimal("4969.71"))
        with self.assertRaisesRegex(m3.RiskError, "frozen"):
            m3.preflight_risk(
                self.connection,
                self.order(order_id="blocked-after-freeze", quantity="1"),
                self.config,
                policy,
            )

    def test_scalar_or_nonfinal_settlement_fails_closed(self):
        scalar = m3.make_settlement(
            "scalar", "kalshi", "KX-TEST", "yes", "0.5", self.now, final=True
        )
        with self.assertRaisesRegex(m3.EvidenceError, "scalar"):
            m3.record_settlement(self.connection, scalar, self.config)
        pending = m3.make_settlement(
            "pending", "kalshi", "KX-TEST", "yes", "1", self.now, final=False
        )
        with self.assertRaisesRegex(m3.EvidenceError, "not final"):
            m3.record_settlement(self.connection, pending, self.config)
        wrong_venue = m3.make_settlement(
            "wrong", "invented", "KX-TEST", "yes", "1", self.now, final=True
        )
        with self.assertRaisesRegex(m3.EvidenceError, "identity"):
            m3.record_settlement(self.connection, wrong_venue, self.config)

    def test_settlement_source_identity_must_match_the_position_market(self):
        source = m3.seal(
            {
                "venue": "kalshi",
                "market_id": "KX-WRONG",
                "observed_at": self.now.isoformat(),
                "payload": {"result": "yes"},
            }
        )
        settlement = m3.make_settlement(
            "wrong-source",
            "kalshi",
            "KX-TEST",
            "yes",
            "1",
            self.now,
            source=source,
        )
        with self.assertRaisesRegex(m3.EvidenceError, "source identity"):
            m3.record_settlement(self.connection, settlement, self.config)

    def test_reconciliation_detects_cash_tampering(self):
        self.connection.execute(
            "UPDATE paper_accounts SET cash = cash + 1 WHERE account_id = 'paper-v1'"
        )
        with self.assertRaisesRegex(RuntimeError, "reconciliation failed"):
            m3.reconcile(self.connection)

    def test_configuration_is_paper_shadow_only(self):
        self.assertEqual(m3.check_configuration(self.config, self.policy)["status"], "ok")
        changed = copy.deepcopy(self.config)
        changed["mode"] = "live"
        with self.assertRaisesRegex(RuntimeError, "paper shadow"):
            m3.check_configuration(changed, self.policy)
        changed = copy.deepcopy(self.config)
        changed["venue_rules"]["kalshi"]["taker_theta"] = "0"
        with self.assertRaisesRegex(RuntimeError, "fee rule changed"):
            m3.check_configuration(changed, self.policy)
        changed = copy.deepcopy(self.config)
        changed["resting_queue_multiplier"] = "0.5"
        with self.assertRaisesRegex(RuntimeError, "queue multiplier"):
            m3.check_configuration(changed, self.policy)
        changed = copy.deepcopy(self.config)
        changed["runtime_probe"]["quantity"] = "2"
        with self.assertRaisesRegex(RuntimeError, "runtime probe"):
            m3.check_configuration(changed, self.policy)

    def test_m3_source_has_no_network_order_or_active_runtime_path(self):
        source = (ROOT / "m3.py").read_text(encoding="utf-8")
        for forbidden in (
            "urlopen",
            "https://",
            "launchctl",
            "m2.DB_PATH",
            "m2.connect(",
            "/v1/orders",
            "/portfolio/orders",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
