import json
import plistlib
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import m1


ROOT = Path(__file__).resolve().parents[1]


class M1Test(unittest.TestCase):
    def test_homebrew_python_and_schedule_are_pinned(self):
        config = json.loads((ROOT / "config" / "m1.json").read_text(encoding="utf-8"))
        with (ROOT / "ops" / "com.williamniu.polymarket-m1.plist").open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["ProgramArguments"][0], "/opt/homebrew/bin/python3.11")
        self.assertEqual(plist["StartInterval"], config["interval_seconds"])
        self.assertEqual(plist["ProgramArguments"][-1], "collect")
        self.assertNotIn("KeepAlive", plist)

    def test_only_official_public_market_hosts_are_used(self):
        self.assertEqual(m1.POLYMARKET_URL, "https://gateway.polymarket.us/v1/markets")
        self.assertEqual(m1.KALSHI_URL, "https://external-api.kalshi.com/trade-api/v2/markets")
        self.assertEqual(
            m1.KALSHI_DEMO_URL,
            "https://external-api.demo.kalshi.co/trade-api/v2/markets",
        )

    def test_polymarket_summary_uses_two_sided_executable_quotes(self):
        markets = [
            {
                "question": "Champion",
                "description": "Rule A",
                "bestBidQuote": {"value": "0.40"},
                "bestAskQuote": {"value": "0.45"},
                "liquidityNum": 100,
            },
            {
                "question": "Champion",
                "description": "Rule B",
                "bestAskQuote": {"value": "0.60"},
                "liquidityNum": 50,
            },
        ]
        result = m1.summarize_markets(markets, "polymarket_us")
        self.assertEqual(result["quote_coverage"], 0.5)
        self.assertEqual(result["median_spread"], 0.05)
        self.assertEqual(result["structured_group_market_coverage"], 1.0)

    def test_invalid_or_crossed_quote_is_not_counted(self):
        result = m1.summarize_markets(
            [{
                "event_ticker": "E",
                "rules_primary": "R",
                "yes_bid_dollars": "0.7",
                "yes_ask_dollars": "0.6",
                "yes_bid_size_fp": "1",
                "yes_ask_size_fp": "1",
            }],
            "kalshi",
        )
        self.assertEqual(result["quote_coverage"], 0.0)
        self.assertEqual(result["invalid_quote_count"], 1)

    def test_zero_size_quote_is_not_executable(self):
        result = m1.summarize_markets(
            [{
                "event_ticker": "E",
                "rules_primary": "R",
                "yes_bid_dollars": "0.4",
                "yes_ask_dollars": "0.6",
                "yes_bid_size_fp": "0",
                "yes_ask_size_fp": "10",
            }],
            "kalshi",
        )
        self.assertEqual(result["quote_coverage"], 0.0)

    def test_selection_cannot_finish_before_time_and_sample_gates(self):
        config = deepcopy(m1.load_config())
        config["duration_hours"] = 1
        config["minimum_samples"] = 2
        start = datetime(2026, 7, 31, tzinfo=timezone.utc)
        snapshots = [self.snapshot(start)]
        self.assertEqual(m1.build_report(snapshots, config)["status"], "collecting")
        snapshots.append(self.snapshot(start + timedelta(hours=2)))
        report = m1.build_report(snapshots, config)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["winner"], "kalshi")

    def test_pagination_limit_fails_closed(self):
        config = deepcopy(m1.load_config())
        config["maximum_pages"] = 1
        config["market_sample_limit"] = 1000
        full_page = {"markets": [{} for _ in range(500)]}
        with patch.object(m1, "fetch_json", return_value=(full_page, 1.0)):
            with self.assertRaisesRegex(RuntimeError, "pagination exceeded"):
                m1.fetch_polymarket(config)

    def test_corrupt_snapshot_log_fails_closed(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            path = Path(directory) / "snapshots.jsonl"
            path.write_text("{not-json}\n", encoding="utf-8")
            with patch.object(m1, "SNAPSHOTS_PATH", path):
                with self.assertRaisesRegex(ValueError, "corrupt snapshot line 1"):
                    m1.read_snapshots()

    @staticmethod
    def snapshot(timestamp):
        common = {
            "ok": True,
            "market_count": 100,
            "sample_truncated": False,
            "quote_coverage": 1.0,
            "structured_group_market_coverage": 0.5,
            "rules_coverage": 1.0,
            "median_liquidity": 100.0,
            "median_volume_24h": 10.0,
            "median_top_quote_notional": 100.0,
            "median_hours_to_close": 24.0,
            "latency_ms": 100.0,
        }
        polymarket = {**common, "median_spread": 0.10}
        kalshi = {**common, "median_spread": 0.02}
        return {
            "collected_at": timestamp.isoformat(),
            "venues": {
                "polymarket_us": polymarket,
                "kalshi": kalshi,
                "kalshi_demo_probe": {"ok": True},
            },
        }


if __name__ == "__main__":
    unittest.main()
