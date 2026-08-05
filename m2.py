#!/usr/bin/env python3
"""M2 single-writer, paper-only runtime foundation."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
import urllib.parse
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
STDOUT_LOG = RUNTIME / "collector.log"
STDERR_LOG = RUNTIME / "collector-error.log"
CONFIG_PATH = ROOT / "config" / "m2.json"
RISK_PATH = ROOT / "config" / "risk-policy.json"
M3_CONFIG_PATH = ROOT / "config" / "m3.json"
POLYMARKET_MARKET_URL = "https://gateway.polymarket.us/v1/market/slug"
SERVICE_LABEL = "com.williamniu.polymarket-m2"
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

M3_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS m3_shadow_probes (
    probe_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    venue TEXT NOT NULL,
    market_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'recorded', 'failed', 'skipped')),
    order_id TEXT UNIQUE,
    decision_at TEXT,
    request_latency_ms REAL,
    effective_latency_ms INTEGER,
    instrument_json TEXT,
    decision_raw_json TEXT,
    decision_book_json TEXT,
    execution_raw_json TEXT,
    execution_book_json TEXT,
    execution_config_json TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(cycle_id, venue)
);
CREATE TABLE IF NOT EXISTS m3_latency_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    venue TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('decision', 'execution')),
    observed_at TEXT NOT NULL,
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
    UNIQUE(cycle_id, venue, phase)
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


def m3_config_digest(config):
    import m3

    evidence_config = json.loads(json.dumps(config))
    evidence_config["runtime_probe"].pop("enabled", None)
    return hashlib.sha256(m3.canonical_json(evidence_config).encode("utf-8")).hexdigest()


def migrate_m3_probe_key(connection):
    unique_indexes = []
    for index in connection.execute("PRAGMA index_list(m3_shadow_probes)"):
        if index["unique"]:
            unique_indexes.append(
                [
                    column["name"]
                    for column in connection.execute(
                        f"PRAGMA index_info('{index['name']}')"
                    )
                ]
            )
    if ["cycle_id"] not in unique_indexes:
        return False
    columns = (
        "probe_id, cycle_id, venue, market_id, status, order_id, decision_at, "
        "request_latency_ms, effective_latency_ms, instrument_json, "
        "decision_raw_json, decision_book_json, execution_raw_json, "
        "execution_book_json, execution_config_json, result_json, error, created_at"
    )
    with connection:
        connection.execute(
            "ALTER TABLE m3_shadow_probes RENAME TO m3_shadow_probes_cycle_unique"
        )
        connection.executescript(M3_RUNTIME_SCHEMA)
        connection.execute(
            f"INSERT INTO m3_shadow_probes({columns}) "
            f"SELECT {columns} FROM m3_shadow_probes_cycle_unique"
        )
        connection.execute("DROP TABLE m3_shadow_probes_cycle_unique")
    return True


def init_m3_runtime(connection, now=None):
    import m3

    now = now or utc_now()
    config = load_json(M3_CONFIG_PATH)
    policy = load_json(RISK_PATH)
    m3.check_configuration(config, policy)
    m3.init_database(connection, now=now)
    digest = m3_config_digest(config)
    with connection:
        connection.executescript(M3_RUNTIME_SCHEMA)
        pending = connection.execute(
            "SELECT COUNT(*) FROM m3_shadow_probes WHERE status = 'pending'"
        ).fetchone()[0]
        if pending:
            raise RuntimeError("M3 has an incomplete prior probe")
        migrate_m3_probe_key(connection)
        stored = connection.execute(
            "SELECT value FROM meta WHERE key = 'm3_config_sha256'"
        ).fetchone()
        started = connection.execute(
            "SELECT value FROM meta WHERE key = 'm3_evidence_started_at'"
        ).fetchone()
        if stored and stored["value"] != digest and started:
            raise RuntimeError("M3 configuration changed after its evidence clock started")
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('m3_config_sha256', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (digest,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('m3_segment_number', '1')"
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('m3_segment_start_probe_id', '1')"
        )
        connection.execute(
            "UPDATE meta SET value = '4' WHERE key = 'schema_version'"
        )
    return config, policy


def start_m3_evidence_segment(connection, reason, now=None):
    import m3

    reason = str(reason).strip()
    if not reason:
        raise ValueError("M3 evidence segment requires a reason")
    config, _ = init_m3_runtime(connection, now)
    if config["runtime_probe"]["enabled"]:
        raise RuntimeError("disable the M3 runtime probe before starting a new evidence segment")
    now = (now or utc_now()).isoformat()
    number = int(
        connection.execute(
            "SELECT value FROM meta WHERE key = 'm3_segment_number'"
        ).fetchone()["value"]
    )
    start_probe_id = int(
        connection.execute(
            "SELECT value FROM meta WHERE key = 'm3_segment_start_probe_id'"
        ).fetchone()["value"]
    )
    started = connection.execute(
        "SELECT value FROM meta WHERE key = 'm3_evidence_started_at'"
    ).fetchone()
    intents = connection.execute(
        "SELECT COUNT(*) FROM m3_shadow_probes AS probe "
        "JOIN paper_orders AS paper ON paper.order_id = probe.order_id "
        "WHERE probe.probe_id >= ? AND probe.status = 'recorded'",
        (start_probe_id,),
    ).fetchone()[0]
    failures = connection.execute(
        "SELECT COUNT(*) FROM m3_shadow_probes WHERE probe_id >= ? AND status = 'failed'",
        (start_probe_id,),
    ).fetchone()[0]
    if started is None and intents == 0 and failures == 0:
        return {
            "status": "already_fresh",
            "segment_number": number,
            "start_probe_id": start_probe_id,
        }
    next_probe_id = connection.execute(
        "SELECT COALESCE(MAX(probe_id), 0) + 1 FROM m3_shadow_probes"
    ).fetchone()[0]
    archive = {
        "segment_number": number,
        "config_version": config["version"],
        "config_sha256": m3_config_digest(config),
        "started_at": started["value"] if started else None,
        "ended_at": now,
        "start_probe_id": start_probe_id,
        "end_probe_id": next_probe_id - 1,
        "order_intent_count": intents,
        "failed_probe_count": failures,
        "reason": reason,
    }
    with connection:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            (f"m3_evidence_segment_archive_{number}", m3.canonical_json(archive)),
        )
        connection.execute("DELETE FROM meta WHERE key = 'm3_evidence_started_at'")
        for key, value in (
            ("m3_segment_number", str(number + 1)),
            ("m3_segment_start_probe_id", str(next_probe_id)),
            ("m3_segment_activated_at", now),
            ("m3_segment_reason", reason),
        ):
            connection.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    return {
        "status": "started",
        "archived_segment": archive,
        "segment_number": number + 1,
        "start_probe_id": next_probe_id,
    }


def m3_market_time_is_safe(value, now, minimum_minutes):
    parsed = m1.iso_datetime(value)
    return parsed is not None and (parsed - now).total_seconds() >= minimum_minutes * 60


def m3_has_binary_outcomes(market):
    outcomes = json.loads(market.get("outcomes", "null"))
    return (
        isinstance(outcomes, list)
        and len(outcomes) == 2
        and set(outcomes) == {"Yes", "No"}
    )


def m3_instrument_from_market(venue, market, now, config, closing=False):
    import m3

    probe = config["runtime_probe"]
    minimum_minutes = probe["minimum_time_to_close_minutes"]
    rule = config["venue_rules"][venue]
    try:
        if venue == "polymarket_us":
            question = str(market.get("question", "")).strip()
            category = str(market.get("category", "")).strip().lower()
            tick = m3.decimal(market.get("orderPriceMinTickSize"))
            reported_fee = m3.decimal(market.get("feeCoefficient"))
            bid = ask = None
            if not closing:
                bid = m3.raw_decimal(market.get("bestBidQuote"))
                ask = m3.raw_decimal(market.get("bestAskQuote"))
            stressed_fee = m3.decimal(rule["taker_theta"]) * m3.decimal(
                config["fee_stress_multiplier"]
            )
            if (
                market.get("active") is not True
                or market.get("closed") is not False
                or market.get("comboEnabled") is not False
                or not m3_has_binary_outcomes(market)
                or not question
                or not category
                or tick < m3.decimal(config["minimum_tick_size"])
                or m3.decimal(market.get("minimumTradeQty")) != m3.ONE
                or reported_fee > stressed_fee
                or (not closing and not m3.ZERO < bid < ask < m3.ONE)
                or (
                    not closing
                    and not m3_market_time_is_safe(
                        market.get("endDate"), now, minimum_minutes
                    )
                )
                or (
                    not closing
                    and category == "sports"
                    and not m3_market_time_is_safe(
                        market.get("gameStartTime"), now, minimum_minutes
                    )
                )
            ):
                return None
            market_id = str(market["slug"])
            event_id = "polymarket_us:question:" + hashlib.sha256(
                question.casefold().encode("utf-8")
            ).hexdigest()[:24]
            theme_id = f"polymarket_us:category:{category}"
            extra = {"reported_fee_coefficient": m3.decimal_text(reported_fee)}
        else:
            ranges = market.get("price_ranges")
            event_id = str(market.get("event_ticker", "")).strip()
            rules = str(market.get("rules_primary", "")).strip()
            if not isinstance(ranges, list) or len(ranges) != 1:
                return None
            tick = m3.decimal(ranges[0].get("step"))
            bid = ask = bid_size = ask_size = None
            if not closing:
                bid = m3.decimal(market.get("yes_bid_dollars"))
                ask = m3.decimal(market.get("yes_ask_dollars"))
                bid_size = m3.decimal(market.get("yes_bid_size_fp"))
                ask_size = m3.decimal(market.get("yes_ask_size_fp"))
            if (
                market.get("market_type") != "binary"
                or market.get("status") != "active"
                or market.get("price_level_structure") != "linear_cent"
                or not event_id
                or not rules
                or tick < m3.decimal(config["minimum_tick_size"])
                or (not closing and not m3.ZERO < bid < ask < m3.ONE)
                or (not closing and bid_size <= m3.ZERO)
                or (not closing and ask_size <= m3.ZERO)
                or (
                    not closing
                    and not m3_market_time_is_safe(
                        market.get("close_time"), now, minimum_minutes
                    )
                )
                or (
                    not closing
                    and market.get("occurrence_datetime")
                    and not m3_market_time_is_safe(
                        market["occurrence_datetime"], now, minimum_minutes
                    )
                )
            ):
                return None
            market_id = str(market["ticker"])
            theme_id = "kalshi:series:" + event_id.split("-", 1)[0].lower()
            extra = {}
        instrument = {
            "venue": venue,
            "market_id": market_id,
            "event_id": event_id,
            "theme_id": theme_id,
            "tick_size": m3.decimal_text(tick),
            "fee_rule_id": rule["rule_id"],
            "source_market": market,
            **extra,
        }
        return m3.seal(instrument)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def fetch_m3_market(venue, market_id):
    import m3

    safe_id = urllib.parse.quote(str(market_id), safe="")
    url = (
        f"{POLYMARKET_MARKET_URL}/{safe_id}"
        if venue == "polymarket_us"
        else f"{m1.KALSHI_URL}/{safe_id}"
    )
    payload, latency_ms = m1.fetch_json(url, m1.load_config()["request_timeout_seconds"])
    market = payload.get("market")
    if not isinstance(market, dict):
        raise m3.EvidenceError(f"{venue} exact market response has no market")
    identity = market.get("slug") if venue == "polymarket_us" else market.get("ticker")
    if identity != market_id:
        raise m3.EvidenceError(f"{venue} exact market identity does not match the request")
    observed_at = utc_now().isoformat()
    raw = m3.seal(
        {
            "venue": venue,
            "market_id": market_id,
            "observed_at": observed_at,
            "latency_ms": latency_ms,
            "payload": payload,
        }
    )
    return market, raw


def validate_m3_position_market(position, market):
    import m3

    venue = position["venue"]
    try:
        if venue == "polymarket_us":
            question = str(market.get("question", "")).strip()
            category = str(market.get("category", "")).strip().lower()
            event_id = "polymarket_us:question:" + hashlib.sha256(
                question.casefold().encode("utf-8")
            ).hexdigest()[:24]
            theme_id = f"polymarket_us:category:{category}"
            valid_product = (
                m3_has_binary_outcomes(market)
                and market.get("comboEnabled") is False
            )
        else:
            event_id = str(market.get("event_ticker", "")).strip()
            theme_id = "kalshi:series:" + event_id.split("-", 1)[0].lower()
            valid_product = (
                market.get("market_type") == "binary"
                and not market.get("mve_selected_legs")
            )
        if not valid_product or (event_id, theme_id) != (
            position["event_id"],
            position["theme_id"],
        ):
            raise m3.EvidenceError("official classification changed for an open probe position")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise m3.EvidenceError("open probe position metadata is invalid") from exc


def settle_m3_position(connection, position, market, metadata_raw, config, policy):
    import m3

    venue, market_id = position["venue"], position["market_id"]
    if venue == "polymarket_us":
        if market.get("closed") is not True:
            return None
        safe_id = urllib.parse.quote(market_id, safe="")
        payload, latency_ms = m1.fetch_json(
            f"{m1.POLYMARKET_URL}/{safe_id}/settlement",
            m1.load_config()["request_timeout_seconds"],
        )
        if payload.get("slug") != market_id:
            raise m3.EvidenceError("Polymarket settlement identity does not match the request")
        yes_price = m3.decimal(payload.get("settlement"), "Polymarket settlement")
        observed_at = utc_now().isoformat()
        settlement_raw = m3.seal(
            {
                "venue": venue,
                "market_id": market_id,
                "observed_at": observed_at,
                "latency_ms": latency_ms,
                "payload": payload,
            }
        )
        source = m3.seal(
            {
                "venue": venue,
                "market_id": market_id,
                "observed_at": observed_at,
                "metadata": metadata_raw,
                "settlement": settlement_raw,
            }
        )
    else:
        if market.get("status") != "finalized":
            return None
        result = market.get("result")
        if result not in ("yes", "no"):
            raise m3.EvidenceError("Kalshi finalized market has no binary result")
        yes_price = m3.ONE if result == "yes" else m3.ZERO
        reported = market.get("settlement_value_dollars")
        if reported is not None and m3.decimal(reported) != yes_price:
            raise m3.EvidenceError("Kalshi result conflicts with its settlement value")
        observed_at = metadata_raw["observed_at"]
        source = metadata_raw
    if yes_price not in (m3.ZERO, m3.ONE):
        raise m3.EvidenceError("official settlement is not binary")
    price = yes_price if position["outcome"] == "yes" else m3.ONE - yes_price
    settlement = m3.make_settlement(
        f"m3-settlement-{venue}-{market_id}-{position['outcome']}",
        venue,
        market_id,
        position["outcome"],
        price,
        observed_at,
        source=source,
    )
    return m3.record_settlement(connection, settlement, config, policy)


def prepare_m3_position(connection, venue, markets, config, policy):
    import m3

    positions = connection.execute(
        "SELECT * FROM paper_positions WHERE account_id = 'paper-v1' AND venue = ? ORDER BY updated_at",
        (venue,),
    ).fetchall()
    if len(positions) > 1:
        raise m3.RiskError("more than one open probe position exists for a venue")
    if not positions:
        return list(markets), None, None
    position = positions[0]
    market, metadata_raw = fetch_m3_market(venue, position["market_id"])
    validate_m3_position_market(position, market)
    settlement = settle_m3_position(
        connection, position, market, metadata_raw, config, policy
    )
    if settlement is not None:
        return list(markets), settlement, None
    tradeable = (
        market.get("active") is True and market.get("closed") is False
        if venue == "polymarket_us"
        else market.get("status") == "active"
    )
    if not tradeable:
        return list(markets), None, "open probe position is awaiting final settlement"
    exact = [market]
    exact.extend(
        item
        for item in markets
        if (item.get("slug") if venue == "polymarket_us" else item.get("ticker"))
        != position["market_id"]
    )
    return exact, None, None


def select_m3_instrument(connection, venue, markets, now, config):
    import m3

    positions = connection.execute(
        "SELECT * FROM paper_positions WHERE account_id = 'paper-v1' AND venue = ? ORDER BY updated_at",
        (venue,),
    ).fetchall()
    if positions:
        position = positions[0]
        matching = [
            market
            for market in markets
            if (market.get("slug") if venue == "polymarket_us" else market.get("ticker"))
            == position["market_id"]
        ]
        instrument = (
            m3_instrument_from_market(venue, matching[0], now, config, closing=True)
            if matching
            else None
        )
        if instrument is None:
            raise m3.EvidenceError("an open probe position has no current official market metadata")
        if (instrument["event_id"], instrument["theme_id"]) != (
            position["event_id"],
            position["theme_id"],
        ):
            raise m3.EvidenceError("official classification changed for an open probe position")
        return instrument, True
    instruments = [
        item
        for market in markets
        if (item := m3_instrument_from_market(venue, market, now, config)) is not None
    ]
    if not instruments:
        raise m3.EvidenceError("no eligible simple binary market exists for the probe")

    def score(instrument):
        source = instrument["source_market"]
        try:
            if venue == "polymarket_us":
                bid = m3.raw_decimal(source["bestBidQuote"])
                ask = m3.raw_decimal(source["bestAskQuote"])
                return (ask - bid, abs((ask + bid) / 2 - m3.decimal("0.5")))
            bid = m3.decimal(source["yes_bid_dollars"])
            ask = m3.decimal(source["yes_ask_dollars"])
            top = min(
                bid * m3.decimal(source["yes_bid_size_fp"]),
                ask * m3.decimal(source["yes_ask_size_fp"]),
            )
            return (-top, -m3.decimal(source.get("volume_24h_fp", "0")))
        except (KeyError, TypeError, ValueError):
            return (m3.ONE, m3.ONE)

    return min(instruments, key=score), False


def fetch_m3_book(instrument, config):
    import m3

    venue = instrument["venue"]
    outcome = config["runtime_probe"]["outcome"]
    market_id = urllib.parse.quote(instrument["market_id"], safe="")
    if venue == "polymarket_us":
        payload, latency_ms = m1.fetch_json(
            f"{m1.POLYMARKET_URL}/{market_id}/book",
            m1.load_config()["request_timeout_seconds"],
        )
        if payload.get("marketData", {}).get("marketSlug") != instrument["market_id"]:
            raise m3.EvidenceError("Polymarket book identity does not match the request")
        observed_at = utc_now()
        book = m3.normalize_polymarket_book(payload, instrument, outcome, observed_at)
    else:
        payload, latency_ms = m1.fetch_json(
            f"{m1.KALSHI_URL}/{market_id}/orderbook?depth=100",
            m1.load_config()["request_timeout_seconds"],
        )
        observed_at = utc_now()
        book = m3.normalize_kalshi_book(payload, instrument, outcome, observed_at, state="open")
    bids, asks = m3.validate_book(book)
    if not bids or not asks:
        raise m3.EvidenceError("point order book is not two-sided")
    raw = m3.seal(
        {
            "venue": venue,
            "market_id": instrument["market_id"],
            "observed_at": observed_at.isoformat(),
            "payload": payload,
        }
    )
    return book, float(latency_ms), raw


def m3_effective_latency_ms(connection, venue, current_latency_ms, config):
    values = [
        float(row["latency_ms"])
        for row in connection.execute(
            "SELECT latency_ms FROM m3_latency_samples WHERE venue = ?", (venue,)
        )
    ] + [float(current_latency_ms)]
    label = str(config["latency_percentile"])
    if not label.startswith("p"):
        raise ValueError("M3 latency percentile is invalid")
    percentile = int(label[1:]) / 100
    if not 0 < percentile <= 1:
        raise ValueError("M3 latency percentile is invalid")
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return math.ceil(sorted(values)[index]) + int(config["processing_buffer_ms"])


def m3_execution_config(config):
    import m3

    result = json.loads(json.dumps(config))
    multiplier = m3.decimal(result["fee_stress_multiplier"])
    for rule in result["venue_rules"].values():
        rule["taker_theta"] = m3.decimal_text(
            m3.decimal(rule["taker_theta"]) * multiplier
        )
        rule["maker_theta"] = m3.decimal_text(
            max(m3.ZERO, m3.decimal(rule["maker_theta"])) * multiplier
        )
    result["fee_stress_multiplier"] = "1"
    return result


def next_m3_venue(connection, config):
    count = connection.execute("SELECT COUNT(*) FROM m3_shadow_probes").fetchone()[0]
    return config["venues"][count % len(config["venues"])]


def record_m3_probe_failure(connection, cycle_id, venue, message):
    now = utc_now().isoformat()
    code = "m3_reconciliation_failure" if "reconciliation" in message.lower() else "m3_probe_failure"
    with connection:
        connection.execute(
            """
            INSERT INTO m3_shadow_probes(cycle_id, venue, status, error, created_at)
            VALUES (?, ?, 'failed', ?, ?)
            ON CONFLICT(cycle_id, venue) DO UPDATE SET
                status = 'failed', error = excluded.error
            """,
            (cycle_id, venue, message, now),
        )
        connection.execute(
            """
            INSERT INTO alerts(cycle_id, created_at, severity, code, message)
            VALUES (?, ?, 'high', ?, ?)
            """,
            (cycle_id, now, code, message),
        )
        connection.execute(
            """
            INSERT INTO heartbeats(component, observed_at, status, detail)
            VALUES ('m3_shadow', ?, 'failed', ?)
            ON CONFLICT(component) DO UPDATE SET
                observed_at = excluded.observed_at,
                status = excluded.status,
                detail = excluded.detail
            """,
            (now, message),
        )
        if code == "m3_reconciliation_failure":
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('m3_runtime_frozen_at', ?)",
                (now,),
            )


def run_m3_shadow_probe(connection, cycle_id, market_sink, venue=None):
    import m3

    config, policy = init_m3_runtime(connection)
    venue = venue or next_m3_venue(connection, config)
    if venue not in config["venues"]:
        raise m3.EvidenceError("M3 probe venue is not configured")
    if not config["runtime_probe"].get("enabled"):
        with connection:
            connection.execute(
                "INSERT INTO m3_shadow_probes(cycle_id, venue, status, created_at) VALUES (?, ?, 'skipped', ?)",
                (cycle_id, venue, utc_now().isoformat()),
            )
        return {"status": "skipped", "venue": venue}
    runtime_frozen = connection.execute(
        "SELECT value FROM meta WHERE key = 'm3_runtime_frozen_at'"
    ).fetchone()
    if runtime_frozen:
        with connection:
            connection.execute(
                "INSERT INTO m3_shadow_probes(cycle_id, venue, status, error, created_at) VALUES (?, ?, 'skipped', 'M3 reconciliation freeze', ?)",
                (cycle_id, venue, utc_now().isoformat()),
            )
        return {"status": "skipped", "venue": venue, "reason": "M3 reconciliation freeze"}
    if m3.account_row(connection)["frozen"]:
        with connection:
            connection.execute(
                "INSERT INTO m3_shadow_probes(cycle_id, venue, status, error, created_at) VALUES (?, ?, 'skipped', 'paper account frozen', ?)",
                (cycle_id, venue, utc_now().isoformat()),
            )
        return {"status": "skipped", "venue": venue, "reason": "paper account frozen"}
    markets = market_sink.get(venue)
    if markets is None:
        raise m3.EvidenceError(f"{venue} has no current official market data")
    markets, settlement, waiting = prepare_m3_position(
        connection, venue, markets, config, policy
    )
    if waiting:
        now = utc_now().isoformat()
        with connection:
            connection.execute(
                "INSERT INTO m3_shadow_probes(cycle_id, venue, status, error, created_at) "
                "VALUES (?, ?, 'skipped', ?, ?)",
                (cycle_id, venue, waiting, now),
            )
            connection.execute(
                """
                INSERT INTO heartbeats(component, observed_at, status, detail)
                VALUES ('m3_shadow', ?, 'ok', ?)
                ON CONFLICT(component) DO UPDATE SET
                    observed_at = excluded.observed_at,
                    status = excluded.status,
                    detail = excluded.detail
                """,
                (now, waiting),
            )
        return {"status": "skipped", "venue": venue, "reason": waiting}
    instrument, closing = select_m3_instrument(connection, venue, markets, utc_now(), config)
    decision_book, decision_latency, decision_raw = fetch_m3_book(instrument, config)
    bids, asks = m3.validate_book(decision_book)
    effective_latency = m3_effective_latency_ms(connection, venue, decision_latency, config)
    execution_config = m3_execution_config(config)
    tick = m3.decimal(instrument["tick_size"])
    slippage = tick * m3.decimal(config["default_slippage_ticks"])
    action = "sell" if closing else "buy"
    limit_price = (
        max(tick, bids[0][0] - slippage)
        if closing
        else min(m3.ONE - tick, asks[0][0] + slippage)
    )
    decision_at = decision_book["observed_at"]
    order = {
        "order_id": f"m3-probe-{cycle_id}-{venue}",
        "venue": venue,
        "market_id": instrument["market_id"],
        "event_id": instrument["event_id"],
        "theme_id": instrument["theme_id"],
        "outcome": config["runtime_probe"]["outcome"],
        "action": action,
        "order_type": "marketable_limit",
        "quantity": config["runtime_probe"]["quantity"],
        "limit_price": m3.decimal_text(limit_price),
        "tick_size": instrument["tick_size"],
        "fee_rule_id": instrument["fee_rule_id"],
        "decision_at": decision_at,
        "latency_ms": effective_latency,
    }
    with connection:
        connection.execute(
            """
            INSERT INTO m3_shadow_probes(
                cycle_id, venue, market_id, status, order_id, decision_at,
                request_latency_ms, effective_latency_ms, instrument_json,
                decision_raw_json, decision_book_json, execution_config_json, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                venue,
                instrument["market_id"],
                order["order_id"],
                decision_at,
                decision_latency,
                effective_latency,
                m3.canonical_json(instrument),
                m3.canonical_json(decision_raw),
                m3.canonical_json(decision_book),
                m3.canonical_json(execution_config),
                utc_now().isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO m3_latency_samples(cycle_id, venue, phase, observed_at, latency_ms)
            VALUES (?, ?, 'decision', ?, ?)
            """,
            (cycle_id, venue, decision_book["observed_at"], decision_latency),
        )
    if not closing:
        top_notional = min(bids[0][0] * bids[0][1], asks[0][0] * asks[0][1])
        if top_notional < m3.decimal(config["runtime_probe"]["minimum_top_quote_notional"]):
            raise m3.EvidenceError("point order book is below the approved depth floor")
    time.sleep(effective_latency / 1000)
    execution_book, execution_latency, execution_raw = fetch_m3_book(instrument, config)
    result = m3.simulate_immediate(order, [execution_book], execution_config)
    recorded = m3.record_result(connection, order, result, execution_config, policy)
    now = utc_now().isoformat()
    with connection:
        finalized = connection.execute(
            """
            UPDATE m3_shadow_probes
            SET status = 'recorded', execution_raw_json = ?, execution_book_json = ?,
                result_json = ?, error = NULL
            WHERE cycle_id = ? AND venue = ? AND status = 'pending'
            """,
            (
                m3.canonical_json(execution_raw),
                m3.canonical_json(execution_book),
                m3.canonical_json(result),
                cycle_id,
                venue,
            ),
        )
        if finalized.rowcount != 1:
            raise RuntimeError("M3 pending probe disappeared before finalization")
        connection.execute(
            """
            INSERT INTO m3_latency_samples(cycle_id, venue, phase, observed_at, latency_ms)
            VALUES (?, ?, 'execution', ?, ?)
            """,
            (cycle_id, venue, execution_book["observed_at"], execution_latency),
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('m3_evidence_started_at', ?)",
            (decision_at,),
        )
        connection.execute(
            """
            INSERT INTO heartbeats(component, observed_at, status, detail)
            VALUES ('m3_shadow', ?, 'ok', ?)
            ON CONFLICT(component) DO UPDATE SET
                observed_at = excluded.observed_at,
                status = excluded.status,
                detail = excluded.detail
            """,
            (now, f"{venue} {recorded['execution_status']}"),
        )
    return {
        "status": "recorded",
        "venue": venue,
        "order_id": recorded["order_id"],
        "execution_status": recorded["execution_status"],
        "reconciliation_status": recorded["status"],
        "cash": recorded["cash"],
        "equity": recorded["equity"],
        "settlement": settlement,
    }


def m3_shadow_status(connection, now=None):
    now = now or utc_now()
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'm3_shadow_probes'"
    ).fetchone():
        return {"status": "not_started", "eligible_for_m3_promotion": False}
    config = load_json(M3_CONFIG_PATH)
    started = connection.execute(
        "SELECT value FROM meta WHERE key = 'm3_evidence_started_at'"
    ).fetchone()
    elapsed = 0.0 if not started else max(
        0.0, (now - datetime.fromisoformat(started["value"])).total_seconds() / 3600
    )
    segment = connection.execute(
        "SELECT value FROM meta WHERE key = 'm3_segment_number'"
    ).fetchone()
    start_probe = connection.execute(
        "SELECT value FROM meta WHERE key = 'm3_segment_start_probe_id'"
    ).fetchone()
    activated = connection.execute(
        "SELECT value FROM meta WHERE key = 'm3_segment_activated_at'"
    ).fetchone()
    segment_number = int(segment["value"]) if segment else 1
    start_probe_id = int(start_probe["value"]) if start_probe else 1
    intents = connection.execute(
        "SELECT COUNT(*) FROM m3_shadow_probes AS probe "
        "JOIN paper_orders AS paper ON paper.order_id = probe.order_id "
        "WHERE probe.probe_id >= ? AND probe.status = 'recorded'",
        (start_probe_id,),
    ).fetchone()[0]
    failed = connection.execute(
        "SELECT COUNT(*) FROM m3_shadow_probes WHERE probe_id >= ? AND status = 'failed'",
        (start_probe_id,),
    ).fetchone()[0]
    venue_status = {}
    for venue in config["venues"]:
        counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN probe.status = 'recorded' AND paper.order_id IS NOT NULL
                         THEN 1 ELSE 0 END) AS intents,
                SUM(CASE WHEN probe.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN probe.status = 'skipped' THEN 1 ELSE 0 END) AS skipped
            FROM m3_shadow_probes AS probe
            LEFT JOIN paper_orders AS paper ON paper.order_id = probe.order_id
            WHERE probe.probe_id >= ? AND probe.venue = ?
            """,
            (start_probe_id, venue),
        ).fetchone()
        latest = connection.execute(
            """
            SELECT probe_id, cycle_id, status, order_id, error, created_at
            FROM m3_shadow_probes
            WHERE probe_id >= ? AND venue = ?
            ORDER BY probe_id DESC LIMIT 1
            """,
            (start_probe_id, venue),
        ).fetchone()
        venue_status[venue] = {
            "order_intent_count": counts["intents"] or 0,
            "failed_probe_count": counts["failed"] or 0,
            "skipped_probe_count": counts["skipped"] or 0,
            "latest_probe": dict(latest) if latest else None,
        }
    lifetime_intents = connection.execute(
        "SELECT COUNT(*) FROM m3_shadow_probes AS probe "
        "JOIN paper_orders AS paper ON paper.order_id = probe.order_id "
        "WHERE probe.status = 'recorded'"
    ).fetchone()[0]
    lifetime_failed = connection.execute(
        "SELECT COUNT(*) FROM m3_shadow_probes WHERE status = 'failed'"
    ).fetchone()[0]
    pending = connection.execute(
        "SELECT COUNT(*) FROM m3_shadow_probes WHERE status = 'pending'"
    ).fetchone()[0]
    reconciliation_errors = connection.execute(
        "SELECT COUNT(*) FROM alerts WHERE code = 'm3_reconciliation_failure' "
        "AND (? IS NULL OR created_at >= ?)",
        (activated["value"] if activated else None, activated["value"] if activated else None),
    ).fetchone()[0]
    runtime_frozen = connection.execute(
        "SELECT value FROM meta WHERE key = 'm3_runtime_frozen_at'"
    ).fetchone()
    account = connection.execute(
        "SELECT frozen FROM paper_accounts WHERE account_id = 'paper-v1'"
    ).fetchone()
    promotion = config["promotion"]
    minimum_per_venue = promotion["minimum_order_intents_per_venue"]
    venue_gate_passed = all(
        item["order_intent_count"] >= minimum_per_venue
        for item in venue_status.values()
    )
    eligible = (
        started is not None
        and elapsed >= promotion["duration_hours"]
        and intents >= promotion["minimum_order_intents"]
        and venue_gate_passed
        and reconciliation_errors <= promotion["maximum_reconciliation_errors"]
        and pending == 0
        and runtime_frozen is None
        and account is not None
        and not account["frozen"]
    )
    return {
        "status": "blocked" if runtime_frozen or (account and account["frozen"]) else "collecting" if started else "not_started",
        "segment_number": segment_number,
        "segment_activated_at": activated["value"] if activated else None,
        "segment_start_probe_id": start_probe_id,
        "evidence_started_at": started["value"] if started else None,
        "elapsed_hours": round(elapsed, 3),
        "configuration_version": config["version"],
        "probes_per_cycle": len(config["venues"]),
        "order_intent_count": intents,
        "failed_probe_count": failed,
        "lifetime_order_intent_count": lifetime_intents,
        "lifetime_failed_probe_count": lifetime_failed,
        "pending_probe_count": pending,
        "reconciliation_error_count": reconciliation_errors,
        "runtime_frozen_at": runtime_frozen["value"] if runtime_frozen else None,
        "paper_account_frozen": bool(account and account["frozen"]),
        "minimum_order_intents_per_venue": minimum_per_venue,
        "venue_gate_passed": venue_gate_passed,
        "venues": venue_status,
        "eligible_for_m3_promotion": eligible,
    }


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
    last_cycle = None
    if checks["database"]:
        last = connection.execute(
            "SELECT cycle_id, snapshot_at, started_at, finished_at, status "
            "FROM cycles ORDER BY cycle_id DESC LIMIT 1"
        ).fetchone()
        if last:
            last_cycle = dict(last)
            last_cycle["duration_seconds"] = round(
                (
                    datetime.fromisoformat(last["finished_at"])
                    - datetime.fromisoformat(last["started_at"])
                ).total_seconds(),
                3,
            )
    return {
        "status": "ok" if healthy else "unhealthy",
        "service": {
            "label": SERVICE_LABEL,
            "interval_seconds": config["interval_seconds"],
            "expected_cycles_per_day": 86400 // config["interval_seconds"],
            "database": str(DB_PATH),
            "stdout_log": str(STDOUT_LOG),
            "stderr_log": str(STDERR_LOG),
        },
        "checks": checks,
        "collector_heartbeat": dict(heartbeat) if heartbeat else None,
        "last_cycle": last_cycle,
        "heartbeat_age_seconds": age_seconds,
        "free_disk_mb": round(free_disk_mb, 1),
        "cycle_count": cycle_count,
        "total_cycle_count": total_cycle_count,
        "elapsed_hours": round(elapsed_hours, 3),
        "evidence_started_at": evidence_started_at,
        "eligible_for_m2_promotion": promotion_ready,
    }


