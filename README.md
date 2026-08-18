# Polymarket Trading System v1.2.0

A paper-only research system designed to become more reliable through measured feedback, reproducible experiments, and deterministic safety gates.

## Current stage

- **M1 venue validation has met its mechanical time/sample gate.** Kalshi passes the configured quality screen; Polymarket US still fails the top-quote-notional gate. Formal review and legal/account eligibility remain separate.
- **M2 paper infrastructure is deployed and mechanically promotion-eligible.** It remains active while formal review is pending.
- M2 uses one SQLite source of truth, heartbeats, health checks, alerts, backups, evidence migration, and a $5,000 simulated account baseline.
- **M3.7 paper shadow execution is connected to the existing M2 service.** Each scheduled cycle runs one-contract public-data probes for both venues, resolves open positions through exact public market endpoints, seals official settlements, and keeps promotion evidence in immutable segments.
- **M4.1-C prospective expert-wallet observation is deployed.** Configuration v4 freezes a five-wallet comparison panel and collects public trades every 15 minutes. It reconstructs 30-minute net directional actions, records the first executable quote available to us and another quote five minutes later, then waits for exact final outcomes. Every wallet remains `not_approved`; no signal, paper position or order exists.
- M3 probe PnL is execution-friction evidence, and M4 candidates are leads rather than experts. Neither is a profitability claim.
- There is no credential loading, signing, order submission, deposit, withdrawal, or live-trading code.
- Market making, latency arbitrage, and maker-rebate capture are prohibited as primary alpha sources.

## First-principles boundary

The deterministic runtime collects evidence and enforces hard rules. An LLM may later propose hypotheses or review results, but it cannot change risk limits, promote itself, access credentials, or place orders.

## Verify

```bash
/opt/homebrew/bin/python3.11 -m unittest discover -s tests -v
/opt/homebrew/bin/python3.11 -m py_compile m1.py m2.py m3.py m4.py tests/test_*.py
```

## M1 read-only commands

```bash
/opt/homebrew/bin/python3.11 m1.py collect
/opt/homebrew/bin/python3.11 m1.py status
```

## M2 commands

```bash
/opt/homebrew/bin/python3.11 m2.py init
/opt/homebrew/bin/python3.11 m2.py m3-init
/opt/homebrew/bin/python3.11 m2.py m3-new-segment "approved reason"
/opt/homebrew/bin/python3.11 m2.py cycle
/opt/homebrew/bin/python3.11 m2.py status
/opt/homebrew/bin/python3.11 m2.py check
/opt/homebrew/bin/python3.11 m2.py backup
/opt/homebrew/bin/python3.11 m2.py migrate-m1 /path/to/old/runtime/m1
```

Runtime files are written under ignored `runtime/`. The LaunchAgent uses `service-cycle`, which starts the write-once M2 evidence clock only when the service actually executes. The approved M1-to-M2 service switch completed on 2026-08-01; future operational or live-capital changes remain separately approval-gated.

There is intentionally only one active runtime directory, `runtime/m2/`. M1 history was imported into `runtime/m2/imports/`; M3 runs inside the same single writer and stores its probes, orders, positions, settlements and reconciliations in `runtime/m2/state.sqlite3`. Separate M1 or M3 runtime directories would create competing sources of truth.

`m2.py status` is the one-command operational view. It checks SQLite, heartbeat freshness, disk, archive integrity and the latest cycle; reports M1/M2/M3 clocks and promotion gates; shows M3 counts and latest probe by venue; and prints the authoritative database and log paths. For the macOS scheduler's own run count and exit code, use `launchctl print gui/$(id -u)/com.williamniu.polymarket-m2`. Each scheduled JSON result is appended to `runtime/m2/collector.log`; process-level stderr goes to `runtime/m2/collector-error.log`.

See `docs/MENTAL_MODEL.md` for the project knowledge graph and the distinction between inherited M1 venue evidence and the new M2 runtime-stability clock.

## M3 commands

```bash
/opt/homebrew/bin/python3.11 m3.py check
/opt/homebrew/bin/python3.11 m2.py status
```

The safe operational stop is `runtime_probe.enabled` in `config/m3.json`. `m3-new-segment` is an approval-gated maintenance command that requires this switch to be disabled and archives the prior counters without rewriting its rows. M3 configuration v3 runs both venues per 15-minute M2 cycle and requires at least 250 valid intents per venue as part of the unchanged 168-hour/600-intent gate. See `docs/USER_DECISIONS.md` for the complete user-editable control surface.

## M4.1-A2/B/C public-data commands

```bash
/opt/homebrew/bin/python3.11 m4.py check
/opt/homebrew/bin/python3.11 m4.py audit
/opt/homebrew/bin/python3.11 m4.py status
/opt/homebrew/bin/python3.11 m4.py peers
/opt/homebrew/bin/python3.11 m4.py peer-status
/opt/homebrew/bin/python3.11 m4.py tracking-init
/opt/homebrew/bin/python3.11 m4.py tracking-cycle
/opt/homebrew/bin/python3.11 m4.py tracking-status
```

`audit` reads public leaderboard, trade, closed-position and profile endpoints. `peers` finds wallets acting in the same direction on the exact same `conditionId` within six hours of a reference action. It has no price or time-to-resolution admission gate; the $100 net-directional-notional floor removes dust and offsetting churn and is not a probability-price threshold. Both commands write a compressed source artifact plus separate content and file SHA-256 values under ignored `runtime/m2/research/m4/`; neither opens SQLite. A match means only “candidate for manual observation,” never “independent expert” or “alpha.”

`tracking-init` is a one-time evidence-clock operation; do not run it casually. The deployed `com.williamniu.polymarket-m4` LaunchAgent invokes `tracking-cycle` every five minutes, while wallet trade collection remains every 15 minutes. The separate append-only hash chain is `runtime/m2/research/m4/prospective/evidence.jsonl`, with immutable compressed source captures below `prospective/raw/`. A configuration change after the clock starts fails closed and requires an approved new M4 segment design; it does not modify M2/M3 SQLite. Use `tracking-status` for the current panel counts and review gate.
