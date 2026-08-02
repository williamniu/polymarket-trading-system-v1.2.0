import json
import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import m2
import m3


ROOT = Path(__file__).resolve().parents[1]


class M2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state.sqlite3"
        self.lock_path = self.root / "writer.lock"
        self.backup_dir = self.root / "backups"
        self.import_dir = self.root / "imports"
        self.patches = [
            patch.object(m2, "DB_PATH", self.db_path),
            patch.object(m2, "LOCK_PATH", self.lock_path),
            patch.object(m2, "BACKUP_DIR", self.backup_dir),
            patch.object(m2, "IMPORT_DIR", self.import_dir),
            patch.object(m2, "RAW_DIR", self.root / "raw"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def connection(self):
        connection = m2.connect(self.db_path)
        m2.init_database(connection)
        return connection

    def test_init_is_idempotent_and_creates_one_paper_account(self):
        with self.connection() as connection:
            m2.init_database(connection)
            account = connection.execute("SELECT * FROM paper_accounts").fetchall()
        self.assertEqual(len(account), 1)
        self.assertEqual(account[0]["starting_capital"], 5000.0)
        self.assertEqual(account[0]["cash"], 5000.0)
        self.assertEqual(account[0]["frozen"], 0)

    def test_policy_capital_mismatch_fails_closed(self):
        policy_path = self.root / "risk-policy.json"
        policy = json.loads((ROOT / "config" / "risk-policy.json").read_text(encoding="utf-8"))
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        with m2.connect(self.db_path) as connection:
            m2.init_database(connection, policy_path=policy_path)
            policy["accounts"]["paper"]["starting_capital"] = 4000.0
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                m2.init_database(connection, policy_path=policy_path)

    def test_second_writer_fails_instead_of_waiting(self):
        with m2.writer_lock(self.lock_path):
            with self.assertRaisesRegex(RuntimeError, "already held"):
                with m2.writer_lock(self.lock_path):
                    pass

    def test_failed_venue_records_degraded_heartbeat_and_alert(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        snapshot = self.snapshot(now, kalshi_ok=False)
        with self.connection() as connection:
            cycle_id, status = m2.record_snapshot(
                connection, snapshot, now, now + timedelta(seconds=10), free_disk_mb=5000
            )
            heartbeat = connection.execute("SELECT * FROM heartbeats").fetchone()
            alert = connection.execute("SELECT * FROM alerts").fetchone()
        self.assertEqual(cycle_id, 1)
        self.assertEqual(status, "partial_failure")
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertEqual(alert["severity"], "high")
        self.assertEqual(alert["code"], "venue_failure")

    def test_duplicate_snapshot_is_rejected(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        snapshot = self.snapshot(now)
        with self.connection() as connection:
            m2.record_snapshot(connection, snapshot, now, free_disk_mb=5000)
            with self.assertRaises(sqlite3.IntegrityError):
                m2.record_snapshot(connection, snapshot, now, free_disk_mb=5000)
            count = connection.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        self.assertEqual(count, 1)

    def test_stale_heartbeat_and_low_disk_fail_health(self):
        observed = datetime(2026, 8, 1, tzinfo=timezone.utc)
        with self.connection() as connection:
            m2.record_snapshot(connection, self.snapshot(observed), observed, observed, free_disk_mb=5000)
            result = m2.health(
                connection,
                now=observed + timedelta(seconds=1801),
                free_disk_mb=100,
            )
        self.assertEqual(result["status"], "unhealthy")
        self.assertFalse(result["checks"]["heartbeat_fresh"])
        self.assertFalse(result["checks"]["disk"])
        self.assertFalse(result["eligible_for_m2_promotion"])

    def test_low_disk_degrades_the_recorded_cycle(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        with self.connection() as connection:
            _, status = m2.record_snapshot(
                connection, self.snapshot(now), now, now, free_disk_mb=100
            )
            heartbeat = connection.execute("SELECT * FROM heartbeats").fetchone()
            alert = connection.execute("SELECT * FROM alerts").fetchone()
        self.assertEqual(status, "partial_failure")
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertEqual(alert["code"], "low_disk")

    def test_corrupt_database_reports_unhealthy_instead_of_crashing(self):
        self.db_path.write_bytes(b"not a sqlite database")
        with redirect_stdout(StringIO()):
            result = m2.show_status(check_only=True)
        self.assertEqual(result, 1)

    def test_uncaught_collection_failure_is_persisted(self):
        with patch.object(m2.m1, "collect_snapshot", side_effect=RuntimeError("offline")):
            with redirect_stdout(StringIO()):
                result = m2.run_cycle(start_evidence=True)
        self.assertEqual(result, 1)
        with m2.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM cycles").fetchone()[0], "failed")
            self.assertEqual(connection.execute("SELECT code FROM alerts").fetchone()[0], "cycle_failure")
            self.assertIsNotNone(
                connection.execute(
                    "SELECT value FROM meta WHERE key = 'm2_evidence_started_at'"
                ).fetchone()
            )

    def test_m1_migration_archives_imports_and_is_idempotent(self):
        source = self.m1_source([self.snapshot_at(0), self.snapshot_at(15)])
        with redirect_stdout(StringIO()):
            self.assertEqual(m2.migrate_m1(source), 0)
            self.assertEqual(m2.migrate_m1(source), 0)
        with m2.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cycles").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence_imports").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM imported_cycles").fetchone()[0], 2)
            imported = connection.execute(
                "SELECT source_snapshot_count, imported_snapshot_count, duplicate_snapshot_count "
                "FROM evidence_imports"
            ).fetchone()
            self.assertEqual(tuple(imported), (2, 2, 0))
            self.assertIsNone(connection.execute("SELECT * FROM heartbeats").fetchone())
            health = m2.health(connection, free_disk_mb=5000)
            self.assertEqual(health["total_cycle_count"], 2)
            self.assertEqual(health["cycle_count"], 0)
            self.assertFalse(health["eligible_for_m2_promotion"])
            self.assertEqual(len(m2.database_snapshots(connection)), 2)
        archives = [path for path in self.import_dir.iterdir() if not path.name.startswith(".")]
        self.assertEqual(len(archives), 1)
        manifest = json.loads((archives[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("snapshots.jsonl", manifest)
        self.assertIn("raw/sample.json", manifest)

    def test_m1_migration_reconciles_existing_snapshot_as_duplicate(self):
        snapshots = [self.snapshot_at(0), self.snapshot_at(15)]
        source = self.m1_source(snapshots)
        observed = datetime.fromisoformat(snapshots[0]["collected_at"])
        with self.connection() as connection:
            m2.record_snapshot(connection, snapshots[0], observed, observed, free_disk_mb=5000)
        with redirect_stdout(StringIO()):
            m2.migrate_m1(source)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cycles").fetchone()[0], 2)
            imported = connection.execute(
                "SELECT imported_snapshot_count, duplicate_snapshot_count FROM evidence_imports"
            ).fetchone()
        self.assertEqual(imported, (1, 1))

    def test_tampered_m1_archive_fails_health_and_repeat_import(self):
        source = self.m1_source([self.snapshot_at(0)])
        with redirect_stdout(StringIO()):
            m2.migrate_m1(source)
        archive = next(path for path in self.import_dir.iterdir() if not path.name.startswith("."))
        (archive / "raw" / "sample.json").write_text("tampered\n", encoding="utf-8")
        with m2.connect(self.db_path) as connection:
            health = m2.health(connection, free_disk_mb=5000)
        self.assertFalse(health["checks"]["imported_archives"])
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(RuntimeError, "archive failed integrity"):
                m2.migrate_m1(source)

    def test_malformed_m1_log_rolls_back_all_snapshot_rows(self):
        source = self.m1_source([self.snapshot_at(0)])
        with (source / "snapshots.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{not-json}\n")
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ValueError, "corrupt M1 snapshot line 2"):
                m2.migrate_m1(source)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cycles").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence_imports").fetchone()[0], 0)

    def test_reordered_m1_timestamps_fail_closed(self):
        source = self.m1_source([self.snapshot_at(15), self.snapshot_at(0)])
        with self.assertRaisesRegex(ValueError, "not strictly increasing"):
            m2.read_m1_snapshots(source / "snapshots.jsonl")

    def test_m2_evidence_clock_starts_once_and_excludes_earlier_cycles(self):
        before = datetime(2026, 8, 1, tzinfo=timezone.utc)
        started = before + timedelta(hours=1)
        after = started + timedelta(minutes=15)
        with self.connection() as connection:
            m2.record_snapshot(connection, self.snapshot(before), before, before, free_disk_mb=5000)
            evidence_at, created = m2.mark_evidence_start(connection, started)
            repeated_at, repeated_created = m2.mark_evidence_start(
                connection, started + timedelta(days=1)
            )
            m2.record_snapshot(connection, self.snapshot(after), after, after, free_disk_mb=5000)
            health = m2.health(connection, now=after, free_disk_mb=5000)
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated_at, evidence_at)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["total_cycle_count"], 2)
        self.assertEqual(health["cycle_count"], 1)
        self.assertEqual(health["elapsed_hours"], 0.25)
        self.assertFalse(health["eligible_for_m2_promotion"])

    def test_backup_is_consistent(self):
        with self.connection():
            pass
        self.assertEqual(m2.backup_database(), 0)
        backups = list(self.backup_dir.glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0], 1)

    def test_launch_agent_is_pinned_but_not_keepalive(self):
        with (ROOT / "ops" / "com.williamniu.polymarket-m2.plist").open("rb") as handle:
            plist = plistlib.load(handle)
        config = json.loads((ROOT / "config" / "m2.json").read_text(encoding="utf-8"))
        self.assertEqual(plist["ProgramArguments"][0], "/opt/homebrew/bin/python3.11")
        self.assertEqual(plist["StartInterval"], config["interval_seconds"])
        self.assertEqual(plist["ProgramArguments"][-1], "service-cycle")
        self.assertTrue(plist["StandardOutPath"].endswith("runtime/m2/collector.log"))
        self.assertTrue(plist["StandardErrorPath"].endswith("runtime/m2/collector-error.log"))
        self.assertNotIn("KeepAlive", plist)

    def test_m3_runtime_probe_records_sealed_evidence_and_stressed_fees(self):
        now = datetime.now(timezone.utc)
        market = self.polymarket(now)
        with self.connection() as connection:
            cycle_id, _ = m2.record_snapshot(
                connection, self.snapshot(now), now, now, free_disk_mb=5000
            )

            calls = 0

            def point_book(instrument, _config):
                nonlocal calls
                observed_at = now + timedelta(milliseconds=400 * calls)
                calls += 1
                book = m3.make_book(
                        instrument["venue"],
                        instrument["market_id"],
                        instrument["event_id"],
                        instrument["theme_id"],
                        "yes",
                        instrument["tick_size"],
                        instrument["fee_rule_id"],
                        observed_at,
                        [["0.45", "100"]],
                        [["0.55", "100"]],
                    )
                raw = m3.seal(
                    {
                        "venue": instrument["venue"],
                        "market_id": instrument["market_id"],
                        "observed_at": observed_at.isoformat(),
                        "payload": {"test": True},
                    }
                )
                return (
                    book,
                    100.0,
                    raw,
                )

            with patch.object(m2, "fetch_m3_book", side_effect=point_book), patch.object(
                m2.time, "sleep"
            ):
                result = m2.run_m3_shadow_probe(
                    connection, cycle_id, {"polymarket_us": [market]}
                )
            probe = connection.execute("SELECT * FROM m3_shadow_probes").fetchone()
            execution_config = json.loads(probe["execution_config_json"])
            instrument = json.loads(probe["instrument_json"])

            self.assertEqual(result["status"], "recorded")
            self.assertEqual(probe["status"], "recorded")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM m3_latency_samples").fetchone()[0], 2)
            self.assertEqual(execution_config["venue_rules"]["polymarket_us"]["taker_theta"], "0.0625")
            self.assertEqual(execution_config["fee_stress_multiplier"], "1")
            self.assertEqual(instrument["reported_fee_coefficient"], "0.06")
            m3.verify_seal(instrument)
            m3.verify_seal(json.loads(probe["decision_raw_json"]))
            m3.verify_seal(json.loads(probe["decision_book_json"]))
            m3.verify_seal(json.loads(probe["execution_raw_json"]))
            m3.verify_seal(json.loads(probe["execution_book_json"]))
            self.assertIsNotNone(
                connection.execute(
                    "SELECT value FROM meta WHERE key = 'm3_evidence_started_at'"
                ).fetchone()
            )
            _, closing = m2.select_m3_instrument(
                connection,
                "polymarket_us",
                [market],
                now,
                json.loads((ROOT / "config" / "m3.json").read_text(encoding="utf-8")),
            )
            self.assertTrue(closing)

    def test_m3_latency_is_empirical_p95_plus_buffer(self):
        with self.connection() as connection:
            m2.init_m3_runtime(connection)
            now = datetime(2026, 8, 1, tzinfo=timezone.utc)
            first, _ = m2.record_snapshot(
                connection, self.snapshot(now), now, now, free_disk_mb=5000
            )
            second_at = now + timedelta(minutes=1)
            second, _ = m2.record_snapshot(
                connection,
                self.snapshot(second_at),
                second_at,
                second_at,
                free_disk_mb=5000,
            )
            connection.executemany(
                """
                INSERT INTO m3_latency_samples(cycle_id, venue, phase, observed_at, latency_ms)
                VALUES (?, 'kalshi', 'decision', ?, ?)
                """,
                (
                    (first, "2026-08-01T00:00:00+00:00", 100),
                    (second, "2026-08-01T00:01:00+00:00", 500),
                ),
            )
            config = json.loads((ROOT / "config" / "m3.json").read_text(encoding="utf-8"))
            self.assertEqual(m2.m3_effective_latency_ms(connection, "kalshi", 200, config), 750)

    def test_m3_failed_depth_probe_keeps_its_slow_latency_evidence(self):
        now = datetime.now(timezone.utc)
        market = self.polymarket(now)
        with self.connection() as connection:
            cycle_id, _ = m2.record_snapshot(
                connection, self.snapshot(now), now, now, free_disk_mb=5000
            )

            def thin_book(instrument, _config):
                book = m3.make_book(
                    instrument["venue"],
                    instrument["market_id"],
                    instrument["event_id"],
                    instrument["theme_id"],
                    "yes",
                    instrument["tick_size"],
                    instrument["fee_rule_id"],
                    now,
                    [["0.45", "1"]],
                    [["0.55", "1"]],
                )
                raw = m3.seal(
                    {
                        "venue": instrument["venue"],
                        "market_id": instrument["market_id"],
                        "observed_at": now.isoformat(),
                        "payload": {"thin": True},
                    }
                )
                return book, 900.0, raw

            with patch.object(m2, "fetch_m3_book", side_effect=thin_book):
                with self.assertRaisesRegex(m3.EvidenceError, "depth floor"):
                    m2.run_m3_shadow_probe(
                        connection, cycle_id, {"polymarket_us": [market]}
                    )
            self.assertEqual(
                connection.execute("SELECT latency_ms FROM m3_latency_samples").fetchone()[0],
                900.0,
            )
            self.assertEqual(
                connection.execute("SELECT status FROM m3_shadow_probes").fetchone()[0],
                "pending",
            )

    def test_m3_rejects_optimistic_or_live_polymarket_metadata(self):
        now = datetime.now(timezone.utc)
        config = json.loads((ROOT / "config" / "m3.json").read_text(encoding="utf-8"))
        market = self.polymarket(now)
        self.assertIsNotNone(m2.m3_instrument_from_market("polymarket_us", market, now, config))
        market["feeCoefficient"] = 0.07
        self.assertIsNone(m2.m3_instrument_from_market("polymarket_us", market, now, config))
        market = self.polymarket(now)
        market["orderPriceMinTickSize"] = 0.001
        self.assertIsNone(m2.m3_instrument_from_market("polymarket_us", market, now, config))
        market = self.polymarket(now)
        market["category"] = "sports"
        market["gameStartTime"] = (now - timedelta(minutes=1)).isoformat()
        self.assertIsNone(m2.m3_instrument_from_market("polymarket_us", market, now, config))
        kalshi = self.kalshi(now)
        self.assertIsNotNone(m2.m3_instrument_from_market("kalshi", kalshi, now, config))
        kalshi["occurrence_datetime"] = (now - timedelta(minutes=1)).isoformat()
        self.assertIsNone(m2.m3_instrument_from_market("kalshi", kalshi, now, config))

    def test_m3_config_change_after_clock_fails_closed(self):
        now = datetime.now(timezone.utc)
        changed_path = self.root / "m3.json"
        with self.connection() as connection:
            m2.init_m3_runtime(connection, now)
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('m3_evidence_started_at', ?)",
                (now.isoformat(),),
            )
            changed = json.loads((ROOT / "config" / "m3.json").read_text(encoding="utf-8"))
            changed["runtime_probe"]["enabled"] = False
            changed_path.write_text(json.dumps(changed), encoding="utf-8")
            with patch.object(m2, "M3_CONFIG_PATH", changed_path):
                m2.init_m3_runtime(connection, now)
            changed["depth_credit_fraction"] = "0.40"
            changed_path.write_text(json.dumps(changed), encoding="utf-8")
            with patch.object(m2, "M3_CONFIG_PATH", changed_path):
                with self.assertRaisesRegex(RuntimeError, "changed after"):
                    m2.init_m3_runtime(connection, now)

    def test_m3_failure_does_not_erase_or_fail_the_m2_cycle(self):
        now = datetime.now(timezone.utc)
        with self.connection() as connection:
            m2.init_m3_runtime(connection, now)
        snapshot = self.snapshot(now)

        def collection(**kwargs):
            kwargs["market_sink"].update({"polymarket_us": [], "kalshi": []})
            return snapshot

        with patch.object(m2.m1, "collect_snapshot", side_effect=collection), patch.object(
            m2, "run_m3_shadow_probe", side_effect=m3.EvidenceError("tampered book")
        ), redirect_stdout(StringIO()):
            self.assertEqual(m2.run_cycle(start_evidence=True), 0)
        with m2.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM cycles").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM m3_shadow_probes"
                ).fetchone()[0],
                "failed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT code FROM alerts WHERE code = 'm3_probe_failure'"
                ).fetchone()[0],
                "m3_probe_failure",
            )

    def test_m3_reconciliation_failure_freezes_only_m3(self):
        now = datetime.now(timezone.utc)
        with self.connection() as connection:
            m2.init_m3_runtime(connection, now)
            cycle_id, _ = m2.record_snapshot(
                connection, self.snapshot(now), now, now, free_disk_mb=5000
            )
            m2.record_m3_probe_failure(
                connection, cycle_id, "kalshi", "paper account reconciliation failed"
            )
            status = m2.m3_shadow_status(connection, now)
            collector = connection.execute(
                "SELECT status FROM heartbeats WHERE component = 'collector'"
            ).fetchone()[0]
        self.assertEqual(status["status"], "blocked")
        self.assertIsNotNone(status["runtime_frozen_at"])
        self.assertEqual(status["reconciliation_error_count"], 1)
        self.assertEqual(collector, "ok")

    def test_m3_runtime_source_has_no_credential_or_order_path(self):
        source = (ROOT / "m2.py").read_text(encoding="utf-8")
        for forbidden in (
            "KALSHI-ACCESS-KEY",
            "X-PM-Access-Key",
            "/portfolio/orders",
            "/v1/orders",
            "private_key",
        ):
            self.assertNotIn(forbidden, source)

    @staticmethod
    def snapshot(timestamp, kalshi_ok=True):
        return {
            "collected_at": timestamp.isoformat(),
            "venues": {
                "polymarket_us": {"ok": True, "market_count": 100},
                "kalshi": {"ok": kalshi_ok, "market_count": 100},
                "kalshi_demo_probe": {"ok": True, "market_count": 1},
            },
        }

    @staticmethod
    def polymarket(now):
        return {
            "id": "1",
            "slug": "test-market",
            "question": "Test event winner",
            "category": "politics",
            "description": "Official test rules",
            "active": True,
            "closed": False,
            "comboEnabled": False,
            "outcomes": '["Yes","No"]',
            "orderPriceMinTickSize": 0.01,
            "minimumTradeQty": 1,
            "feeCoefficient": 0.06,
            "endDate": (now + timedelta(days=10)).isoformat(),
            "bestBidQuote": {"value": "0.45", "currency": "USD"},
            "bestAskQuote": {"value": "0.55", "currency": "USD"},
        }

    @staticmethod
    def kalshi(now):
        return {
            "ticker": "KXTEST-YES",
            "event_ticker": "KXTEST",
            "title": "Test event",
            "rules_primary": "Official test rules",
            "market_type": "binary",
            "status": "active",
            "price_level_structure": "linear_cent",
            "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
            "close_time": (now + timedelta(days=10)).isoformat(),
            "occurrence_datetime": (now + timedelta(days=9)).isoformat(),
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "yes_bid_size_fp": "100",
            "yes_ask_size_fp": "100",
            "volume_24h_fp": "1000",
        }

    def snapshot_at(self, minutes):
        return self.snapshot(datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes))

    def m1_source(self, snapshots):
        source = self.root / "m1-source"
        raw = source / "raw"
        raw.mkdir(parents=True)
        (source / "snapshots.jsonl").write_text(
            "".join(json.dumps(snapshot) + "\n" for snapshot in snapshots), encoding="utf-8"
        )
        (raw / "sample.json").write_text("{}\n", encoding="utf-8")
        return source


if __name__ == "__main__":
    unittest.main()
