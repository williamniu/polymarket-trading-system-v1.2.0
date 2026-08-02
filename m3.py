#!/opt/homebrew/bin/python3.11
"""M3 deterministic paper-execution engine."""

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN
from pathlib import Path

import m2


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "m3.json"
RISK_PATH = ROOT / "config" / "risk-policy.json"
ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
EXPECTED_FEE_RULES = {
    "polymarket_us": {
        "rule_id": "polymarket-us-general-2026-04-03",
        "taker_theta": "0.05",
        "maker_theta": "0",
        "fee_rounding": "half_even_cent",
    },
    "kalshi": {
        "rule_id": "kalshi-general-2026-02-05",
        "taker_theta": "0.07",
        "maker_theta": "0.0175",
        "fee_rounding": "ceiling_cent",
    },
}


class EvidenceError(ValueError):
    pass


class RiskError(ValueError):
    pass


M3_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('yes', 'no')),
    action TEXT NOT NULL CHECK (action IN ('buy', 'sell')),
    order_type TEXT NOT NULL CHECK (order_type IN ('marketable_limit', 'resting_limit')),
    quantity TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    eligible_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,
    filled_quantity TEXT NOT NULL,
    average_price TEXT,
    fees TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    reserved_loss TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    evidence_hash TEXT NOT NULL,
    order_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    fee TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    UNIQUE(order_id, sequence)
);
CREATE TABLE IF NOT EXISTS paper_positions (
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    theme_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('yes', 'no')),
    quantity TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    mark_price TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, venue, market_id, outcome)
);
CREATE TABLE IF NOT EXISTS paper_cash_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('order', 'settlement')),
    amount TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_equity_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    equity TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('initial', 'order', 'mark', 'settlement'))
);
CREATE TABLE IF NOT EXISTS paper_settlements (
    settlement_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('yes', 'no')),
    settlement_price TEXT NOT NULL,
    payout TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    UNIQUE(venue, market_id, outcome)
);
CREATE TABLE IF NOT EXISTS paper_reconciliations (
    reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    expected_cash TEXT NOT NULL,
    actual_cash TEXT NOT NULL,
    expected_equity TEXT NOT NULL,
    actual_equity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed'))
);
"""


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def decimal(value, name="value"):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} is not finite")
    return result


def decimal_text(value):
    return format(decimal(value), "f")


def money(value, rounding=ROUND_HALF_EVEN):
    return decimal(value).quantize(CENT, rounding=rounding)


def parse_time(value, name="timestamp"):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc
    if result.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload):
    content = {key: value for key, value in payload.items() if key != "evidence_hash"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def seal(payload):
    result = dict(payload)
    result["evidence_hash"] = content_hash(result)
    return result


def verify_seal(payload):
    if not isinstance(payload, dict) or payload.get("evidence_hash") != content_hash(payload):
        raise EvidenceError("evidence hash mismatch")


def raw_decimal(value):
    if isinstance(value, dict):
        value = value.get("value")
    return decimal(value)


def normalize_levels(levels, descending):
    totals = {}
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise EvidenceError("book level must contain price and quantity")
        price, quantity = raw_decimal(level[0]), raw_decimal(level[1])
        if not ZERO < price < ONE or quantity <= ZERO:
            raise EvidenceError("book level has invalid price or quantity")
        totals[price] = totals.get(price, ZERO) + quantity
    return [
        [decimal_text(price), decimal_text(totals[price])]
        for price in sorted(totals, reverse=descending)
    ]


def make_book(
    venue,
    market_id,
    event_id,
    theme_id,
    outcome,
    tick_size,
    fee_rule_id,
    observed_at,
    bids,
    asks,
    state="open",
):
    if outcome not in ("yes", "no"):
        raise EvidenceError("unsupported outcome")
    tick_size = decimal(tick_size, "tick_size")
    if not event_id or not theme_id or not fee_rule_id or tick_size <= ZERO:
        raise EvidenceError("instrument metadata is incomplete")
    book = {
        "venue": str(venue),
        "market_id": str(market_id),
        "event_id": str(event_id),
        "theme_id": str(theme_id),
        "outcome": outcome,
        "tick_size": decimal_text(tick_size),
        "fee_rule_id": str(fee_rule_id),
        "observed_at": parse_time(observed_at).isoformat(),
        "state": str(state).lower().removeprefix("market_state_"),
        "bids": normalize_levels(bids, True),
        "asks": normalize_levels(asks, False),
    }
    validate_book(book)
    return seal(book)


def normalize_polymarket_book(payload, instrument, outcome, observed_at):
    data = payload.get("marketData", {})
    yes_bids = [(item.get("px"), item.get("qty")) for item in data.get("bids", [])]
    yes_asks = [(item.get("px"), item.get("qty")) for item in data.get("offers", [])]
    if outcome == "yes":
        bids, asks = yes_bids, yes_asks
    else:
        bids = [(ONE - raw_decimal(price), quantity) for price, quantity in yes_asks]
        asks = [(ONE - raw_decimal(price), quantity) for price, quantity in yes_bids]
    return make_book(
        "polymarket_us",
        instrument["market_id"],
        instrument["event_id"],
        instrument["theme_id"],
        outcome,
        instrument["tick_size"],
        instrument["fee_rule_id"],
        observed_at,
        bids,
        asks,
        data.get("state", "unknown"),
    )


def normalize_kalshi_book(payload, instrument, outcome, observed_at, state="open"):
    data = payload.get("orderbook_fp", payload.get("orderbook", {}))
    yes_bids = data.get("yes_dollars", data.get("yes", []))
    no_bids = data.get("no_dollars", data.get("no", []))
    if outcome == "yes":
        bids = yes_bids
        asks = [(ONE - raw_decimal(price), quantity) for price, quantity in no_bids]
    else:
        bids = no_bids
        asks = [(ONE - raw_decimal(price), quantity) for price, quantity in yes_bids]
    return make_book(
        "kalshi",
        instrument["market_id"],
        instrument["event_id"],
        instrument["theme_id"],
        outcome,
        instrument["tick_size"],
        instrument["fee_rule_id"],
        observed_at,
        bids,
        asks,
        state,
    )


def make_trade(venue, market_id, outcome, observed_at, price, quantity, aggressor_action):
    if outcome not in ("yes", "no") or aggressor_action not in ("buy", "sell"):
        raise EvidenceError("trade side is invalid")
    price, quantity = decimal(price), decimal(quantity)
    if not ZERO < price < ONE or quantity <= ZERO:
        raise EvidenceError("trade price or quantity is invalid")
    return seal(
        {
            "venue": str(venue),
            "market_id": str(market_id),
            "outcome": outcome,
            "observed_at": parse_time(observed_at).isoformat(),
            "price": decimal_text(price),
            "quantity": decimal_text(quantity),
            "aggressor_action": aggressor_action,
        }
    )


def validate_book(book):
    bids = [(decimal(price), decimal(quantity)) for price, quantity in book.get("bids", [])]
    asks = [(decimal(price), decimal(quantity)) for price, quantity in book.get("asks", [])]
    tick = decimal(book.get("tick_size"), "book tick_size")
    if tick <= ZERO:
        raise EvidenceError("book tick size is invalid")
    if any(not ZERO < price < ONE or quantity <= ZERO for price, quantity in bids + asks):
        raise EvidenceError("book contains invalid levels")
    if any(bids[index][0] <= bids[index + 1][0] for index in range(len(bids) - 1)):
        raise EvidenceError("bids are not strictly descending")
    if any(asks[index][0] >= asks[index + 1][0] for index in range(len(asks) - 1)):
        raise EvidenceError("asks are not strictly ascending")
    if bids and asks and bids[0][0] >= asks[0][0]:
        raise EvidenceError("book is locked or crossed")
    if any(price % tick for price, _ in bids + asks):
        raise EvidenceError("book price does not align with instrument tick")
    return bids, asks


def validate_order(order, config):
    required = (
        "order_id",
        "venue",
        "market_id",
        "event_id",
        "theme_id",
        "outcome",
        "action",
        "order_type",
        "quantity",
        "limit_price",
        "tick_size",
        "fee_rule_id",
        "decision_at",
        "latency_ms",
    )
    missing = [key for key in required if order.get(key) in (None, "")]
    if missing:
        raise ValueError(f"order missing: {', '.join(missing)}")
    if order["venue"] not in config["venues"] or order["outcome"] not in ("yes", "no"):
        raise ValueError("order venue or outcome is unsupported")
    if order["fee_rule_id"] != config["venue_rules"][order["venue"]]["rule_id"]:
        raise ValueError("order fee rule is not the configured venue rule")
    if order["action"] not in ("buy", "sell") or order["order_type"] not in config["order_types"]:
        raise ValueError("order action or type is unsupported")
    quantity = decimal(order["quantity"], "quantity")
    price = decimal(order["limit_price"], "limit_price")
    tick = decimal(order["tick_size"], "tick_size")
    increment = decimal(config["quantity_increment"], "quantity_increment")
    if (
        quantity <= ZERO
        or price <= ZERO
        or price >= ONE
        or tick < decimal(config["minimum_tick_size"])
    ):
        raise ValueError("order quantity, price, or tick is invalid")
    if quantity % increment or price % tick:
        raise ValueError("order does not align with quantity or price increment")
    if config["whole_contracts_only"] and quantity != quantity.to_integral_value():
        raise ValueError("fractional contracts are disabled")
    decision_at = parse_time(order["decision_at"], "decision_at")
    try:
        latency_ms = int(order["latency_ms"])
    except (TypeError, ValueError) as exc:
        raise ValueError("latency_ms is invalid") from exc
    if latency_ms < config["processing_buffer_ms"]:
        raise ValueError("latency_ms is below the approved processing buffer")
    eligible_at = decision_at + timedelta(milliseconds=latency_ms)
    expires_at = None
    if order["order_type"] == "resting_limit":
        expires_at = parse_time(order.get("expires_at"), "expires_at")
        if expires_at <= eligible_at:
            raise ValueError("resting order expires before it becomes eligible")
    return quantity, price, eligible_at, expires_at


def select_book(order, books, config):
    _, _, eligible_at, _ = validate_order(order, config)
    previous = None
    selected = None
    for book in books:
        verify_seal(book)
        if (
            book.get("venue"),
            book.get("market_id"),
            book.get("event_id"),
            book.get("theme_id"),
            book.get("outcome"),
            book.get("tick_size"),
            book.get("fee_rule_id"),
        ) != (
            order["venue"],
            order["market_id"],
            order["event_id"],
            order["theme_id"],
            order["outcome"],
            decimal_text(order["tick_size"]),
            order["fee_rule_id"],
        ):
            raise EvidenceError("book instrument metadata does not match order")
        observed_at = parse_time(book.get("observed_at"), "book observed_at")
        if previous is not None and observed_at <= previous:
            raise EvidenceError("book evidence is reordered or duplicated")
        previous = observed_at
        validate_book(book)
        if selected is None and observed_at >= eligible_at:
            selected = book
    if selected is None:
        raise EvidenceError("no book exists after order latency")
    selected_at = parse_time(selected["observed_at"])
    wait_ms = (selected_at - eligible_at).total_seconds() * 1000
    if wait_ms > config["maximum_post_latency_wait_ms"]:
        raise EvidenceError("first post-latency book is too late")
    if selected["state"] != "open":
        raise EvidenceError("market is not open")
    return selected, eligible_at


def fee_for_fill(venue, role, quantity, price, config, stress=False):
    rules = config["venue_rules"][venue]
    theta = decimal(rules[f"{role}_theta"])
    if theta <= ZERO:
        return ZERO
    raw = theta * decimal(quantity) * decimal(price) * (ONE - decimal(price))
    if stress:
        raw *= decimal(config["fee_stress_multiplier"])
    if rules["fee_rounding"] == "half_even_cent":
        return money(raw, ROUND_HALF_EVEN)
    if rules["fee_rounding"] == "ceiling_cent":
        return raw.quantize(CENT, rounding=ROUND_CEILING)
    raise ValueError("unsupported fee rounding rule")


def execution_result(order, fills, status, eligible_at, evidence_hash, reserved_quantity=ZERO):
    filled = sum((decimal(fill["quantity"]) for fill in fills), ZERO)
    fees = sum((decimal(fill["fee"]) for fill in fills), ZERO)
    notional = sum((decimal(fill["price"]) * decimal(fill["quantity"]) for fill in fills), ZERO)
    average = notional / filled if filled else None
    return seal(
        {
            "order_id": order["order_id"],
            "status": status,
            "eligible_at": eligible_at.isoformat(),
            "filled_quantity": decimal_text(filled),
            "average_price": decimal_text(average) if average is not None else None,
            "fees": decimal_text(fees),
            "reserved_quantity": decimal_text(reserved_quantity),
            "source_evidence_hash": evidence_hash,
            "fills": fills,
        }
    )


def valid_sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def validate_result(order, result, config):
    verify_seal(result)
    quantity, limit_price, eligible_at, expires_at = validate_order(order, config)
    if result.get("order_id") != order["order_id"]:
        raise EvidenceError("result belongs to a different order")
    if parse_time(result.get("eligible_at")) != eligible_at:
        raise EvidenceError("result eligibility does not match order latency")
    fills = result.get("fills")
    if not isinstance(fills, list) or not valid_sha256(result.get("source_evidence_hash")):
        raise EvidenceError("result evidence reference is invalid")
    total_quantity, total_fees, total_notional = ZERO, ZERO, ZERO
    previous = None
    role = "taker" if order["order_type"] == "marketable_limit" else "maker"
    increment = decimal(config["quantity_increment"])
    for fill in fills:
        if not isinstance(fill, dict) or not valid_sha256(fill.get("evidence_hash")):
            raise EvidenceError("fill evidence reference is invalid")
        price = decimal(fill.get("price"), "fill price")
        fill_quantity = decimal(fill.get("quantity"), "fill quantity")
        fee = decimal(fill.get("fee"), "fill fee")
        observed_at = parse_time(fill.get("observed_at"), "fill observed_at")
        if fill_quantity <= ZERO or fill_quantity % increment:
            raise EvidenceError("fill quantity is invalid")
        if not ZERO < price < ONE:
            raise EvidenceError("fill price is invalid")
        if (order["action"] == "buy" and price > limit_price) or (
            order["action"] == "sell" and price < limit_price
        ):
            raise EvidenceError("fill violates the order limit")
        if observed_at < eligible_at or (expires_at and observed_at > expires_at):
            raise EvidenceError("fill is outside the executable time window")
        if previous is not None and observed_at < previous:
            raise EvidenceError("fills are reordered")
        previous = observed_at
        expected_fee = fee_for_fill(order["venue"], role, fill_quantity, price, config)
        if fee != expected_fee:
            raise EvidenceError("fill fee does not match the venue rule")
        total_quantity += fill_quantity
        total_fees += fee
        total_notional += price * fill_quantity
    if total_quantity > quantity:
        raise EvidenceError("fills exceed order quantity")
    expected_average = total_notional / total_quantity if total_quantity else None
    if decimal(result.get("filled_quantity")) != total_quantity:
        raise EvidenceError("result filled quantity does not match fills")
    if decimal(result.get("fees")) != total_fees:
        raise EvidenceError("result fees do not match fills")
    if (result.get("average_price") is None) != (expected_average is None):
        raise EvidenceError("result average price is inconsistent")
    if expected_average is not None and decimal(result["average_price"]) != expected_average:
        raise EvidenceError("result average price does not match fills")
    remaining = quantity - total_quantity
    if order["order_type"] == "marketable_limit":
        expected_status = "filled" if not remaining else "partial" if fills else "unfilled"
        expected_reserved = ZERO
    else:
        status = result.get("status")
        if not remaining:
            expected_status = "filled"
        elif fills and status in ("partial_expired", "partial_resting"):
            expected_status = status
        elif not fills and status in ("expired", "unverified"):
            expected_status = status
        else:
            raise EvidenceError("resting result status is inconsistent")
        expected_reserved = remaining if expected_status in ("partial_resting", "unverified") else ZERO
    if result.get("status") != expected_status:
        raise EvidenceError("result status is inconsistent")
    if decimal(result.get("reserved_quantity")) != expected_reserved:
        raise EvidenceError("result reservation is inconsistent")
    if fills and result["source_evidence_hash"] != fills[-1]["evidence_hash"]:
        raise EvidenceError("result source does not match the last fill")
    return total_quantity, total_fees


def simulate_immediate(order, books, config):
    quantity, limit_price, eligible_at, _ = validate_order(order, config)
    if order["order_type"] != "marketable_limit":
        raise ValueError("immediate simulation requires a marketable_limit order")
    book, _ = select_book(order, books, config)
    bids, asks = validate_book(book)
    levels = asks if order["action"] == "buy" else bids
    fraction = decimal(config["depth_credit_fraction"])
    increment = decimal(config["quantity_increment"])
    remaining, fills = quantity, []
    for price, visible_quantity in levels:
        if (order["action"] == "buy" and price > limit_price) or (
            order["action"] == "sell" and price < limit_price
        ):
            break
        credited = (visible_quantity * fraction / increment).to_integral_value(
            rounding=ROUND_FLOOR
        ) * increment
        fill_quantity = min(remaining, credited)
        if fill_quantity <= ZERO:
            continue
        fills.append(
            {
                "price": decimal_text(price),
                "quantity": decimal_text(fill_quantity),
                "fee": decimal_text(
                    fee_for_fill(order["venue"], "taker", fill_quantity, price, config)
                ),
                "observed_at": book["observed_at"],
                "evidence_hash": book["evidence_hash"],
            }
        )
        remaining -= fill_quantity
        if remaining <= ZERO:
            break
    status = "filled" if remaining == ZERO else "partial" if fills else "unfilled"
    return execution_result(order, fills, status, eligible_at, book["evidence_hash"])


def simulate_resting(order, books, trades, evidence_end_at, config):
    quantity, limit_price, eligible_at, expires_at = validate_order(order, config)
    if order["order_type"] != "resting_limit":
        raise ValueError("resting simulation requires a resting_limit order")
    book, _ = select_book(order, books, config)
    bids, asks = validate_book(book)
    if order["action"] == "buy" and asks and limit_price >= asks[0][0]:
        raise ValueError("resting buy crosses the book")
    if order["action"] == "sell" and bids and limit_price <= bids[0][0]:
        raise ValueError("resting sell crosses the book")
    own_side = bids if order["action"] == "buy" else asks
    queue_ahead = sum((quantity for price, quantity in own_side if price == limit_price), ZERO)
    queue_ahead *= decimal(config["resting_queue_multiplier"])
    end_at = parse_time(evidence_end_at, "evidence_end_at")
    if end_at < eligible_at:
        raise EvidenceError("trade evidence ends before order eligibility")
    previous, cumulative, filled, fills = None, ZERO, ZERO, []
    for trade in trades:
        verify_seal(trade)
        if (trade.get("venue"), trade.get("market_id"), trade.get("outcome")) != (
            order["venue"],
            order["market_id"],
            order["outcome"],
        ):
            raise EvidenceError("trade identity does not match order")
        observed_at = parse_time(trade.get("observed_at"), "trade observed_at")
        if previous is not None and observed_at <= previous:
            raise EvidenceError("trade evidence is reordered or duplicated")
        previous = observed_at
        if observed_at < eligible_at or observed_at > min(end_at, expires_at):
            continue
        price, traded = decimal(trade["price"]), decimal(trade["quantity"])
        eligible_trade = (
            order["action"] == "buy"
            and trade["aggressor_action"] == "sell"
            and price <= limit_price
        ) or (
            order["action"] == "sell"
            and trade["aggressor_action"] == "buy"
            and price >= limit_price
        )
        if not eligible_trade:
            continue
        cumulative += traded
        available_to_us = max(ZERO, cumulative - queue_ahead)
        new_fill = min(quantity, available_to_us) - filled
        if new_fill <= ZERO:
            continue
        fills.append(
            {
                "price": decimal_text(limit_price),
                "quantity": decimal_text(new_fill),
                "fee": decimal_text(
                    fee_for_fill(order["venue"], "maker", new_fill, limit_price, config)
                ),
                "observed_at": trade["observed_at"],
                "evidence_hash": trade["evidence_hash"],
            }
        )
        filled += new_fill
        if filled == quantity:
            break
    expired = end_at >= expires_at
    if filled == quantity:
        status = "filled"
    elif filled:
        status = "partial_expired" if expired else "partial_resting"
    else:
        status = "expired" if expired else "unverified"
    reserved = quantity - filled if status in ("partial_resting", "unverified") else ZERO
    source_hash = fills[-1]["evidence_hash"] if fills else book["evidence_hash"]
    return execution_result(order, fills, status, eligible_at, source_hash, reserved)


def init_database(connection, now=None, risk_path=RISK_PATH):
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    m2.init_database(connection, now=now, policy_path=risk_path)
    with connection:
        connection.executescript(M3_SCHEMA)
        if not connection.execute("SELECT 1 FROM paper_equity_history LIMIT 1").fetchone():
            account = account_row(connection)
            observed_at = (
                now.astimezone(timezone.utc).isoformat()
                if isinstance(now, datetime)
                else datetime.now(timezone.utc).isoformat()
            )
            connection.execute(
                "INSERT INTO paper_equity_history(observed_at, equity, reason) VALUES (?, ?, 'initial')",
                (observed_at, decimal_text(money(account["equity"]))),
            )


def account_row(connection):
    row = connection.execute(
        "SELECT * FROM paper_accounts WHERE account_id = 'paper-v1'"
    ).fetchone()
    if row is None:
        raise RuntimeError("paper account is missing")
    return row


def open_loss(connection, event_id=None, theme_id=None):
    positions = connection.execute("SELECT * FROM paper_positions").fetchall()
    orders = connection.execute(
        "SELECT event_id, theme_id, reserved_loss FROM paper_orders WHERE reserved_loss != '0'"
    ).fetchall()
    total = ZERO
    for row in positions:
        if event_id is not None and row["event_id"] != event_id:
            continue
        if theme_id is not None and row["theme_id"] != theme_id:
            continue
        total += decimal(row["cost_basis"])
    for row in orders:
        if event_id is not None and row["event_id"] != event_id:
            continue
        if theme_id is not None and row["theme_id"] != theme_id:
            continue
        total += decimal(row["reserved_loss"])
    return total


def preflight_risk(connection, order, config, policy, candidate_floor=ZERO):
    quantity, limit_price, _, _ = validate_order(order, config)
    account = account_row(connection)
    if account["frozen"]:
        raise RiskError("paper account is frozen")
    equity = decimal(account["equity"])
    if equity <= ZERO:
        raise RiskError("paper account has no equity")
    if order["action"] == "sell":
        position = connection.execute(
            """
            SELECT quantity, event_id, theme_id FROM paper_positions
            WHERE account_id = 'paper-v1' AND venue = ? AND market_id = ? AND outcome = ?
            """,
            (order["venue"], order["market_id"], order["outcome"]),
        ).fetchone()
        if position is None or decimal(position["quantity"]) < quantity:
            raise RiskError("sell order exceeds the existing position")
        if (position["event_id"], position["theme_id"]) != (order["event_id"], order["theme_id"]):
            raise RiskError("sell order classification conflicts with the position")
        return {"candidate_loss": ZERO}
    candidate = max(decimal(candidate_floor), quantity * limit_price + fee_for_fill(
        order["venue"], "taker", quantity, limit_price, config, stress=True
    ))
    limits = policy["limits_pct"]

    def allowed(percent):
        return equity * decimal(percent) / Decimal("100")

    if candidate > allowed(limits["max_loss_per_trade"]):
        raise RiskError("order exceeds maximum loss per trade")
    if open_loss(connection, event_id=order["event_id"]) + candidate > allowed(
        limits["max_event_risk"]
    ):
        raise RiskError("order exceeds maximum event risk")
    if open_loss(connection, theme_id=order["theme_id"]) + candidate > allowed(
        limits["max_theme_risk"]
    ):
        raise RiskError("order exceeds maximum theme risk")
    if open_loss(connection) + candidate > allowed(limits["max_total_worst_case_loss"]):
        raise RiskError("order exceeds total worst-case loss")
    if candidate > decimal(account["cash"]):
        raise RiskError("order exceeds available cash")
    return {"candidate_loss": candidate}


def position_row(connection, order):
    return connection.execute(
        """
        SELECT * FROM paper_positions
        WHERE account_id = 'paper-v1' AND venue = ? AND market_id = ? AND outcome = ?
        """,
        (order["venue"], order["market_id"], order["outcome"]),
    ).fetchone()


def apply_fill(connection, order, fill):
    quantity, price, fee = (
        decimal(fill["quantity"]),
        decimal(fill["price"]),
        decimal(fill["fee"]),
    )
    position = position_row(connection, order)
    now = fill["observed_at"]
    if order["action"] == "buy":
        old_quantity = decimal(position["quantity"]) if position else ZERO
        old_basis = decimal(position["cost_basis"]) if position else ZERO
        new_quantity = old_quantity + quantity
        new_basis = old_basis + quantity * price + fee
        connection.execute(
            """
            INSERT INTO paper_positions(
                account_id, venue, market_id, event_id, theme_id, outcome,
                quantity, cost_basis, mark_price, updated_at
            ) VALUES ('paper-v1', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, venue, market_id, outcome) DO UPDATE SET
                quantity = excluded.quantity,
                cost_basis = excluded.cost_basis,
                mark_price = excluded.mark_price,
                updated_at = excluded.updated_at
            """,
            (
                order["venue"],
                order["market_id"],
                order["event_id"],
                order["theme_id"],
                order["outcome"],
                decimal_text(new_quantity),
                decimal_text(new_basis),
                decimal_text(price),
                now,
            ),
        )
        return -(quantity * price + fee), ZERO
    if position is None or decimal(position["quantity"]) < quantity:
        raise RiskError("fill exceeds the existing position")
    old_quantity, old_basis = decimal(position["quantity"]), decimal(position["cost_basis"])
    allocated_basis = old_basis * quantity / old_quantity
    new_quantity, new_basis = old_quantity - quantity, old_basis - allocated_basis
    if new_quantity == ZERO:
        connection.execute(
            "DELETE FROM paper_positions WHERE account_id = 'paper-v1' AND venue = ? AND market_id = ? AND outcome = ?",
            (order["venue"], order["market_id"], order["outcome"]),
        )
    else:
        connection.execute(
            """
            UPDATE paper_positions SET quantity = ?, cost_basis = ?, mark_price = ?, updated_at = ?
            WHERE account_id = 'paper-v1' AND venue = ? AND market_id = ? AND outcome = ?
            """,
            (
                decimal_text(new_quantity),
                decimal_text(new_basis),
                decimal_text(price),
                now,
                order["venue"],
                order["market_id"],
                order["outcome"],
            ),
        )
    proceeds = quantity * price - fee
    return proceeds, proceeds - allocated_basis


def loss_percentage(start, current):
    return ZERO if start <= ZERO or current >= start else (start - current) * Decimal("100") / start


def freeze_for_losses(connection, observed_at, policy):
    account = account_row(connection)
    if account["frozen"]:
        return ["already_frozen"]
    current = money(account["equity"])
    observed = parse_time(observed_at)
    day_start = observed.replace(hour=0, minute=0, second=0, microsecond=0)
    rolling_start = observed - timedelta(days=3)

    def basis(target, same_day=False):
        if same_day:
            row = connection.execute(
                """
                SELECT equity FROM paper_equity_history
                WHERE observed_at >= ? AND observed_at <= ?
                ORDER BY observed_at, history_id LIMIT 1
                """,
                (target.isoformat(), observed.isoformat()),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT equity FROM paper_equity_history
                WHERE observed_at <= ? ORDER BY observed_at DESC, history_id DESC LIMIT 1
                """,
                (target.isoformat(),),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT equity FROM paper_equity_history ORDER BY observed_at, history_id LIMIT 1"
                ).fetchone()
        return decimal(row["equity"]) if row else current

    limits = policy["limits_pct"]
    reasons = []
    if loss_percentage(basis(day_start, same_day=True), current) >= decimal(
        limits["daily_hard_stop"]
    ):
        reasons.append("daily_hard_stop")
    if loss_percentage(basis(rolling_start), current) >= decimal(
        limits["rolling_3d_hard_stop"]
    ):
        reasons.append("rolling_3d_hard_stop")
    if loss_percentage(decimal(account["high_watermark"]), current) >= decimal(
        limits["high_watermark_drawdown_freeze"]
    ):
        reasons.append("high_watermark_drawdown_freeze")
    if reasons:
        connection.execute(
            "UPDATE paper_accounts SET frozen = 1 WHERE account_id = 'paper-v1'"
        )
        connection.execute(
            """
            INSERT INTO alerts(created_at, severity, code, message)
            VALUES (?, 'critical', 'm3_risk_freeze', ?)
            """,
            (observed.isoformat(), ", ".join(reasons)),
        )
    return reasons


