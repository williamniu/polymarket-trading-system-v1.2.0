#!/opt/homebrew/bin/python3.11
import argparse
import concurrent.futures
import hashlib
import json
import re
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import m1


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "m4.json"
RUNTIME = ROOT / "runtime" / "m2" / "research" / "m4"
LATEST_REPORT_PATH = RUNTIME / "candidate-audit-latest.json"
LATEST_PEER_REPORT_PATH = RUNTIME / "peer-discovery-latest.json"
DATA_API = "https://data-api.polymarket.com"
PROFILE_API = "https://gamma-api.polymarket.com/public-profile"
WALLET_PATTERN = re.compile(r"^0x[a-f0-9]{40}$")
CONDITION_PATTERN = re.compile(r"^0x[a-f0-9]{64}$")


def utc_now():
    return datetime.now(timezone.utc)


def load_config(path=CONFIG_PATH):
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload):
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rounded(value):
    return round(value, 6) if value is not None else None


def fraction(part, whole):
    return rounded(part / whole) if whole else None


def validate_config(config):
    if config.get("mode") != "candidate_audit" or config.get("paper_only") is not True:
        raise ValueError("M4.1-A must remain paper-only candidate_audit")
    if config.get("source") != "polymarket_public_data":
        raise ValueError("M4.1-A source must be public Polymarket data")
    if set(config.get("categories", [])) != {"POLITICS", "ECONOMICS", "FINANCE"}:
        raise ValueError("M4.1-A categories must be politics, economics and finance")
    if set(config.get("periods", [])) != {"WEEK", "MONTH", "ALL"}:
        raise ValueError("M4.1-A periods must be week, month and all")
    integer_ranges = {
        "leaderboard_limit": (1, 50),
        "minimum_leaderboard_appearances": (1, 9),
        "maximum_candidates_to_audit": (1, 50),
        "observation_pool_limit": (1, 50),
        "recent_trade_limit": (1, 10000),
        "closed_position_limit": (1, 50),
        "recent_trade_lookback_days": (1, 3650),
        "aggregation_window_minutes": (1, 1440),
        "request_timeout_seconds": (1, 120),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        value = config.get(key)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}")
    if number(config.get("minimum_material_notional_usd")) is None or number(
        config["minimum_material_notional_usd"]
    ) < 0:
        raise ValueError("minimum_material_notional_usd must be non-negative")
    if number(config.get("maximum_material_actions_per_day")) is None or number(
        config["maximum_material_actions_per_day"]
    ) <= 0:
        raise ValueError("maximum_material_actions_per_day must be positive")
    low, high = number(config.get("extreme_price_low")), number(
        config.get("extreme_price_high")
    )
    if low is None or high is None or not 0 <= low < high <= 1:
        raise ValueError("extreme price thresholds must satisfy 0 <= low < high <= 1")
    for key, value in config.get("gates", {}).items():
        if key == "minimum_closed_positions":
            if not isinstance(value, int) or not 0 <= value <= config["closed_position_limit"]:
                raise ValueError("minimum_closed_positions is invalid")
        elif number(value) is None or not 0 <= number(value) <= 1:
            raise ValueError(f"{key} must be a fraction from 0 to 1")
    required_gates = {
        "minimum_closed_positions",
        "minimum_target_trade_notional_fraction",
        "maximum_excluded_trade_notional_fraction",
        "maximum_extreme_price_fraction",
        "maximum_single_closed_event_abs_pnl_fraction",
    }
    if set(config.get("gates", {})) != required_gates:
        raise ValueError("M4.1-A gates are incomplete or unknown")
    if config["observation_pool_limit"] > config["maximum_candidates_to_audit"]:
        raise ValueError("observation_pool_limit cannot exceed audited candidates")
    peer = config.get("peer_discovery")
    required_peer_keys = {
        "reference_wallets",
        "lookback_days",
        "minimum_shared_target_markets",
        "consensus_window_hours",
        "reference_trade_limit",
        "market_trade_limit",
        "maximum_candidates_to_review",
        "required_independent_experts",
    }
    if not isinstance(peer, dict) or set(peer) != required_peer_keys:
        raise ValueError("M4.1-B peer discovery configuration is incomplete or unknown")
    wallets = peer["reference_wallets"]
    if (
        not isinstance(wallets, list)
        or not wallets
        or len(wallets) > 10
        or any(not isinstance(wallet, str) or not WALLET_PATTERN.fullmatch(wallet) for wallet in wallets)
        or len(set(wallets)) != len(wallets)
    ):
        raise ValueError("peer discovery reference wallets are invalid")
    peer_integer_ranges = {
        "lookback_days": (1, 3650),
        "minimum_shared_target_markets": (1, 1000),
        "consensus_window_hours": (1, 168),
        "reference_trade_limit": (1, 10000),
        "market_trade_limit": (1, 10000),
        "maximum_candidates_to_review": (1, 100),
        "required_independent_experts": (2, 10),
    }
    for key, (minimum, maximum) in peer_integer_ranges.items():
        value = peer.get(key)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"peer_discovery.{key} must be from {minimum} to {maximum}")
    for key in ("target_terms", "excluded_terms"):
        values = config.get(key)
        if not isinstance(values, list) or not values or any(
            not isinstance(value, str) or not value or value != value.lower()
            for value in values
        ):
            raise ValueError(f"{key} must contain lowercase strings")
    if set(config["target_terms"]) & set(config["excluded_terms"]):
        raise ValueError("target and excluded terms cannot overlap")
    return config


