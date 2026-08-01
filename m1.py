#!/opt/homebrew/bin/python3.11
import argparse
import concurrent.futures
import gzip
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "m1.json"
RUNTIME = ROOT / "runtime" / "m1"
SNAPSHOTS_PATH = RUNTIME / "snapshots.jsonl"
REPORT_PATH = RUNTIME / "report.json"
POLYMARKET_URL = "https://gateway.polymarket.us/v1/markets"
KALSHI_URL = "https://external-api.kalshi.com/trade-api/v2/markets"
KALSHI_DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2/markets"


def utc_now():
    return datetime.now(timezone.utc)


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def fetch_json(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-m1-readonly/1"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("API response is not an object")
    return payload, round((time.monotonic() - started) * 1000, 1)


def fetch_polymarket(config):
    started = time.monotonic()
    markets, pages = [], []
    limit = 500
    for page in range(config["maximum_pages"]):
        query = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": page * limit,
                "orderBy": "volumeNum",
                "orderDirection": "desc",
            }
        )
        payload, elapsed = fetch_json(f"{POLYMARKET_URL}?{query}", config["request_timeout_seconds"])
        batch = payload.get("markets")
        if not isinstance(batch, list):
            raise ValueError("Polymarket response has no markets list")
        pages.append(payload)
        markets.extend(batch)
        if len(batch) < limit:
            truncated = False
            break
        if len(markets) >= config["market_sample_limit"]:
            markets = markets[: config["market_sample_limit"]]
            truncated = True
            break
    else:
        raise RuntimeError("Polymarket pagination exceeded maximum_pages")

    sample = sorted(
        markets,
        key=lambda market: (
            number(market.get("volume24hr")) or 0.0,
            number(market.get("liquidityNum", market.get("liquidity"))) or 0.0,
        ),
        reverse=True,
    )[: config["book_sample_size"]]

    def get_book(market):
        slug = market.get("slug")
        if not slug:
            return {"ok": False, "error": "missing slug"}
        try:
            safe_slug = urllib.parse.quote(str(slug), safe="")
            payload, _ = fetch_json(
                f"{POLYMARKET_URL}/{safe_slug}/book",
                config["request_timeout_seconds"],
            )
            return {"ok": True, "slug": slug, "payload": payload}
        except Exception as exc:
            return {"ok": False, "slug": slug, "error": f"{type(exc).__name__}: {exc}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        books = list(pool.map(get_book, sample))
    raw = {"market_pages": pages, "book_samples": books, "sample_truncated": truncated}
    return markets, raw, round((time.monotonic() - started) * 1000, 1), books, truncated


def fetch_kalshi(config, demo=False):
    started = time.monotonic()
    markets, pages, cursor = [], [], ""
    base_url = KALSHI_DEMO_URL if demo else KALSHI_URL
    limit = 1 if demo else 1000
    max_pages = 1 if demo else config["maximum_pages"]
    for _ in range(max_pages):
        params = {"status": "open", "limit": limit}
        if not demo:
            params["mve_filter"] = "exclude"
        if cursor:
            params["cursor"] = cursor
        payload, elapsed = fetch_json(
            f"{base_url}?{urllib.parse.urlencode(params)}",
            config["request_timeout_seconds"],
        )
        batch = payload.get("markets")
        if not isinstance(batch, list):
            raise ValueError("Kalshi response has no markets list")
        pages.append(payload)
        markets.extend(batch)
        next_cursor = payload.get("cursor") or ""
        if demo or not next_cursor or next_cursor == cursor:
            sample = [] if demo else sorted(
                markets,
                key=lambda market: (
                    number(market.get("volume_24h_fp")) or 0.0,
                    number(market.get("liquidity_dollars")) or 0.0,
                ),
                reverse=True,
            )[: config["book_sample_size"]]
            return (
                markets,
                {"market_pages": pages, "sample_truncated": False},
                round((time.monotonic() - started) * 1000, 1),
                sample,
                False,
            )
        if len(markets) >= config["market_sample_limit"]:
            markets = markets[: config["market_sample_limit"]]
            sample = sorted(
                markets,
                key=lambda market: (
                    number(market.get("volume_24h_fp")) or 0.0,
                    number(market.get("liquidity_dollars")) or 0.0,
                ),
                reverse=True,
            )[: config["book_sample_size"]]
            return (
                markets,
                {"market_pages": pages, "sample_truncated": True},
                round((time.monotonic() - started) * 1000, 1),
                sample,
                True,
            )
        cursor = next_cursor
    raise RuntimeError("Kalshi pagination exceeded maximum_pages")


def number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def median(values):
    return round(statistics.median(values), 6) if values else None


def book_quote(book):
    if not book.get("ok"):
        return None
    market_data = book.get("payload", {}).get("marketData", {})
    bids = [
        (number(level.get("px")), number(level.get("qty")))
        for level in market_data.get("bids", [])
    ]
    offers = [
        (number(level.get("px")), number(level.get("qty")))
        for level in market_data.get("offers", [])
    ]
    bids = [(price, qty) for price, qty in bids if price is not None and qty is not None and qty > 0]
    offers = [(price, qty) for price, qty in offers if price is not None and qty is not None and qty > 0]
    if not bids or not offers:
        return None
    bid, bid_qty = max(bids, key=lambda item: item[0])
    ask, ask_qty = min(offers, key=lambda item: item[0])
    return bid, ask, bid_qty, ask_qty


def summarize_markets(markets, venue, observed_at=None, quote_sample=None):
    observed_at = observed_at or utc_now()
    spreads, top_notional, liquidity, volume, hours_to_close = [], [], [], [], []
    groups, rules, invalid_quotes = Counter(), 0, 0

    for market in markets:
        if venue == "polymarket_us":
            bid = number(market.get("bestBidQuote", market.get("bestBid")))
            ask = number(market.get("bestAskQuote", market.get("bestAsk")))
            group = market.get("question")
            rule = market.get("description")
            liquidity_value = number(market.get("liquidityNum", market.get("liquidity")))
            volume_value = number(market.get("volume24hr"))
            close_time = iso_datetime(market.get("endDate"))
        else:
            bid = number(market.get("yes_bid_dollars"))
            ask = number(market.get("yes_ask_dollars"))
            group = market.get("event_ticker")
            rule = market.get("rules_primary")
            liquidity_value = number(market.get("liquidity_dollars"))
            volume_value = number(market.get("volume_24h_fp"))
            close_time = iso_datetime(market.get("close_time"))

        if group:
            groups[str(group).strip().lower()] += 1
        if rule:
            rules += 1
        if liquidity_value is not None and liquidity_value >= 0:
            liquidity.append(liquidity_value)
        if volume_value is not None and volume_value >= 0:
            volume.append(volume_value)
        if close_time and close_time > observed_at:
            hours_to_close.append((close_time - observed_at).total_seconds() / 3600)

    quote_sample = markets if quote_sample is None else quote_sample
    for item in quote_sample:
        if venue == "polymarket_us" and "ok" in item:
            quote = book_quote(item)
        elif venue == "polymarket_us":
            quote = (
                number(item.get("bestBidQuote", item.get("bestBid"))),
                number(item.get("bestAskQuote", item.get("bestAsk"))),
                1.0,
                1.0,
            )
        else:
            quote = (
                number(item.get("yes_bid_dollars")),
                number(item.get("yes_ask_dollars")),
                number(item.get("yes_bid_size_fp")),
                number(item.get("yes_ask_size_fp")),
            )
        if quote is None or any(value is None for value in quote):
            continue
        bid, ask, bid_qty, ask_qty = quote
        if 0 <= bid <= ask <= 1 and bid_qty > 0 and ask_qty > 0:
            spreads.append(ask - bid)
            top_notional.append(min(bid * bid_qty, ask * ask_qty))
        else:
            invalid_quotes += 1

    count = len(markets)
    quote_count = len(quote_sample)
    grouped = sum(size for size in groups.values() if size >= 2)
    return {
        "market_count": count,
        "quote_sample_count": quote_count,
        "two_sided_quote_count": len(spreads),
        "quote_coverage": round(len(spreads) / quote_count, 6) if quote_count else 0.0,
        "median_spread": median(spreads),
        "median_top_quote_notional": median(top_notional),
        "invalid_quote_count": invalid_quotes,
        "structured_group_count": sum(size >= 2 for size in groups.values()),
        "structured_group_market_coverage": round(grouped / count, 6) if count else 0.0,
        "rules_coverage": round(rules / count, 6) if count else 0.0,
        "median_liquidity": median(liquidity),
        "median_volume_24h": median(volume),
        "median_hours_to_close": median(hours_to_close),
    }


def atomic_json(path, payload, compress=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if compress:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    else:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_snapshot(snapshot):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with SNAPSHOTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_snapshots():
    if not SNAPSHOTS_PATH.exists():
        return []
    snapshots = []
    for line_number, line in enumerate(SNAPSHOTS_PATH.read_text(encoding="utf-8").splitlines(), 1):
        try:
            snapshots.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt snapshot line {line_number}") from exc
    return snapshots


def mean_metric(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return round(statistics.mean(values), 6) if values else None


def median_metric(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return median(values)


def venue_report(snapshots, venue, config, elapsed_hours, enough_samples):
    attempts = len(snapshots)
    rows = [item["venues"][venue] for item in snapshots if item["venues"].get(venue, {}).get("ok")]
    success_rate = round(len(rows) / attempts, 6) if attempts else 0.0
    quote_coverage = mean_metric(rows, "quote_coverage") or 0.0
    rules_coverage = mean_metric(rows, "rules_coverage") or 0.0
    structure_coverage = mean_metric(rows, "structured_group_market_coverage") or 0.0
    spread = median_metric(rows, "median_spread")
    top_notional = median_metric(rows, "median_top_quote_notional") or 0.0
    spread_score = 0.0 if spread is None else max(0.0, 1.0 - spread / 0.20)
    depth_score = min(1.0, top_notional / 100.0)
    score = round(
        25 * success_rate
        + 20 * quote_coverage
        + 20 * spread_score
        + 10 * depth_score
        + 15 * structure_coverage
        + 10 * rules_coverage,
        3,
    )
    gates = config["quality_gates"]
    reasons = []
    if elapsed_hours < config["duration_hours"]:
        reasons.append("duration")
    if not enough_samples:
        reasons.append("sample_count")
    if success_rate < gates["minimum_success_rate"]:
        reasons.append("success_rate")
    if quote_coverage < gates["minimum_quote_coverage"]:
        reasons.append("quote_coverage")
    if rules_coverage < gates["minimum_rules_coverage"]:
        reasons.append("rules_coverage")
    if top_notional < gates["minimum_median_top_quote_notional"]:
        reasons.append("top_quote_notional")
    return {
        "attempts": attempts,
        "successes": len(rows),
        "success_rate": success_rate,
        "median_sampled_market_count": median_metric(rows, "market_count"),
        "truncated_sample_rate": mean_metric(rows, "sample_truncated") or 0.0,
        "mean_quote_coverage": quote_coverage,
        "median_spread": spread,
        "median_top_quote_notional": top_notional,
        "mean_structured_group_market_coverage": structure_coverage,
        "mean_rules_coverage": rules_coverage,
        "median_liquidity": median_metric(rows, "median_liquidity"),
        "median_volume_24h": median_metric(rows, "median_volume_24h"),
        "median_hours_to_close": median_metric(rows, "median_hours_to_close"),
        "median_latency_ms": median_metric(rows, "latency_ms"),
        "score": score,
        "eligible_for_selection": not reasons,
        "gate_reasons": reasons,
    }


def build_report(snapshots, config):
    if snapshots:
        first = iso_datetime(snapshots[0]["collected_at"])
        last = iso_datetime(snapshots[-1]["collected_at"])
        elapsed_hours = round((last - first).total_seconds() / 3600, 3)
    else:
        elapsed_hours = 0.0
    enough_samples = len(snapshots) >= config["minimum_samples"]
    venues = {
        venue: venue_report(snapshots, venue, config, elapsed_hours, enough_samples)
        for venue in ("polymarket_us", "kalshi")
    }
    candidates = [name for name, result in venues.items() if result["eligible_for_selection"]]
    winner = max(candidates, key=lambda name: venues[name]["score"]) if candidates else None
    if winner:
        status = "complete"
    elif elapsed_hours >= config["duration_hours"] and enough_samples:
        status = "insufficient_quality"
    else:
        status = "collecting"
    demo_successes = sum(
        bool(item["venues"].get("kalshi_demo_probe", {}).get("ok")) for item in snapshots
    )
    return {
        "generated_at": utc_now().isoformat(),
        "status": status,
        "elapsed_hours": elapsed_hours,
        "sample_count": len(snapshots),
        "required_hours": config["duration_hours"],
        "required_samples": config["minimum_samples"],
        "winner": winner,
        "venues": venues,
        "kalshi_demo_probe_successes": demo_successes,
        "note": "Spread and depth measure whether slower directional or relative-value signals can execute; market making is not an alpha source. Legal and account eligibility remain separate gates.",
    }


def collect_snapshot(config=None, raw_dir=None):
    config, observed_at = config or load_config(), utc_now()
    stamp = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
    snapshot = {"collected_at": observed_at.isoformat(), "venues": {}}
    jobs = {
        "polymarket_us": lambda: fetch_polymarket(config),
        "kalshi": lambda: fetch_kalshi(config),
        "kalshi_demo_probe": lambda: fetch_kalshi(config, demo=True),
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        for name, future in futures.items():
            try:
                markets, raw, latency_ms, quote_sample, truncated = future.result()
                if name == "kalshi_demo_probe":
                    summary = {"market_count": len(markets)}
                else:
                    summary = summarize_markets(markets, name, observed_at, quote_sample)
                snapshot["venues"][name] = {
                    "ok": True,
                    "latency_ms": round(latency_ms, 1),
                    "sample_truncated": truncated,
                    **summary,
                }
                if raw_dir is not None:
                    atomic_json(
                        raw_dir / f"{stamp}-{name}.json.gz",
                        {"collected_at": observed_at.isoformat(), **raw},
                        compress=True,
                    )
            except Exception as exc:
                snapshot["venues"][name] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    return snapshot


def collect():
    config = load_config()
    snapshot = collect_snapshot(config, RUNTIME / "raw")
    append_snapshot(snapshot)
    report = build_report(read_snapshots(), config)
    atomic_json(REPORT_PATH, report)
    print(json.dumps({"snapshot": snapshot, "report": report}, indent=2, ensure_ascii=False))
    return 0 if all(snapshot["venues"][name]["ok"] for name in ("polymarket_us", "kalshi")) else 1


def main():
    parser = argparse.ArgumentParser(description="M1 read-only venue bake-off")
    parser.add_argument("command", choices=("collect", "report", "status"))
    command = parser.parse_args().command
    if command == "collect":
        return collect()
    report = build_report(read_snapshots(), load_config())
    if command == "report":
        atomic_json(REPORT_PATH, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