def refresh_account(connection, observed_at, reason, policy):
    account = account_row(connection)
    cash = money(account["cash"])
    marked = sum(
        (
            decimal(row["quantity"]) * decimal(row["mark_price"])
            for row in connection.execute("SELECT quantity, mark_price FROM paper_positions")
        ),
        ZERO,
    )
    equity = money(cash + marked)
    high_watermark = max(money(account["high_watermark"]), equity)
    connection.execute(
        "UPDATE paper_accounts SET equity = ?, high_watermark = ?, updated_at = ? WHERE account_id = 'paper-v1'",
        (float(equity), float(high_watermark), observed_at),
    )
    connection.execute(
        "INSERT INTO paper_equity_history(observed_at, equity, reason) VALUES (?, ?, ?)",
        (parse_time(observed_at).isoformat(), decimal_text(equity), reason),
    )
    return freeze_for_losses(connection, observed_at, policy)


def reconciliation_values(connection):
    account = account_row(connection)
    ledger = sum(
        (decimal(row["amount"]) for row in connection.execute("SELECT amount FROM paper_cash_ledger")),
        ZERO,
    )
    expected_cash = money(decimal(account["starting_capital"]) + ledger)
    actual_cash = money(account["cash"])
    marked = sum(
        (
            decimal(row["quantity"]) * decimal(row["mark_price"])
            for row in connection.execute("SELECT quantity, mark_price FROM paper_positions")
        ),
        ZERO,
    )
    expected_equity = money(actual_cash + marked)
    actual_equity = money(account["equity"])
    return expected_cash, actual_cash, expected_equity, actual_equity


