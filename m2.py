#!/usr/bin/env python3
"""M2 single-writer, paper-only runtime foundation."""

import argparse
import contextlib
import fcntl
import hashlib
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
IMPORT_DIR = RUNTIME / "imports"
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
CREATE TABLE IF NOT EXISTS evidence_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_sha256 TEXT NOT NULL UNIQUE,
    archive_path TEXT NOT NULL,
    source_snapshot_count INTEGER NOT NULL CHECK (source_snapshot_count >= 0),
    imported_snapshot_count INTEGER NOT NULL CHECK (imported_snapshot_count >= 0),
    duplicate_snapshot_count INTEGER NOT NULL CHECK (duplicate_snapshot_count >= 0),
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imported_cycles (
    cycle_id INTEGER PRIMARY KEY REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL
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


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    try:
        observed_at = datetime.fromisoformat(snapshot["collected_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("snapshot has an invalid collected_at") from exc
    if observed_at.tzinfo is None:
        raise ValueError("snapshot collected_at must include a timezone")
    venues = snapshot.get("venues")
    if not isinstance(venues, dict):
        raise ValueError("snapshot venues must be an object")
    missing = [venue for venue in REQUIRED_VENUES if venue not in venues]
    if missing:
        raise ValueError(f"snapshot missing required venues: {', '.join(missing)}")
    return observed_at, venues


def insert_snapshot(
    connection,
    snapshot,
    started_at,
    finished_at,
    free_disk_mb=None,
    update_heartbeat=True,
    check_disk=True,
):
    finished_at = finished_at or utc_now()
    snapshot_at = snapshot["collected_at"]
    _, venues = validate_snapshot(snapshot)
    failures = [venue for venue in REQUIRED_VENUES if not venues[venue].get("ok")]
    config = load_json(CONFIG_PATH)
    if free_disk_mb is None:
        free_disk_mb = shutil.disk_usage(ROOT).free / (1024 * 1024)
    disk_low = check_disk and free_disk_mb < config["minimum_free_disk_mb"]
    status = "partial_failure" if failures or disk_low else "ok"

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
    if update_heartbeat:
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


def record_snapshot(connection, snapshot, started_at, finished_at=None, free_disk_mb=None):
    finished_at = finished_at or utc_now()
    with connection:
        return insert_snapshot(
            connection,
            snapshot,
            started_at,
            finished_at,
            free_disk_mb=free_disk_mb,
        )


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest(root):
    return {
        str(path.relative_to(root)): {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def verify_archive(path):
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file() or any(item.is_symlink() for item in path.rglob("*")):
        return False
    expected = load_json(manifest_path)
    actual = file_manifest(path)
    actual.pop("manifest.json", None)
    return actual == expected


def read_m1_snapshots(path):
    snapshots, previous = [], None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            snapshot = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt M1 snapshot line {line_number}") from exc
        observed_at, _ = validate_snapshot(snapshot)
        if previous is not None and observed_at <= previous:
            raise ValueError(f"M1 snapshot timestamps are not strictly increasing at line {line_number}")
        snapshots.append(snapshot)
        previous = observed_at
    if not snapshots:
        raise ValueError("M1 snapshot log is empty")
    return snapshots


def archive_m1(source_dir):
    source_dir = source_dir.resolve()
    snapshots_path = source_dir / "snapshots.jsonl"
    if not snapshots_path.is_file():
        raise ValueError("M1 source has no snapshots.jsonl")
    if any(path.is_symlink() for path in source_dir.rglob("*")):
        raise ValueError("M1 source may not contain symlinks")
    source_manifest = file_manifest(source_dir)
    source_sha256 = source_manifest["snapshots.jsonl"]["sha256"]
    archive_name = f"m1-{source_sha256[:16]}"
    destination = IMPORT_DIR / archive_name
    if destination.exists():
        if not verify_archive(destination):
            raise RuntimeError("existing M1 archive failed integrity check")
        if load_json(destination / "manifest.json") != source_manifest:
            raise RuntimeError("existing M1 archive digest mismatch")
        return destination, source_sha256

    temporary = IMPORT_DIR / f".{archive_name}.tmp"
    if temporary.exists():
        raise RuntimeError(f"incomplete M1 archive already exists: {temporary}")
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, temporary, copy_function=shutil.copy2)
    copied_manifest = file_manifest(temporary)
    if copied_manifest != source_manifest or file_manifest(source_dir) != source_manifest:
        raise RuntimeError("M1 source changed during archive")
    manifest_path = temporary / "manifest.json"
    manifest_path.write_text(
        json.dumps(copied_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, destination)
    return destination, source_sha256


def migrate_m1(source_dir):
    imported_at = utc_now()
    with writer_lock():
        with connect() as connection:
            init_database(connection, imported_at)
            source_path = Path(source_dir).resolve()
            snapshots_path = source_path / "snapshots.jsonl"
            if not snapshots_path.is_file():
                raise ValueError("M1 source has no snapshots.jsonl")
            source_sha256 = sha256_file(snapshots_path)
            existing = connection.execute(
                "SELECT * FROM evidence_imports WHERE source_sha256 = ?", (source_sha256,)
            ).fetchone()
            if existing:
                if not verify_archive(Path(existing["archive_path"])):
                    raise RuntimeError("imported M1 archive failed integrity check")
                result = {"status": "already_imported", **dict(existing)}
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0

            archive_path, archived_sha256 = archive_m1(source_path)
            if archived_sha256 != source_sha256:
                raise RuntimeError("archived M1 digest mismatch")
            snapshots = read_m1_snapshots(archive_path / "snapshots.jsonl")
            imported, duplicates = 0, 0
            with connection:
                for snapshot in snapshots:
                    if connection.execute(
                        "SELECT 1 FROM cycles WHERE snapshot_at = ?", (snapshot["collected_at"],)
                    ).fetchone():
                        duplicates += 1
                        continue
                    observed_at, _ = validate_snapshot(snapshot)
                    cycle_id, _ = insert_snapshot(
                        connection,
                        snapshot,
                        observed_at,
                        observed_at,
                        update_heartbeat=False,
                        check_disk=False,
                    )
                    connection.execute(
                        "INSERT INTO imported_cycles(cycle_id, source_sha256) VALUES (?, ?)",
                        (cycle_id, source_sha256),
                    )
                    imported += 1
                connection.execute(
                    """
                    INSERT INTO evidence_imports(
                        source_sha256, archive_path, source_snapshot_count,
                        imported_snapshot_count, duplicate_snapshot_count, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_sha256,
                        str(archive_path),
                        len(snapshots),
                        imported,
                        duplicates,
                        imported_at.isoformat(),
                    ),
                )
            if imported + duplicates != len(snapshots):
                raise RuntimeError("M1 import reconciliation failed")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("database integrity failed after M1 import")
    print(
        json.dumps(
            {
                "status": "imported",
                "source_sha256": source_sha256,
                "archive_path": str(archive_path),
                "source_snapshot_count": len(snapshots),
                "imported_snapshot_count": imported,
                "duplicate_snapshot_count": duplicates,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def mark_evidence_start(connection, now=None):
    started_at = (now or utc_now()).isoformat()
    with connection:
        existing = connection.execute(
            "SELECT value FROM meta WHERE key = 'm2_evidence_started_at'"
        ).fetchone()
        if existing:
            return existing["value"], False
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('m2_evidence_started_at', ?)", (started_at,)
        )
    return started_at, True


def start_evidence():
    with writer_lock():
        with connect() as connection:
            init_database(connection)
            started_at, created = mark_evidence_start(connection)
    print(json.dumps({"evidence_started_at": started_at, "created": created}))
    return 0


def database_snapshots(connection):
    snapshots = []
    rows = connection.execute(
        """
        SELECT c.cycle_id, c.snapshot_at, v.venue, v.metrics_json
        FROM cycles AS c
        JOIN venue_snapshots AS v ON v.cycle_id = c.cycle_id
        ORDER BY c.snapshot_at, c.cycle_id, v.venue
        """
    ).fetchall()
    current_id, current = None, None
    for row in rows:
        if row["cycle_id"] != current_id:
            current = {"collected_at": row["snapshot_at"], "venues": {}}
            snapshots.append(current)
            current_id = row["cycle_id"]
        current["venues"][row["venue"]] = json.loads(row["metrics_json"])
    return snapshots


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

    total_cycle_count = 0
    cycle_count = 0
    elapsed_hours = 0.0
    evidence_started_at = None
    if checks["database"]:
        total_cycle_count = connection.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        evidence = connection.execute(
            "SELECT value FROM meta WHERE key = 'm2_evidence_started_at'"
        ).fetchone()
        evidence_started_at = evidence["value"] if evidence else None
        if evidence_started_at:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(finished_at) AS last
                FROM cycles
                WHERE started_at >= ?
                  AND cycle_id NOT IN (SELECT cycle_id FROM imported_cycles)
                """,
                (evidence_started_at,),
            ).fetchone()
            cycle_count = row["count"]
        else:
            row = None
        if row and row["last"]:
            first = datetime.fromisoformat(evidence_started_at)
            last = datetime.fromisoformat(row["last"])
            elapsed_hours = max(0.0, (last - first).total_seconds() / 3600)
        try:
            archives = connection.execute("SELECT archive_path FROM evidence_imports").fetchall()
            checks["imported_archives"] = all(
                verify_archive(Path(archive["archive_path"])) for archive in archives
            )
        except (OSError, ValueError, json.JSONDecodeError):
            checks["imported_archives"] = False
    checks["evidence_started"] = evidence_started_at is not None
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
        "total_cycle_count": total_cycle_count,
        "elapsed_hours": round(elapsed_hours, 3),
        "evidence_started_at": evidence_started_at,
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
                result["venue_validation"] = m1.build_report(
                    database_snapshots(connection), m1.load_config()
                )
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
    parser.add_argument(
        "command",
        choices=("init", "cycle", "status", "check", "backup", "migrate-m1", "start-evidence"),
    )
    parser.add_argument("source", nargs="?")
    arguments = parser.parse_args()
    command = arguments.command
    if command == "init":
        return initialize()
    if command == "cycle":
        return run_cycle()
    if command == "backup":
        return backup_database()
    if command == "start-evidence":
        return start_evidence()
    if command == "migrate-m1":
        if not arguments.source:
            parser.error("migrate-m1 requires the old runtime/m1 directory")
        return migrate_m1(arguments.source)
    return show_status(check_only=command == "check")


if __name__ == "__main__":
    raise SystemExit(main())
