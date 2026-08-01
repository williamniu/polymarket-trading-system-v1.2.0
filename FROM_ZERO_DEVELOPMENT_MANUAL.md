# From-zero development manual

Version: 1.2.0

## Goal

Build a 24/7, paper-first trading research system that can improve through evidence without being allowed to rewrite its own safety boundary.

The system may eventually generate positive cash flow, but no profit is assumed. Every claimed edge must survive executable-price, fee, latency, sample-size, drawdown, and out-of-sample tests.

## Non-negotiable boundaries

- Paper only until a later milestone and fresh user approval.
- No geographic circumvention.
- No private keys, recovery phrases, or withdrawal authority in the runtime or LLM context.
- No market making, latency arbitrage, or maker-rebate capture as primary alpha.
- No LLM decisions in the deterministic execution path.
- No strategy may modify risk limits, its own promotion test, or evidence history.
- Missing, stale, corrupt, or contradictory state fails closed.
- Runtime state has one writer and one SQLite source of truth.

## Improvement loop

1. Observe immutable market evidence available at decision time.
2. Form a falsifiable hypothesis.
3. Paper-execute using executable prices, size, fees, latency, partial fills, and settlement rules.
4. Attribute outcome to signal, execution, risk, and regime.
5. Compare champion and challenger out of sample.
6. Promote only through deterministic gates; otherwise retain or roll back.

This is controlled experimentation, not unrestricted self-modification.

## Milestones

- **M0:** versioned risk policy, paper-only boundary, secret exclusions, tests.
- **M1:** seven-day public-data venue validation; at least 168 hours and 600 scheduled samples.
- **M2:** 24/7 process foundation; SQLite, single writer, heartbeat, health, alerts, simulated account, backup and recovery evidence.
- **M3:** realistic paper execution including spread, fees, latency, depth, partial fills, settlement, and reconciliation.
- **M4:** incremental signal research, including expert-wallet and cross-market hypotheses, with strict time alignment.
- **M5:** champion/challenger promotion and automatic rollback.
- **M6:** isolated parameter and code-evolution laboratory; never direct production self-editing.
- **M7:** $200-$300 live trial only after every prior gate and a new explicit approval.

Milestones may be implemented in parallel, but none may be promoted by bypassing an earlier evidence gate.

## M2 acceptance criteria

- Exactly one process can write runtime state at a time.
- SQLite uses transactions, foreign keys, WAL mode, and an integrity check.
- Every collection cycle records start, finish, venue outcome, and heartbeat.
- Venue failure, stale heartbeat, low disk, or corrupt database makes health non-zero.
- The paper account starts at $5,000 and cannot silently reset or change policy capital.
- A consistent SQLite backup can be created without copying a live database file directly.
- Restarting does not duplicate the paper account or a recorded cycle.
- No order-capable or credential-bearing code exists.
- Seven days and 600 cycles are required before M2 promotion.

## Change control

Critical changes require user approval before execution:

- changing any risk limit;
- enabling credentials, authenticated trading, live orders, deposits, or withdrawals;
- switching the active 24/7 service or its authoritative database;
- promoting a milestone or strategy;
- publishing secrets or runtime data;
- weakening a test, alert, fail-closed condition, or adversarial gate.

Source changes should remain small, use the Python standard library when sufficient, and leave one runnable check for non-trivial logic.

