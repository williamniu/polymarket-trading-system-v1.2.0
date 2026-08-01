#!/usr/bin/env python3
"""M2 single-writer, paper-only runtime foundation."""

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import m1


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime" / "m2"
DB_PATH = RUNTIME / "state.sqlite3"
LOCK_PATH = RUNTIME / "writer.lock"
BACKUP_DIR = RUNTIME / "backups"
RAW_DIR = RUNTIME / "raw"
CONFIG_PATH = ROOT / "config" / "m2.json"
RISK_PATH = ROOT / "config" / "risk-policy.json"
REQUIRED_VENUES = ("polymarket_us", "kalshi")


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    starting_capital REAL NOT NULL CHECK (starting_capital > 0),
    cash REAL NOT NULL CHECK (cash >= 0),
    equity REAL NOT NULL CHECK (equity >= 0),
    high_watermark REAL NOT NULL CHECK (high_watermark >= equity),
    frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'partial_failure', 'failed'))
);
CREATE TABLE IF NOT EXISTS venue_snapshots (
    cycle_id INTEGER NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    venue TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    metrics_json TEXT NOT NULL,
    PRIMARY KEY (cycle_id, venue)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'failed')),
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER REFERENCES cycles(cycle_id),
    created_at TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'high', 'critical')),
    code TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def utc_now():
    return datetime.now(timezone.utc)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def connect(path=None):
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def writer_lock(path=None):
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("M2 writer lock is already held") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def init_database(connection, now=None, policy_path=RISK_PATH):
    now = (now or utc_now()).isoformat()
    policy = load_json(policy_path)
    if policy.get("mode") != "paper" or policy["controls"].get("live_trading_enabled"):
        raise RuntimeError("risk policy is not paper-only")
    capital = float(policy["accounts"]["paper"]["starting_capital"])
    with connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
        )
        row = connection.execute(
            "SELECT starting_capital FROM paper_accounts WHERE account_id = 'paper-v1'"
        ).fetchone()
        if row is not None and row["starting_capital"] != capital:
            raise RuntimeError("paper capital conflicts with the approved risk policy")
        connection.execute(
            """
            INSERT OR IGNORE INTO paper_accounts(
                account_id, starting_capital, cash, equity, high_watermark,
                frozen, created_at, updated_at
            ) VALUES ('paper-v1', ?, ?, ?, ?, 0, ?, ?)
            """,
            (capital, capital, capital, capital, now, now),
        )


def record_snapshot(connection, snapshot, started_at, finished_at=None, free_disk_mb=None):
    finished_at = finished_at or utc_now()
    snapshot_at = snapshot["collected_at"]
    venues = snapshot.get("venues")
    if not isinstance(venues, dict):
        raise ValueError("snapshot venues must be an object")
    missing = [venue for venue in REQUIRED_VENUES if venue not in venues]
    if missing:
        raise ValueError(f"snapshot missing required venues: {', '.join(missing)}")
    failures = [venue for venue in REQUIRED_VENUES if not venues[venue].get("ok")]
    config = load_json(CONFIG_PATH)
    if free_disk_mb is None:
        free_disk_mb = shutil.disk_usage(ROOT).free / (1024 * 1024)
    disk_low = free_disk_mb < config["minimum_free_disk_mb"]
    status = "partial_failure" if failures or disk_low else "ok"

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO cycles(snapshot_at, started_at, finished_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (snapshot_at, started_at.isoformat(), finished_at.isoformat(), status),
        )
        cycle_id = cursor.lastrowid
        for venue, metrics in venues.items():
            connection.execute(
                """
                INSERT INTO venue_snapshots(cycle_id, venue, ok, metrics_json)
                VALUES (?, ?, ?, ?)
                """,
                (cycle_id, venue, int(bool(metrics.get("ok"))), json.dumps(metrics, sort_keys=True)),
            )
        heartbeat_status = "degraded" if failures or disk_low else "ok"
        details = []
        if failures:
            details.append("failed venues: " + ", ".join(failures))
        if disk_low:
            details.append(f"low disk: {free_disk_mb:.1f} MB")
        detail = "; ".join(details) if details else "collection complete"
        connection.execute(
            """
            INSERT INTO heartbeats(component, observed_at, status, detail)
            VALUES ('collector', ?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET
                observed_at = excluded.observed_at,
                status = excluded.status,
                detail = excluded.detail
            """,
            (finished_at.isoformat(), heartbeat_status, detail),
        )
        for venue in failures:
            connection.execute(
                """
                INSERT INTO alerts(cycle_id, created_at, severity, code, message)
                VALUES (?, ?, 'high', 'venue_failure', ?)
                """,
                (cycle_id, finished_at.isoformat(), f"{venue} collection failed"),
            )
        if disk_low:
            connection.execute(
                """
                INSERT INTO alerts(cycle_id, created_at, severity, code, message)
                VALUES (?, ?, 'critical', 'low_disk', ?)
                """,
                (cycle_id, finished_at.isoformat(), f"free disk is {free_disk_mb:.1f} MB"),
            )
    return cycle_id, status