def fetch_json(url, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": "polymarket-m4-candidate-audit/1"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        payload = json.load(response)
    if not isinstance(payload, (dict, list)):
        raise ValueError("API response is not an object or list")
    return payload


def api_url(base, params):
    return f"{base}?{urllib.parse.urlencode(params)}"


def fetch_leaderboards(config):
    payloads, errors = {}, {}

    def fetch_one(category, period):
        key = f"{category}:{period}"
        url = api_url(
            f"{DATA_API}/v1/leaderboard",
            {
                "category": category,
                "timePeriod": period,
                "orderBy": "PNL",
                "limit": config["leaderboard_limit"],
            },
        )
        payload = fetch_json(url, config["request_timeout_seconds"])
        if not isinstance(payload, list):
            raise ValueError("leaderboard response is not a list")
        return key, url, payload

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(fetch_one, category, period): f"{category}:{period}"
            for category in config["categories"]
            for period in config["periods"]
        }
        for future, key in futures.items():
            try:
                name, url, payload = future.result()
                payloads[name] = {"url": url, "rows": payload}
            except Exception as exc:
                errors[key] = f"{type(exc).__name__}: {exc}"
    return payloads, errors


def discover_candidates(leaderboards, config):
    candidates = {}
    for key, source in sorted(leaderboards.items()):
        category, period = key.split(":", 1)
        for row in source["rows"]:
            if not isinstance(row, dict):
                continue
            wallet = str(row.get("proxyWallet", "")).lower()
            if not WALLET_PATTERN.fullmatch(wallet):
                continue
            rank = number(row.get("rank"))
            candidate = candidates.setdefault(
                wallet,
                {
                    "wallet": wallet,
                    "user_name": None,
                    "x_username": None,
                    "verified_badge": False,
                    "leaderboard_appearances": [],
                },
            )
            candidate["user_name"] = row.get("userName") or candidate["user_name"]
            candidate["x_username"] = row.get("xUsername") or candidate["x_username"]
            candidate["verified_badge"] = bool(
                row.get("verifiedBadge") or candidate["verified_badge"]
            )
            candidate["leaderboard_appearances"].append(
                {
                    "category": category,
                    "period": period,
                    "rank": int(rank) if rank is not None else None,
                    "pnl": number(row.get("pnl")),
                    "volume": number(row.get("vol")),
                }
            )

    result = []
    for candidate in candidates.values():
        appearances = candidate["leaderboard_appearances"]
        candidate["leaderboard_appearance_count"] = len(appearances)
        candidate["categories"] = sorted({item["category"] for item in appearances})
        candidate["periods"] = sorted({item["period"] for item in appearances})
        ranks = [item["rank"] for item in appearances if item["rank"] is not None]
        candidate["best_rank"] = min(ranks) if ranks else None
        if len(appearances) >= config["minimum_leaderboard_appearances"]:
            result.append(candidate)
    result.sort(
        key=lambda item: (
            -item["leaderboard_appearance_count"],
            -len(item["categories"]),
            -len(item["periods"]),
            item["best_rank"] if item["best_rank"] is not None else 10**9,
            item["wallet"],
        )
    )
    return result


def fetch_candidate_evidence(candidate, config, observed_at):
    wallet = candidate["wallet"]
    start = int((observed_at - timedelta(days=config["recent_trade_lookback_days"])).timestamp())
    end = int(observed_at.timestamp())
    requests = {
        "trades": api_url(
            f"{DATA_API}/trades",
            {
                "user": wallet,
                "start": start,
                "end": end,
                "limit": config["recent_trade_limit"],
                "takerOnly": "false",
            },
        ),
        "closed_positions": api_url(
            f"{DATA_API}/closed-positions",
            {
                "user": wallet,
                "limit": config["closed_position_limit"],
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
        ),
        "profile": api_url(PROFILE_API, {"address": wallet}),
    }
    result = {"wallet": wallet, "urls": requests, "errors": {}}
    for name, url in requests.items():
        try:
            payload = fetch_json(url, config["request_timeout_seconds"])
            expected = dict if name == "profile" else list
            if not isinstance(payload, expected):
                raise ValueError(f"{name} response has the wrong type")
            result[name] = payload
        except Exception as exc:
            result[name] = None
            result["errors"][name] = f"{type(exc).__name__}: {exc}"
    return result


def fetch_all_candidate_evidence(candidates, config, observed_at):
    evidence = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(fetch_candidate_evidence, candidate, config, observed_at): candidate[
                "wallet"
            ]
            for candidate in candidates
        }
        for future, wallet in futures.items():
            try:
                evidence[wallet] = future.result()
            except Exception as exc:
                evidence[wallet] = {
                    "wallet": wallet,
                    "errors": {"candidate": f"{type(exc).__name__}: {exc}"},
                    "trades": None,
                    "closed_positions": None,
                    "profile": None,
                }
    return evidence


def classify_title(title, config):
    text = str(title or "").lower()
    if any(term_matches(text, term) for term in config["excluded_terms"]):
        return "excluded"
    if any(term_matches(text, term) for term in config["target_terms"]):
        return "target"
    return "unknown"


def term_matches(text, term):
    phrase = term.strip()
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))