def run_cycle(start_evidence=False):
    started_at = utc_now()
    with writer_lock():
        with connect() as connection:
            init_database(connection, started_at)
            if start_evidence:
                mark_evidence_start(connection, started_at)
            try:
                market_sink = {}
                snapshot = m1.collect_snapshot(raw_dir=RAW_DIR, market_sink=market_sink)
                cycle_id, status = record_snapshot(connection, snapshot, started_at)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                cycle_id = record_failed_cycle(connection, started_at, message)
                print(json.dumps({"cycle_id": cycle_id, "status": "failed", "error": message}))
                return 1
            m3_result = {"status": "not_requested"}
            if start_evidence:
                config = load_json(M3_CONFIG_PATH)
                m3_result = {}
                for venue in config["venues"]:
                    try:
                        m3_result[venue] = run_m3_shadow_probe(
                            connection, cycle_id, market_sink, venue
                        )
                    except Exception as exc:
                        message = f"{type(exc).__name__}: {exc}"
                        try:
                            connection.executescript(M3_RUNTIME_SCHEMA)
                            record_m3_probe_failure(connection, cycle_id, venue, message)
                        except sqlite3.DatabaseError:
                            pass
                        m3_result[venue] = {
                            "status": "failed",
                            "venue": venue,
                            "error": message,
                        }
                m3_failed = any(
                    result["status"] == "failed" for result in m3_result.values()
                )
                with connection:
                    connection.execute(
                        """
                        INSERT INTO heartbeats(component, observed_at, status, detail)
                        VALUES ('m3_shadow', ?, ?, ?)
                        ON CONFLICT(component) DO UPDATE SET
                            observed_at = excluded.observed_at,
                            status = excluded.status,
                            detail = excluded.detail
                        """,
                        (
                            utc_now().isoformat(),
                            "failed" if m3_failed else "ok",
                            json.dumps(
                                {venue: result["status"] for venue, result in m3_result.items()},
                                sort_keys=True,
                            ),
                        ),
                    )
    print(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "status": status,
                "snapshot_at": snapshot["collected_at"],
                "m3_shadow": m3_result,
            }
        )
    )
    return 0 if status == "ok" else 1