def record_failed_cycle(connection, started_at, message, finished_at=None):
    finished_at = finished_at or utc_now()
    snapshot_at = finished_at.isoformat()
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO cycles(snapshot_at, started_at, finished_at, status)
            VALUES (?, ?, ?, 'failed')
            """,
            (snapshot_at, started_at.isoformat(), finished_at.isoformat()),
        )
        cycle_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO heartbeats(component, observed_at, status, detail)
            VALUES ('collector', ?, 'failed', ?)
            ON CONFLICT(component) DO UPDATE SET
                observed_at = excluded.observed_at,
                status = excluded.status,
                detail = excluded.detail
            """,
            (finished_at.isoformat(), message),
        )
        connection.execute(
            """
            INSERT INTO alerts(cycle_id, created_at, severity, code, message)
            VALUES (?, ?, 'critical', 'cycle_failure', ?)
            """,
            (cycle_id, finished_at.isoformat(), message),
        )
    return cycle_id


def health(connection, now=None, free_disk_mb=None):
    now = now or utc_now()
    config = load_json(CONFIG_PATH)
    checks = {}
    try:
        checks["database"] = connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        checks["database"] = False

    heartbeat = None
    if checks["database"]:
        heartbeat = connection.execute(
            "SELECT observed_at, status, detail FROM heartbeats WHERE component = 'collector'"
        ).fetchone()
    if heartbeat:
        observed = datetime.fromisoformat(heartbeat["observed_at"])
        age_seconds = max(0.0, (now - observed).total_seconds())
        checks["heartbeat_fresh"] = age_seconds <= config["stale_after_seconds"]
        checks["last_cycle_ok"] = heartbeat["status"] == "ok"
    else:
        age_seconds = None
        checks["heartbeat_fresh"] = False
        checks["last_cycle_ok"] = False

    if free_disk_mb is None:
        free_disk_mb = shutil.disk_usage(ROOT).free / (1024 * 1024)
    checks["disk"] = free_disk_mb >= config["minimum_free_disk_mb"]

    cycle_count = 0
    elapsed_hours = 0.0
    if checks["database"]:
        row = connection.execute(
            "SELECT COUNT(*) AS count, MIN(started_at) AS first, MAX(finished_at) AS last FROM cycles"
        ).fetchone()
        cycle_count = row["count"]
        if row["first"] and row["last"]:
            first = datetime.fromisoformat(row["first"])
            last = datetime.fromisoformat(row["last"])
            elapsed_hours = max(0.0, (last - first).total_seconds() / 3600)
    healthy = all(checks.values())
    promotion_ready = (
        healthy
        and cycle_count >= config["minimum_cycles"]
        and elapsed_hours >= config["evidence_duration_hours"]
    )
    return {
        "status": "ok" if healthy else "unhealthy",
        "checks": checks,
        "heartbeat_age_seconds": age_seconds,
        "free_disk_mb": round(free_disk_mb, 1),
        "cycle_count": cycle_count,
        "elapsed_hours": round(elapsed_hours, 3),
        "eligible_for_m2_promotion": promotion_ready,
    }


def run_cycle():
    started_at = utc_now()
    with writer_lock():
        with connect() as connection:
            init_database(connection, started_at)
            try:
                snapshot = m1.collect_snapshot(raw_dir=RAW_DIR)
                cycle_id, status = record_snapshot(connection, snapshot, started_at)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                cycle_id = record_failed_cycle(connection, started_at, message)
                print(json.dumps({"cycle_id": cycle_id, "status": "failed", "error": message}))
                return 1
    print(json.dumps({"cycle_id": cycle_id, "status": status, "snapshot_at": snapshot["collected_at"]}))
    return 0 if status == "ok" else 1


def initialize():
    with writer_lock():
        with connect() as connection:
            init_database(connection)
    print(json.dumps({"database": str(DB_PATH), "status": "initialized"}))
    return 0


def show_status(check_only=False):
    if not DB_PATH.exists():
        result = {"status": "unhealthy", "checks": {"database": False}, "reason": "database missing"}
    else:
        try:
            with connect() as connection:
                result = health(connection)
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            result = {
                "status": "unhealthy",
                "checks": {"database": False},
                "reason": f"{type(exc).__name__}: {exc}",
            }
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(check_only and result["status"] != "ok")


def backup_database():
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    destination = BACKUP_DIR / f"state-{stamp}.sqlite3"
    temporary = destination.with_suffix(".tmp")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with writer_lock():
        with connect() as source:
            init_database(source)
            with sqlite3.connect(temporary) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("backup integrity check failed")
        os.replace(temporary, destination)
    print(json.dumps({"backup": str(destination), "status": "ok"}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "cycle", "status", "check", "backup"))
    command = parser.parse_args().command
    if command == "init":
        return initialize()
    if command == "cycle":
        return run_cycle()
    if command == "backup":
        return backup_database()
    return show_status(check_only=command == "check")


if __name__ == "__main__":
    raise SystemExit(main())