def directional_actions(trades, config, start_timestamp=0, target_only=False):
    rows_by_wallet_market = defaultdict(list)
    window_seconds = config["aggregation_window_minutes"] * 60
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        wallet = str(trade.get("proxyWallet") or "").lower()
        condition = str(trade.get("conditionId") or "").lower()
        side = str(trade.get("side") or "").upper()
        outcome_index = number(trade.get("outcomeIndex"))
        price, size, timestamp = (
            number(trade.get("price")),
            number(trade.get("size")),
            number(trade.get("timestamp")),
        )
        title = str(trade.get("title") or "")
        if (
            not WALLET_PATTERN.fullmatch(wallet)
            or not CONDITION_PATTERN.fullmatch(condition)
            or side not in ("BUY", "SELL")
            or outcome_index not in (0, 1)
            or price is None
            or size is None
            or timestamp is None
            or not 0 <= price <= 1
            or size <= 0
            or timestamp < start_timestamp
            or (target_only and classify_title(title, config) != "target")
        ):
            continue
        direction = (1 if side == "BUY" else -1) * (
            1 if int(outcome_index) == 0 else -1
        )
        rows_by_wallet_market[(wallet, condition)].append(
            {
                "timestamp": int(timestamp),
                "direction": direction,
                "size": size,
                "notional": price * size,
                "price": price,
                "title": title,
                "event_slug": trade.get("eventSlug"),
                "name": trade.get("name"),
                "pseudonym": trade.get("pseudonym"),
                "transaction_hash": trade.get("transactionHash"),
            }
        )

    actions = []
    for (wallet, condition), rows in rows_by_wallet_market.items():
        rows.sort(key=lambda item: item["timestamp"])
        bucket, bucket_start = [], None
        for row in rows:
            if bucket_start is None or row["timestamp"] - bucket_start < window_seconds:
                bucket.append(row)
                bucket_start = row["timestamp"] if bucket_start is None else bucket_start
            else:
                actions.append((wallet, condition, bucket))
                bucket, bucket_start = [row], row["timestamp"]
        if bucket:
            actions.append((wallet, condition, bucket))

    result = []
    for wallet, condition, bucket in actions:
        gross_notional = sum(row["notional"] for row in bucket)
        total_contracts = sum(row["size"] for row in bucket)
        net_contracts = sum(row["direction"] * row["size"] for row in bucket)
        average_observed_price = gross_notional / total_contracts
        net_directional_notional = abs(net_contracts) * average_observed_price
        if net_directional_notional < config["minimum_material_notional_usd"]:
            continue
        result.append(
            {
                "wallet": wallet,
                "condition_id": condition,
                "event_slug": next(
                    (row["event_slug"] for row in bucket if row["event_slug"]), None
                ),
                "title": next((row["title"] for row in bucket if row["title"]), None),
                "name": next((row["name"] for row in bucket if row["name"]), None),
                "pseudonym": next(
                    (row["pseudonym"] for row in bucket if row["pseudonym"]), None
                ),
                "started_at": bucket[0]["timestamp"],
                "ended_at": bucket[-1]["timestamp"],
                "direction_outcome_index": 0 if net_contracts > 0 else 1,
                "gross_notional_usd": rounded(gross_notional),
                "net_directional_notional_usd": rounded(net_directional_notional),
                "net_contracts": rounded(abs(net_contracts)),
                "minimum_observed_price": rounded(min(row["price"] for row in bucket)),
                "maximum_observed_price": rounded(max(row["price"] for row in bucket)),
                "source_trade_count": len(bucket),
            }
        )
    return result


