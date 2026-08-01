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


ROOT = Path(__file__).resolve().parents[1]


class M2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state.sqlite3"
        self.lock_path = self.root / "writer.lock"
        self.backup_dir = self.root / "backups"
        self.patches = [
            patch.object(m2, "DB_PATH", self.db_path),
            patch.object(m2, "LOCK_PATH", self.lock_path),
            patch.object(m2, "BACKUP_DIR", self.backup_dir),
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
                result = m2.run_cycle()
        self.assertEqual(result, 1)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT status FROM cycles").fetchone()[0], "failed")
            self.assertEqual(connection.execute("SELECT code FROM alerts").fetchone()[0], "cycle_failure")

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
        self.assertEqual(plist["ProgramArguments"][-1], "cycle")
        self.assertNotIn("KeepAlive", plist)

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


if __name__ == "__main__":
    unittest.main()