def initialize():
    with writer_lock():
        with connect() as connection:
            init_database(connection)
    print(json.dumps({"database": str(DB_PATH), "status": "initialized"}))
    return 0


def initialize_m3():
    with writer_lock():
        with connect() as connection:
            init_database(connection)
            config, _ = init_m3_runtime(connection)
    print(json.dumps({"database": str(DB_PATH), "mode": config["mode"], "status": "initialized"}))
    return 0


def initialize_m3_segment(reason):
    with writer_lock():
        with connect() as connection:
            init_database(connection)
            result = start_m3_evidence_segment(connection, reason)
    print(json.dumps(result, indent=2, sort_keys=True))
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
                result["m3_shadow"] = m3_shadow_status(connection)
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
        choices=(
            "init",
            "m3-init",
            "m3-new-segment",
            "cycle",
            "service-cycle",
            "status",
            "check",
            "backup",
            "migrate-m1",
        ),
    )
    parser.add_argument("source", nargs="?")
    arguments = parser.parse_args()
    command = arguments.command
    if command == "init":
        return initialize()
    if command == "m3-init":
        return initialize_m3()
    if command == "m3-new-segment":
        if not arguments.source:
            parser.error("m3-new-segment requires a reason")
        return initialize_m3_segment(arguments.source)
    if command == "cycle":
        return run_cycle()
    if command == "service-cycle":
        return run_cycle(start_evidence=True)
    if command == "backup":
        return backup_database()
    if command == "migrate-m1":
        if not arguments.source:
            parser.error("migrate-m1 requires the old runtime/m1 directory")
        return migrate_m1(arguments.source)
    return show_status(check_only=command == "check")


if __name__ == "__main__":
    raise SystemExit(main())