def reconcile(connection, created_at=None, record=False):
    values = reconciliation_values(connection)
    status = "ok" if values[0] == values[1] and values[2] == values[3] else "failed"
    if record:
        connection.execute(
            """
            INSERT INTO paper_reconciliations(
                created_at, expected_cash, actual_cash, expected_equity, actual_equity, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                created_at or datetime.now(timezone.utc).isoformat(),
                *(decimal_text(value) for value in values),
                status,
            ),
        )
    if status != "ok":
        raise RuntimeError("paper account reconciliation failed")
    return {
        "status": status,
        "cash": decimal_text(values[1]),
        "equity": decimal_text(values[3]),
    }


def record_result(connection, order, result, config, policy):
    quantity, limit_price, eligible_at, expires_at = validate_order(order, config)
    validate_result(order, result, config)
    with connection:
        if connection.execute(
            "SELECT 1 FROM paper_orders WHERE order_id = ?", (order["order_id"],)
        ).fetchone():
            raise sqlite3.IntegrityError("duplicate paper order")
        fills = result["fills"]
        filled = sum((decimal(fill["quantity"]) for fill in fills), ZERO)
        fees = sum((decimal(fill["fee"]) for fill in fills), ZERO)
        remaining = quantity - filled
        reserved = ZERO
        if order["action"] == "buy" and result["status"] in ("unverified", "partial_resting"):
            reserved = remaining * limit_price + fee_for_fill(
                order["venue"], "maker", remaining, limit_price, config, stress=True
            )
        actual_loss = reserved + sum(
            (
                decimal(fill["quantity"]) * decimal(fill["price"])
                + decimal(fill["fee"])
                for fill in fills
            ),
            ZERO,
        )
        preflight_risk(connection, order, config, policy, actual_loss)
        cash_delta, realized = ZERO, ZERO
        for sequence, fill in enumerate(fills, 1):
            delta, pnl = apply_fill(connection, order, fill)
            cash_delta += delta
            realized += pnl
        average = result["average_price"]
        connection.execute(
            """
            INSERT INTO paper_orders(
                order_id, account_id, venue, market_id, event_id, theme_id, outcome,
                action, order_type, quantity, limit_price, decision_at, eligible_at,
                expires_at, status, filled_quantity, average_price, fees, realized_pnl,
                reserved_loss, config_version, evidence_hash, order_json, result_json
            ) VALUES (?, 'paper-v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["order_id"],
                order["venue"],
                order["market_id"],
                order["event_id"],
                order["theme_id"],
                order["outcome"],
                order["action"],
                order["order_type"],
                decimal_text(quantity),
                decimal_text(limit_price),
                parse_time(order["decision_at"]).isoformat(),
                eligible_at.isoformat(),
                expires_at.isoformat() if expires_at else None,
                result["status"],
                decimal_text(filled),
                average,
                decimal_text(fees),
                decimal_text(realized),
                decimal_text(reserved),
                config["version"],
                result["evidence_hash"],
                canonical_json(order),
                canonical_json(result),
            ),
        )
        for sequence, fill in enumerate(fills, 1):
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, sequence, price, quantity, fee, observed_at, evidence_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["order_id"],
                    sequence,
                    fill["price"],
                    fill["quantity"],
                    fill["fee"],
                    fill["observed_at"],
                    fill["evidence_hash"],
                ),
            )
        if cash_delta:
            account = account_row(connection)
            new_cash = money(decimal(account["cash"]) + cash_delta)
            if new_cash < ZERO:
                raise RiskError("execution creates negative cash")
            connection.execute(
                "UPDATE paper_accounts SET cash = ? WHERE account_id = 'paper-v1'",
                (float(new_cash),),
            )
            connection.execute(
                "INSERT INTO paper_cash_ledger(reference_id, kind, amount, created_at) VALUES (?, 'order', ?, ?)",
                (order["order_id"], decimal_text(money(cash_delta)), result["eligible_at"]),
            )
        observed_at = fills[-1]["observed_at"] if fills else result["eligible_at"]
        refresh_account(connection, observed_at, "order", policy)
        reconciliation = reconcile(connection, observed_at, record=True)
    return {
        "order_id": order["order_id"],
        "execution_status": result["status"],
        **reconciliation,
    }