def match_peer_actions(reference_actions, market_actions, config):
    peer_config = config["peer_discovery"]
    reference_wallets = set(peer_config["reference_wallets"])
    window_seconds = peer_config["consensus_window_hours"] * 3600
    references_by_market = defaultdict(list)
    for action in reference_actions:
        references_by_market[action["condition_id"]].append(action)

    peers = {}
    for action in market_actions:
        if action["wallet"] in reference_wallets:
            continue
        references = [
            reference
            for reference in references_by_market[action["condition_id"]]
            if reference["direction_outcome_index"]
            == action["direction_outcome_index"]
            and abs(reference["started_at"] - action["started_at"]) <= window_seconds
        ]
        if not references:
            continue
        reference = min(
            references,
            key=lambda item: abs(item["started_at"] - action["started_at"]),
        )
        peer = peers.setdefault(
            action["wallet"],
            {
                "wallet": action["wallet"],
                "user_name": action["name"],
                "pseudonym": action["pseudonym"],
                "markets": {},
                "matching_action_count": 0,
                "matched_gross_notional_usd": 0.0,
                "matched_net_directional_notional_usd": 0.0,
                "absolute_time_deltas_seconds": [],
                "signed_time_deltas_seconds": [],
            },
        )
        peer["user_name"] = action["name"] or peer["user_name"]
        peer["pseudonym"] = action["pseudonym"] or peer["pseudonym"]
        peer["matching_action_count"] += 1
        peer["matched_gross_notional_usd"] += action["gross_notional_usd"]
        peer["matched_net_directional_notional_usd"] += action[
            "net_directional_notional_usd"
        ]
        signed_delta = action["started_at"] - reference["started_at"]
        delta = abs(signed_delta)
        peer["absolute_time_deltas_seconds"].append(delta)
        peer["signed_time_deltas_seconds"].append(signed_delta)
        market = peer["markets"].setdefault(
            action["condition_id"],
            {
                "condition_id": action["condition_id"],
                "event_slug": action["event_slug"],
                "title": action["title"],
                "matching_action_count": 0,
                "first_peer_action_at": action["started_at"],
                "last_peer_action_at": action["started_at"],
                "minimum_time_delta_seconds": delta,
                "reference_wallets": set(),
            },
        )
        market["matching_action_count"] += 1
        market["first_peer_action_at"] = min(
            market["first_peer_action_at"], action["started_at"]
        )
        market["last_peer_action_at"] = max(
            market["last_peer_action_at"], action["started_at"]
        )
        market["minimum_time_delta_seconds"] = min(
            market["minimum_time_delta_seconds"], delta
        )
        market["reference_wallets"].add(reference["wallet"])

    candidates = []
    for peer in peers.values():
        if len(peer["markets"]) < peer_config["minimum_shared_target_markets"]:
            continue
        markets = []
        for market in peer.pop("markets").values():
            market["reference_wallets"] = sorted(market["reference_wallets"])
            markets.append(market)
        markets.sort(key=lambda item: item["condition_id"])
        deltas = peer.pop("absolute_time_deltas_seconds")
        signed_deltas = peer.pop("signed_time_deltas_seconds")
        peer["shared_target_market_count"] = len(markets)
        peer["matched_gross_notional_usd"] = rounded(
            peer["matched_gross_notional_usd"]
        )
        peer["matched_net_directional_notional_usd"] = rounded(
            peer["matched_net_directional_notional_usd"]
        )
        peer["median_absolute_time_delta_seconds"] = rounded(statistics.median(deltas))
        peer["median_signed_time_delta_seconds"] = rounded(
            statistics.median(signed_deltas)
        )
        peer["within_one_minute_match_count"] = sum(
            abs(delta) <= 60 for delta in signed_deltas
        )
        peer["peer_action_after_reference_count"] = sum(
            delta > 0 for delta in signed_deltas
        )
        peer["peer_action_before_reference_count"] = sum(
            delta < 0 for delta in signed_deltas
        )
        peer["same_second_match_count"] = sum(delta == 0 for delta in signed_deltas)
        peer["synchronous_follower_warning"] = (
            len(signed_deltas) >= 3
            and peer["within_one_minute_match_count"] / len(signed_deltas) >= 0.8
            and peer["peer_action_after_reference_count"] / len(signed_deltas) >= 0.8
        )
        peer["markets"] = markets
        candidates.append(peer)
    candidates.sort(
        key=lambda item: (
            -item["shared_target_market_count"],
            -item["matching_action_count"],
            -item["matched_net_directional_notional_usd"],
            item["wallet"],
        )
    )
    return candidates


def trade_metrics(trades, config):
    counts = defaultdict(int)
    notionals = defaultdict(float)
    examples = defaultdict(list)
    timestamps, trade_notionals, markets = [], [], set()
    action_rows = defaultdict(list)
    invalid = 0
    window_seconds = config["aggregation_window_minutes"] * 60
    for trade in trades or []:
        if not isinstance(trade, dict):
            invalid += 1
            continue
        price, size, timestamp = (
            number(trade.get("price")),
            number(trade.get("size")),
            number(trade.get("timestamp")),
        )
        if (
            price is None
            or size is None
            or timestamp is None
            or not 0 <= price <= 1
            or size <= 0
        ):
            invalid += 1
            continue
        title = str(trade.get("title") or "")
        domain = classify_title(title, config)
        notional = price * size
        condition = str(trade.get("conditionId") or trade.get("slug") or title)
        side = str(trade.get("side") or "UNKNOWN").upper()
        outcome = str(trade.get("outcome") or "")
        action_key = (condition, outcome, side)
        action_rows[action_key].append((timestamp, notional))
        timestamps.append(timestamp)
        trade_notionals.append(notional)
        markets.add(condition)
        counts[domain] += 1
        counts[side.lower()] += 1
        notionals[domain] += notional
        if price <= config["extreme_price_low"] or price >= config["extreme_price_high"]:
            counts["extreme"] += 1
        if title and title not in examples[domain] and len(examples[domain]) < 3:
            examples[domain].append(title)
    valid = len(trade_notionals)
    total_notional = sum(trade_notionals)
    action_notionals = []
    for rows in action_rows.values():
        cluster_start, cluster_notional = None, 0.0
        for timestamp, notional in sorted(rows):
            if cluster_start is None or timestamp - cluster_start < window_seconds:
                cluster_start = timestamp if cluster_start is None else cluster_start
                cluster_notional += notional
            else:
                action_notionals.append(cluster_notional)
                cluster_start, cluster_notional = timestamp, notional
        if cluster_start is not None:
            action_notionals.append(cluster_notional)
    action_median = statistics.median(action_notionals) if action_notionals else 0.0
    material_floor = max(config["minimum_material_notional_usd"], action_median)
    span_hours = (max(timestamps) - min(timestamps)) / 3600 if len(timestamps) > 1 else 0.0
    material_count = sum(notional >= material_floor for notional in action_notionals)
    return {
        "raw_trade_count": len(trades or []),
        "valid_trade_count": valid,
        "invalid_trade_count": invalid,
        "distinct_market_count": len(markets),
        "sample_limit_reached": len(trades or []) >= config["recent_trade_limit"],
        "sample_span_hours": rounded(span_hours),
        "buy_count": counts["buy"],
        "sell_count": counts["sell"],
        "median_trade_notional_usd": rounded(
            statistics.median(trade_notionals) if trade_notionals else None
        ),
        "aggregated_action_count": len(action_notionals),
        "median_aggregated_action_notional_usd": rounded(action_median),
        "material_action_floor_usd": rounded(material_floor),
        "material_aggregated_action_count": material_count,
        "material_aggregated_actions_per_day": rounded(
            material_count / (span_hours / 24) if span_hours else None
        ),
        "target_trade_notional_fraction": fraction(notionals["target"], total_notional),
        "excluded_trade_notional_fraction": fraction(notionals["excluded"], total_notional),
        "unknown_trade_notional_fraction": fraction(notionals["unknown"], total_notional),
        "extreme_price_fraction": fraction(counts["extreme"], valid),
        "examples": dict(examples),
    }


