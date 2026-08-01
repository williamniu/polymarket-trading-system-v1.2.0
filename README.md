# Polymarket Trading System v1.2.0

A paper-only research system designed to become more reliable through measured feedback, reproducible experiments, and deterministic safety gates.

## Current stage

- **M1 is still collecting evidence.** No venue has passed the 168-hour/600-sample gate.
- **M2 is deployment-gated.** It adds one SQLite source of truth, heartbeats, health checks, alerts, backups, evidence migration, and a $5,000 simulated account baseline.
- There is no credential loading, signing, order submission, deposit, withdrawal, or live-trading code.
- Market making, latency arbitrage, and maker-rebate capture are prohibited as primary alpha sources.

## First-principles boundary

The deterministic runtime collects evidence and enforces hard rules. An LLM may later propose hypotheses or review results, but it cannot change risk limits, promote itself, access credentials, or place orders.

## Verify

```bash
/opt/homebrew/bin/python3.11 -m unittest discover -s tests -v
/opt/homebrew/bin/python3.11 -m py_compile m1.py m2.py tests/test_*.py
```

## M1 read-only commands

```bash
/opt/homebrew/bin/python3.11 m1.py collect
/opt/homebrew/bin/python3.11 m1.py status
```

## M2 commands

```bash
/opt/homebrew/bin/python3.11 m2.py init
/opt/homebrew/bin/python3.11 m2.py cycle
/opt/homebrew/bin/python3.11 m2.py status
/opt/homebrew/bin/python3.11 m2.py check
/opt/homebrew/bin/python3.11 m2.py backup
/opt/homebrew/bin/python3.11 m2.py migrate-m1 /path/to/old/runtime/m1
```

Runtime files are written under ignored `runtime/`. The LaunchAgent uses `service-cycle`, which starts the write-once M2 evidence clock only when the service actually executes. Installing or switching a LaunchAgent is a separate approval-gated operation.

See `docs/MENTAL_MODEL.md` for the project knowledge graph and the distinction between inherited M1 venue evidence and the new M2 runtime-stability clock.