def make_settlement(settlement_id, venue, market_id, outcome, price, observed_at, final=True):
    price = decimal(price, "settlement price")
    if not ZERO <= price <= ONE:
        raise EvidenceError("settlement price is outside zero and one")
    return seal(
        {
            "settlement_id": str(settlement_id),
            "venue": str(venue),
            "market_id": str(market_id),
            "outcome": outcome,
            "price": decimal_text(price),
            "observed_at": parse_time(observed_at).isoformat(),
            "final": bool(final),
        }
    )


def record_settlement(connection, settlement, config, policy=None):
    verify_seal(settlement)
    if (
        settlement.get("venue") not in config["venues"]
        or settlement.get("outcome") not in ("yes", "no")
        or not settlement.get("market_id")
        or not settlement.get("settlement_id")
    ):
        raise EvidenceError("settlement identity is invalid")
    if not settlement.get("final"):
        raise EvidenceError("settlement is not final")
    price = decimal(settlement["price"])
    if not config["allow_scalar_settlement"] and price not in (ZERO, ONE):
        raise EvidenceError("scalar settlement is disabled")
    with connection:
        open_order = connection.execute(
            """
            SELECT 1 FROM paper_orders
            WHERE venue = ? AND market_id = ? AND reserved_loss != '0'
            """,
            (settlement["venue"], settlement["market_id"]),
        ).fetchone()
        if open_order:
            raise RiskError("market has an open paper order at settlement")
        position = connection.execute(
            """
            SELECT * FROM paper_positions
            WHERE account_id = 'paper-v1' AND venue = ? AND market_id = ? AND outcome = ?
            """,
            (settlement["venue"], settlement["market_id"], settlement["outcome"]),
        ).fetchone()
        payout = money(decimal(position["quantity"]) * price, ROUND_DOWN) if position else ZERO
        connection.execute(
            """
            INSERT INTO paper_settlements(
                settlement_id, venue, market_id, outcome, settlement_price,
                payout, observed_at, evidence_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settlement["settlement_id"],
                settlement["venue"],
                settlement["market_id"],
                settlement["outcome"],
                decimal_text(price),
                decimal_text(payout),
                settlement["observed_at"],
                settlement["evidence_hash"],
            ),
        )
        if position:
            connection.execute(
                "DELETE FROM paper_positions WHERE account_id = 'paper-v1' AND venue = ? AND market_id = ? AND outcome = ?",
                (settlement["venue"], settlement["market_id"], settlement["outcome"]),
            )
        if payout:
            account = account_row(connection)
            new_cash = money(decimal(account["cash"]) + payout)
            connection.execute(
                "UPDATE paper_accounts SET cash = ? WHERE account_id = 'paper-v1'",
                (float(new_cash),),
            )
            connection.execute(
                "INSERT INTO paper_cash_ledger(reference_id, kind, amount, created_at) VALUES (?, 'settlement', ?, ?)",
                (settlement["settlement_id"], decimal_text(payout), settlement["observed_at"]),
            )
        refresh_account(
            connection,
            settlement["observed_at"],
            "settlement",
            policy or load_json(RISK_PATH),
        )
        reconciliation = reconcile(connection, settlement["observed_at"], record=True)
    return {"settlement_id": settlement["settlement_id"], "payout": decimal_text(payout), **reconciliation}


def mark_to_market(connection, books, observed_at, config, policy):
    observed = parse_time(observed_at)
    evidence = {}
    for book in books:
        verify_seal(book)
        validate_book(book)
        book_time = parse_time(book["observed_at"])
        if book_time > observed or (observed - book_time).total_seconds() * 1000 > config[
            "maximum_post_latency_wait_ms"
        ]:
            raise EvidenceError("mark book is future-dated or stale")
        if book["state"] != "open":
            raise EvidenceError("cannot mark a position from a closed market")
        if (
            book["venue"] not in config["venues"]
            or book["fee_rule_id"] != config["venue_rules"][book["venue"]]["rule_id"]
            or decimal(book["tick_size"]) < decimal(config["minimum_tick_size"])
        ):
            raise EvidenceError("mark book uses unsupported instrument rules")
        key = (book["venue"], book["market_id"], book["outcome"])
        if key in evidence:
            raise EvidenceError("duplicate mark book")
        evidence[key] = book
    with connection:
        positions = connection.execute("SELECT * FROM paper_positions").fetchall()
        for position in positions:
            key = (position["venue"], position["market_id"], position["outcome"])
            book = evidence.get(key)
            if book is None:
                raise EvidenceError("position has no mark book")
            if (position["event_id"], position["theme_id"]) != (
                book["event_id"],
                book["theme_id"],
            ):
                raise EvidenceError("mark book classification conflicts with position")
            remaining, liquidation = decimal(position["quantity"]), ZERO
            increment = decimal(config["quantity_increment"])
            for price, visible in validate_book(book)[0]:
                credited = (
                    decimal(visible)
                    * decimal(config["depth_credit_fraction"])
                    / increment
                ).to_integral_value(rounding=ROUND_FLOOR) * increment
                quantity = min(remaining, credited)
                if quantity <= ZERO:
                    continue
                liquidation += quantity * price - fee_for_fill(
                    position["venue"], "taker", quantity, price, config
                )
                remaining -= quantity
                if remaining == ZERO:
                    break
            effective_mark = max(ZERO, liquidation) / decimal(position["quantity"])
            connection.execute(
                """
                UPDATE paper_positions SET mark_price = ?, updated_at = ?
                WHERE account_id = 'paper-v1' AND venue = ? AND market_id = ? AND outcome = ?
                """,
                (
                    decimal_text(effective_mark),
                    observed.isoformat(),
                    position["venue"],
                    position["market_id"],
                    position["outcome"],
                ),
            )
        reasons = refresh_account(connection, observed.isoformat(), "mark", policy)
        reconciliation = reconcile(connection, observed.isoformat(), record=True)
    return {"frozen": bool(account_row(connection)["frozen"]), "reasons": reasons, **reconciliation}


def check_configuration(config=None, policy=None):
    config = config or load_json(CONFIG_PATH)
    policy = policy or load_json(RISK_PATH)
    if config.get("mode") not in ("offline_shadow", "runtime_shadow") or policy.get("mode") != "paper":
        raise RuntimeError("M3 configuration is not paper shadow mode")
    if not config.get("ignore_rebates") or config.get("products") != ["binary"]:
        raise RuntimeError("M3 scope or rebate boundary changed")
    if not ZERO < decimal(config["depth_credit_fraction"]) <= ONE:
        raise RuntimeError("M3 depth credit is outside zero and one")
    if decimal(config["resting_queue_multiplier"]) < ONE:
        raise RuntimeError("M3 queue multiplier understates observed queue")
    if decimal(config["fee_stress_multiplier"]) < ONE:
        raise RuntimeError("M3 fee stress is below published fees")
    if decimal(config["minimum_tick_size"]) < CENT:
        raise RuntimeError("M3 sub-cent execution is not implemented")
    probe = config.get("runtime_probe", {})
    quantity = decimal(probe.get("quantity"), "runtime probe quantity")
    if (
        not isinstance(probe.get("enabled"), bool)
        or probe.get("outcome") not in ("yes", "no")
        or quantity != ONE
        or decimal(probe.get("minimum_top_quote_notional")) <= ZERO
        or int(probe.get("minimum_time_to_close_minutes", 0)) <= 0
    ):
        raise RuntimeError("M3 runtime probe boundary is invalid")
    for venue, expected in EXPECTED_FEE_RULES.items():
        actual = config["venue_rules"].get(venue, {})
        if any(str(actual.get(key)) != value for key, value in expected.items()):
            raise RuntimeError(f"M3 {venue} fee rule changed without review")
    connection = sqlite3.connect(":memory:")
    try:
        init_database(connection)
        return reconcile(connection)
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.parse_args()
    result = check_configuration()
    print(json.dumps({"mode": load_json(CONFIG_PATH)["mode"], **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