def closed_position_metrics(positions, config):
    valid, invalid, realized_pnl = 0, 0, []
    domains, examples = defaultdict(int), defaultdict(list)
    event_pnl = defaultdict(float)
    for position in positions or []:
        if not isinstance(position, dict):
            invalid += 1
            continue
        pnl = number(position.get("realizedPnl"))
        if pnl is None:
            invalid += 1
            continue
        title = str(position.get("title") or "")
        domain = classify_title(title, config)
        valid += 1
        domains[domain] += 1
        realized_pnl.append(pnl)
        event = str(
            position.get("eventSlug")
            or position.get("conditionId")
            or position.get("title")
            or f"unknown-{valid}"
        )
        event_pnl[event] += pnl
        if title and title not in examples[domain] and len(examples[domain]) < 3:
            examples[domain].append(title)
    absolute_event_pnl = [abs(value) for value in event_pnl.values()]
    total_absolute = sum(absolute_event_pnl)
    return {
        "raw_closed_position_count": len(positions or []),
        "valid_closed_position_count": valid,
        "invalid_closed_position_count": invalid,
        "sample_limit_reached": len(positions or []) >= config["closed_position_limit"],
        "sample_realized_pnl_usd": rounded(sum(realized_pnl)),
        "profitable_position_fraction": fraction(sum(pnl > 0 for pnl in realized_pnl), valid),
        "target_position_fraction": fraction(domains["target"], valid),
        "excluded_position_fraction": fraction(domains["excluded"], valid),
        "unknown_position_fraction": fraction(domains["unknown"], valid),
        "distinct_event_count": len(event_pnl),
        "single_event_abs_pnl_fraction": fraction(
            max(absolute_event_pnl) if absolute_event_pnl else 0.0, total_absolute
        ),
        "examples": dict(examples),
    }


def audit_candidate(candidate, evidence, config):
    trades = trade_metrics(evidence.get("trades"), config)
    closed = closed_position_metrics(evidence.get("closed_positions"), config)
    required_errors = {
        key: value
        for key, value in evidence.get("errors", {}).items()
        if key in ("candidate", "trades", "closed_positions")
    }
    gates = config["gates"]
    checks = {
        "required_sources_complete": not required_errors,
        "leaderboard_recurrence": candidate["leaderboard_appearance_count"]
        >= config["minimum_leaderboard_appearances"],
        "enough_closed_positions": closed["valid_closed_position_count"]
        >= gates["minimum_closed_positions"],
        "target_domain_purity": (trades["target_trade_notional_fraction"] or 0)
        >= gates["minimum_target_trade_notional_fraction"],
        "excluded_domain_cap": (trades["excluded_trade_notional_fraction"] or 0)
        <= gates["maximum_excluded_trade_notional_fraction"],
        "closed_target_domain_purity": (closed["target_position_fraction"] or 0)
        >= gates["minimum_target_trade_notional_fraction"],
        "closed_excluded_domain_cap": (closed["excluded_position_fraction"] or 0)
        <= gates["maximum_excluded_trade_notional_fraction"],
        "low_frequency_activity": (
            trades["material_aggregated_actions_per_day"] or 0
        )
        <= config["maximum_material_actions_per_day"],
        "extreme_price_cap": (trades["extreme_price_fraction"] or 0)
        <= gates["maximum_extreme_price_fraction"],
        "closed_pnl_concentration_cap": (closed["single_event_abs_pnl_fraction"] or 0)
        <= gates["maximum_single_closed_event_abs_pnl_fraction"],
        "has_material_actions": trades["material_aggregated_action_count"] > 0,
    }
    flags = []
    if trades["sample_limit_reached"]:
        flags.append("recent trades hit the configured limit; reported shares are truncated")
    if closed["sample_limit_reached"]:
        flags.append("closed positions hit the configured limit; PnL metrics are truncated")
    if trades["valid_trade_count"] >= 100 and trades["sample_span_hours"] <= 6:
        flags.append("high-burst activity may be delay-sensitive or scanner-like")
    if (
        trades["material_aggregated_actions_per_day"] or 0
    ) > config["maximum_material_actions_per_day"]:
        flags.append(
            f"more than {config['maximum_material_actions_per_day']:g} sampled material actions per day conflicts with low-frequency preference"
        )
    if (trades["unknown_trade_notional_fraction"] or 0) > 0.25:
        flags.append("more than 25% of sampled trade notional is unclassified")
    if evidence.get("errors", {}).get("profile"):
        flags.append("public profile unavailable; identity explainability is weaker")
    eligible = all(checks.values())
    result = {
        **candidate,
        "profile": evidence.get("profile"),
        "source_errors": evidence.get("errors", {}),
        "trade_audit": trades,
        "closed_position_audit": closed,
        "screen_checks": checks,
        "screen_status": (
            "observation_review"
            if eligible
            else "insufficient_evidence"
            if required_errors
            else "rejected_by_screen"
        ),
        "flags": flags,
        "note": "Candidate only. This audit does not establish expertise or alpha.",
    }
    result["screen_pass_count"] = sum(checks.values())
    return result


def build_report(leaderboards, leaderboard_errors, evidence, config, observed_at):
    discovered = discover_candidates(leaderboards, config)
    selected = discovered[: config["maximum_candidates_to_audit"]]
    audits = [
        audit_candidate(candidate, evidence.get(candidate["wallet"], {}), config)
        for candidate in selected
    ]
    audits.sort(
        key=lambda item: (
            item["screen_status"] != "observation_review",
            -item["screen_pass_count"],
            -item["leaderboard_appearance_count"],
            -(item["trade_audit"]["target_trade_notional_fraction"] or 0),
            item["best_rank"] if item["best_rank"] is not None else 10**9,
            item["wallet"],
        )
    )
    pool = [
        {
            "wallet": item["wallet"],
            "user_name": item["user_name"],
            "x_username": item["x_username"],
            "reason": "Passed the mechanical candidate screen; manual identity and independence review remains required.",
        }
        for item in audits
        if not leaderboard_errors and item["screen_status"] == "observation_review"
    ][: config["observation_pool_limit"]]
    required_candidate_errors = sum(
        bool(
            {
                key: value
                for key, value in item["source_errors"].items()
                if key in ("candidate", "trades", "closed_positions")
            }
        )
        for item in audits
    )
    return {
        "generated_at": observed_at.isoformat(),
        "configuration_version": config["version"],
        "mode": config["mode"],
        "paper_only": True,
        "status": "complete"
        if not leaderboard_errors and not required_candidate_errors
        else "partial",
        "source": config["source"],
        "leaderboard_snapshot_count": len(leaderboards),
        "leaderboard_errors": leaderboard_errors,
        "raw_recurring_candidate_count": len(discovered),
        "audited_candidate_count": len(audits),
        "observation_pool_count": len(pool),
        "observation_pool": pool,
        "candidates": audits,
        "method": {
            "selection_order": "recurrence, category breadth, period breadth, then best rank",
            "profitability_claim": "none",
            "independence_status": "manual review required; three wallets do not imply three independent experts",
            "future_use": "freeze an approved observation pool, then collect prospective evidence only",
        },
        "limitations": [
            "The discovery snapshot is selected with current leaderboard data and cannot prove historical alpha.",
            "Trade-domain classification is a conservative editable title-keyword screen, not ground truth.",
            "Trade and closed-position endpoints may hit configured limits; truncation is explicit per candidate.",
            "A passing wallet remains a candidate until identity, independence, delay and out-of-sample tests pass.",
            "No order, position, credential, wallet or active M2/M3 state is created or changed.",
        ],
    }


def write_artifacts(raw, report, observed_at):
    stamp = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = RUNTIME / f"candidate-sources-{stamp}.json.gz"
    report_path = RUNTIME / f"candidate-audit-{stamp}.json"
    m1.atomic_json(raw_path, raw, compress=True)
    report = {
        **report,
        "raw_evidence_content_sha256": content_hash(raw),
        "raw_evidence_file_sha256": file_hash(raw_path),
        "raw_evidence_path": str(raw_path),
    }
    m1.atomic_json(report_path, report)
    m1.atomic_json(LATEST_REPORT_PATH, report)
    return report_path, raw_path, report


def fetch_peer_sources(config, observed_at):
    peer_config = config["peer_discovery"]
    start = int(
        (observed_at - timedelta(days=peer_config["lookback_days"])).timestamp()
    )
    end = int(observed_at.timestamp())
    reference_sources = {}

    def fetch_reference(wallet):
        url = api_url(
            f"{DATA_API}/trades",
            {
                "user": wallet,
                "start": start,
                "end": end,
                "limit": peer_config["reference_trade_limit"],
                "takerOnly": "false",
            },
        )
        payload = fetch_json(url, config["request_timeout_seconds"])
        if not isinstance(payload, list):
            raise ValueError("reference trade response is not a list")
        return wallet, {"url": url, "trades": payload, "error": None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(fetch_reference, wallet): wallet
            for wallet in peer_config["reference_wallets"]
        }
        for future, wallet in futures.items():
            try:
                name, source = future.result()
                reference_sources[name] = source
            except Exception as exc:
                reference_sources[wallet] = {
                    "url": None,
                    "trades": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    reference_rows = [
        row
        for source in reference_sources.values()
        for row in (source.get("trades") or [])
    ]
    reference_actions = directional_actions(
        reference_rows, config, start_timestamp=start, target_only=True
    )
    conditions = sorted({action["condition_id"] for action in reference_actions})
    market_sources = {}

    def fetch_market(condition):
        url = api_url(
            f"{DATA_API}/trades",
            {
                "market": condition,
                "start": start,
                "end": end,
                "limit": peer_config["market_trade_limit"],
                "takerOnly": "false",
            },
        )
        payload = fetch_json(url, config["request_timeout_seconds"])
        if not isinstance(payload, list):
            raise ValueError("market trade response is not a list")
        return condition, {"url": url, "trades": payload, "error": None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_market, condition): condition for condition in conditions
        }
        for future, condition in futures.items():
            try:
                name, source = future.result()
                market_sources[name] = source
            except Exception as exc:
                market_sources[condition] = {
                    "url": None,
                    "trades": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    return start, reference_sources, reference_actions, market_sources


def summarize_peer_profile(wallet, source):
    profile = source.get("profile") if isinstance(source, dict) else None
    profile = profile if isinstance(profile, dict) else {}
    name = profile.get("name")
    x_username = profile.get("xUsername")
    readable_name = bool(name and not re.fullmatch(r"0x[a-fA-F0-9]{40}(?:-\d+)?", name))
    identity_status = (
        "public_x" if x_username else "named_profile" if readable_name else "wallet_only"
    )
    return {
        "name": name,
        "pseudonym": profile.get("pseudonym"),
        "x_username": x_username,
        "verified_badge": bool(profile.get("verifiedBadge")),
        "profile_created_at": profile.get("createdAt"),
        "identity_status": identity_status,
        "profile_error": (
            source.get("errors", {}).get("profile") or source.get("error")
            if isinstance(source, dict)
            else "missing"
        ),
        "identity_note": "Public identity is evidence for review, not proof of a distinct beneficial owner.",
        "wallet": wallet,
    }


def build_peer_report(raw, candidates, config, observed_at):
    peer_config = config["peer_discovery"]
    reference_errors = {
        wallet: source["error"]
        for wallet, source in raw["reference_sources"].items()
        if source.get("error")
    }
    market_errors = {
        condition: source["error"]
        for condition, source in raw["market_sources"].items()
        if source.get("error")
    }
    reference_limit_hits = sum(
        len(source.get("trades") or []) >= peer_config["reference_trade_limit"]
        for source in raw["reference_sources"].values()
    )
    market_limit_hits = sum(
        len(source.get("trades") or []) >= peer_config["market_trade_limit"]
        for source in raw["market_sources"].values()
    )
    review = []
    for candidate in candidates[: peer_config["maximum_candidates_to_review"]]:
        evidence = raw["candidate_evidence"].get(candidate["wallet"], {})
        profile = summarize_peer_profile(candidate["wallet"], evidence)
        trades = trade_metrics(evidence.get("trades"), config)
        closed = closed_position_metrics(evidence.get("closed_positions"), config)
        warnings = []
        if candidate.get("synchronous_follower_warning"):
            warnings.append(
                "at least eighty percent of matches follow the reference within one minute"
            )
        if profile["identity_status"] == "wallet_only":
            warnings.append("wallet-only identity")
        if trades["sample_limit_reached"]:
            warnings.append("recent trade sample is truncated")
        if closed["sample_limit_reached"]:
            warnings.append("closed-position sample is truncated")
        if (trades["target_trade_notional_fraction"] or 0) < 0.5:
            warnings.append("less than half of sampled trade notional is in the target domain")
        if (trades["excluded_trade_notional_fraction"] or 0) > 0.1:
            warnings.append("more than ten percent of sampled trade notional is excluded")
        if (
            trades["material_aggregated_actions_per_day"] or 0
        ) > config["maximum_material_actions_per_day"]:
            warnings.append("sampled activity conflicts with the low-frequency preference")
        if (closed["single_event_abs_pnl_fraction"] or 0) > 0.25:
            warnings.append("sampled closed PnL is concentrated above twenty-five percent")
        review.append(
            {
                **candidate,
                "profile": profile,
                "recent_trade_audit": trades,
                "closed_position_audit": closed,
                "review_warnings": warnings,
                "source_errors": evidence.get("errors", {}),
                "candidate_status": "manual_identity_strategy_independence_review",
                "expert_status": "not_approved",
            }
        )
    return {
        "generated_at": observed_at.isoformat(),
        "configuration_version": config["version"],
        "mode": "peer_discovery",
        "paper_only": True,
        "status": "complete" if not reference_errors and not market_errors else "partial",
        "reference_wallets": peer_config["reference_wallets"],
        "lookback_days": peer_config["lookback_days"],
        "consensus_window_hours": peer_config["consensus_window_hours"],
        "minimum_shared_target_markets": peer_config[
            "minimum_shared_target_markets"
        ],
        "seed_target_market_count": len(raw["market_sources"]),
        "reference_material_action_count": raw["reference_material_action_count"],
        "mechanical_peer_count": len(candidates),
        "review_candidate_count": len(review),
        "review_candidates": review,
        "source_completeness": {
            "reference_errors": reference_errors,
            "market_errors": market_errors,
            "reference_samples_at_limit": reference_limit_hits,
            "market_samples_at_limit": market_limit_hits,
        },
        "eligibility": {
            "price_filter": "none; observed prices are recorded but never gate admission",
            "time_to_resolution_filter": "none; time to resolution is not an admission gate",
            "materiality": f"estimated net directional notional must reach ${config['minimum_material_notional_usd']:g}; this is not a price threshold",
            "same_market": "exact conditionId only",
            "same_direction": "binary outcome-index exposure after 30-minute netting",
        },
        "team": {
            "required_independent_experts": peer_config["required_independent_experts"],
            "team_ready": False,
            "status": "manual review required; wallet count is not expert independence",
        },
        "tracking": {
            "status": "not_started",
            "reason": "A reviewable independent cohort must be frozen before prospective tracking.",
        },
        "limitations": [
            "Current-data discovery is in-sample candidate generation and cannot establish alpha.",
            "A market sample at the public API limit is visibly truncated; presence is evidence, absence is not.",
            "A sell may close an old position rather than express a new thesis; later tracking must reconstruct net position changes.",
            "Similar names, timing or wallets do not prove common ownership, and different names do not prove independence.",
            "No signal, paper position, order, credential, M2/M3 integration or M4 evidence clock is created.",
        ],
    }


def write_peer_artifacts(raw, report, observed_at):
    stamp = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = RUNTIME / f"peer-sources-{stamp}.json.gz"
    report_path = RUNTIME / f"peer-discovery-{stamp}.json"
    m1.atomic_json(raw_path, raw, compress=True)
    report = {
        **report,
        "raw_evidence_content_sha256": content_hash(raw),
        "raw_evidence_file_sha256": file_hash(raw_path),
        "raw_evidence_path": str(raw_path),
    }
    m1.atomic_json(report_path, report)
    m1.atomic_json(LATEST_PEER_REPORT_PATH, report)
    return report_path, raw_path, report


def run_peer_discovery(config=None):
    config = validate_config(config or load_config())
    observed_at = utc_now()
    start, reference_sources, reference_actions, market_sources = fetch_peer_sources(
        config, observed_at
    )
    market_rows = [
        row for source in market_sources.values() for row in (source.get("trades") or [])
    ]
    market_actions = directional_actions(
        market_rows, config, start_timestamp=start, target_only=True
    )
    candidates = match_peer_actions(reference_actions, market_actions, config)
    review_wallets = [
        candidate["wallet"]
        for candidate in candidates[
            : config["peer_discovery"]["maximum_candidates_to_review"]
        ]
    ]
    candidate_evidence = fetch_all_candidate_evidence(
        [{"wallet": wallet} for wallet in review_wallets], config, observed_at
    )
    raw = {
        "observed_at": observed_at.isoformat(),
        "configuration": config,
        "reference_sources": reference_sources,
        "reference_material_action_count": len(reference_actions),
        "market_sources": market_sources,
        "candidate_evidence": candidate_evidence,
    }
    report = build_peer_report(raw, candidates, config, observed_at)
    report_path, raw_path, report = write_peer_artifacts(raw, report, observed_at)
    print(
        json.dumps(
            {
                "status": report["status"],
                "generated_at": report["generated_at"],
                "seed_target_market_count": report["seed_target_market_count"],
                "mechanical_peer_count": report["mechanical_peer_count"],
                "review_candidate_count": report["review_candidate_count"],
                "team_ready": report["team"]["team_ready"],
                "report_path": str(report_path),
                "raw_evidence_path": str(raw_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "complete" else 1


def run_audit(config=None):
    config = validate_config(config or load_config())
    observed_at = utc_now()
    leaderboards, leaderboard_errors = fetch_leaderboards(config)
    discovered = discover_candidates(leaderboards, config)
    selected = discovered[: config["maximum_candidates_to_audit"]]
    evidence = fetch_all_candidate_evidence(selected, config, observed_at)
    raw = {
        "observed_at": observed_at.isoformat(),
        "configuration": config,
        "leaderboards": leaderboards,
        "leaderboard_errors": leaderboard_errors,
        "candidate_evidence": evidence,
    }
    report = build_report(
        leaderboards, leaderboard_errors, evidence, config, observed_at
    )
    report_path, raw_path, report = write_artifacts(raw, report, observed_at)
    print(
        json.dumps(
            {
                "status": report["status"],
                "generated_at": report["generated_at"],
                "audited_candidate_count": report["audited_candidate_count"],
                "observation_pool_count": report["observation_pool_count"],
                "report_path": str(report_path),
                "raw_evidence_path": str(raw_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "complete" else 1


def show_status():
    if not LATEST_REPORT_PATH.exists():
        print(json.dumps({"status": "not_run", "report_path": str(LATEST_REPORT_PATH)}, indent=2))
        return 1
    report = json.loads(LATEST_REPORT_PATH.read_text(encoding="utf-8"))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def show_peer_status():
    if not LATEST_PEER_REPORT_PATH.exists():
        print(
            json.dumps(
                {"status": "not_run", "report_path": str(LATEST_PEER_REPORT_PATH)},
                indent=2,
            )
        )
        return 1
    report = json.loads(LATEST_PEER_REPORT_PATH.read_text(encoding="utf-8"))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def check():
    config = validate_config(load_config())
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": config["mode"],
                "paper_only": config["paper_only"],
                "source_hosts": ["data-api.polymarket.com", "gamma-api.polymarket.com"],
                "peer_price_gate": False,
                "peer_time_to_resolution_gate": False,
                "runtime_integration": False,
                "order_capability": False,
            },
            indent=2,
        )
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description="M4 public expert-wallet research")
    parser.add_argument(
        "command", choices=("audit", "status", "peers", "peer-status", "check")
    )
    command = parser.parse_args().command
    if command == "audit":
        return run_audit()
    if command == "status":
        return show_status()
    if command == "peers":
        return run_peer_discovery()
    if command == "peer-status":
        return show_peer_status()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
