# Adversarial review ledger

Promotion is blocked by any unresolved critical or high-severity finding. A narrative without reproducible evidence is not a pass.

## Permanent attacks

- Look-ahead, stale, malformed, duplicated, or contradictory data.
- Midpoint or unlimited-size fills that were not executable.
- Missing fees, latency, partial fills, settlement, or correlated exposure.
- A restart, retry, or second writer duplicating or corrupting state.
- A strategy or LLM relaxing its own limits, tests, or promotion criteria.
- Research, dashboard, or logs reaching credentials or order endpoints.
- Results that cannot be replayed from immutable inputs and versioned configuration.

## M0

Decision: **PASS for the paper-only baseline.** Exact approved limits and fail-closed controls have tests. This is not live-trading approval.

## M1

Decision: **IN PROGRESS.** The old M1 service is stopped, its final evidence is archived, and M2 now continues the same venue-quality evidence chain. No venue winner may be selected before 168 hours, 600 samples, and every quality gate.

## M2 implementation review

Date: 2026-08-01

Decision: **PASS for implementation and approved paper-only deployment.** This starts M2 evidence collection; it does not promote M2, establish an edge, or authorize live trading. M2 cannot be promoted until its own 168-hour/600-cycle evidence gate passes.

Verified attacks:

- second writer cannot acquire the lock;
- failed venue persists a failed cycle and alert;
- stale heartbeat and low disk fail health;
- corrupt database fails integrity check;
- repeated initialization preserves the same single paper account;
- policy capital mismatch fails closed;
- backup passes SQLite integrity check;
- source inventory contains no credential or order implementation.

Evidence:

- Homebrew Python 3.11: 31 unit tests passed after migration and evidence-clock attacks were added.
- Python compilation, JSON parsing, plist validation, and whitespace checks passed.
- A sandboxed network probe failed DNS resolution; the cycle persisted as `partial_failure`, created two venue alerts, marked the heartbeat degraded, and returned non-zero.
- The approved read-only network probe then completed as `ok`, persisted one SQLite cycle and three raw compressed responses, and made health pass.
- SQLite `PRAGMA integrity_check` returned `ok`; one and only one $5,000 paper account exists.
- SQLite online backup completed and passed its own integrity check.

Findings closed during review:

- **High:** low disk originally created a critical alert but could still return an `ok` cycle. Low disk now degrades the heartbeat, marks the cycle partial failure, and returns non-zero.
- **High:** a corrupt database could fail while opening the status connection before the health handler ran. Status and check now report unhealthy without crashing.

Residual gates:

- M2 runtime evidence has only just started: two scheduled cycles over about 15 minutes versus the required 168 hours and 600 cycles.
- Venue validation remains below 168 hours and 600 samples, and no venue winner exists.
- Alert delivery is stored locally only; remote notification and dashboard work remain future, separately scoped work.

## M1-to-M2 migration review

Date: 2026-08-01

Status: **PASS for evidence-preserving paper-service cutover.** This is not M2 promotion.

Required invariants:

- the old service is stopped before the final archive;
- every archived file has a size and SHA-256 digest;
- source and copied manifests match before the archive is accepted;
- malformed or reordered snapshots import no cycle rows;
- repeated import is idempotent;
- imported counts plus known duplicates equal source counts;
- imported M1 samples continue the venue-quality evidence chain;
- imported samples and manual probes do not count toward M2's runtime-stability gate;
- the M2 evidence clock is write-once and begins inside the first actual `service-cycle`, not a manual preflight;
- a failed cutover reloads the old M1 LaunchAgent.

Findings closed before cutover:

- **Critical:** imported M1 venue samples initially could have increased the M2 runtime-stability count. Imported cycles are now explicitly tagged and excluded; the M2 clock starts once at approved cutover.
- **High:** the archive manifest was generated but not rechecked after import. Archive integrity is now part of health, and a tampered archive blocks repeat migration.
- **High:** starting the evidence clock before LaunchAgent bootstrap could count a failed cutover or rollback gap as uptime. Only `service-cycle` can start it; manual `cycle` probes cannot.

Cutover evidence:

- The old M1 LaunchAgent was stopped before the archive and remains unloaded; its original code and valid plist are preserved for rollback.
- The final M1 source contained 72 snapshots. Source and archived `snapshots.jsonl` both have SHA-256 `f2a38ed91f519f9ce042d40d4656e185f97930330e0125976110bb541e2a1855`.
- Archive `runtime/m2/imports/m1-f2a38ed91f519f9c` imported 72 cycles with zero duplicates. SQLite integrity returned `ok`; imported archive health passed.
- A manual post-migration preflight produced cycle 75 while leaving `evidence_started_at` unset and the M2 runtime count at zero.
- The M2 LaunchAgent started the write-once clock at `2026-08-01T17:14:33.332729+00:00`. Its first two planned cycles were 76 and 77, both `ok`; launchd reported `runs = 2` and `last exit code = 0`, and stderr remained empty.
- Post-cutover status was `ok`, with 77 total venue samples but only two eligible M2 runtime cycles. `eligible_for_m2_promotion` remained false and the venue winner remained null.
- Online backup `runtime/m2/backups/state-20260801T173034052294Z.sqlite3` completed and passed SQLite integrity validation.

## M3 offline execution review

Date: 2026-08-01

Decision: **PASS for offline M3.0-M3.4 only.** The engine is not connected to the active SQLite database or LaunchAgent, has no credentials or network order path, and has no M3 runtime evidence. This is not M3 deployment or promotion.

Verified attacks:

- a later favorable book cannot replace the first post-latency book;
- stale, halted, reordered, crossed, off-tick or hash-tampered books fail closed;
- observed depth is haircutted and fills never use midpoint or exceed credited size;
- YES/NO complement conversion is checked for both venue formats;
- resting touch does not fill ahead of displayed queue; incomplete trade evidence remains `unverified`;
- venue fee coefficients, roles and rounding are recalculated before ledger entry;
- a strategy cannot spoof event, theme, tick size or fee-rule identity against sealed book metadata;
- duplicate orders, oversells, risk-limit breaches, duplicate settlements and cash tampering fail;
- executable liquidation marks include depth and exit fees, and can trigger the M0 loss freeze;
- scalar, non-final or unknown-venue settlement fails closed.

Evidence:

- Homebrew Python 3.11: 54 repository tests passed, including 23 M3 adversarial tests.
- Python compilation, JSON configuration, whitespace and the isolated `m3.py check` passed.
- The offline check initializes a temporary $5,000 paper account and reconciles cash and equity exactly.
- Source inventory contains no credential loading, authenticated request, order submission or active-service change.

Findings closed during review:

- **High:** execution and reconciliation status initially shared one field name. They are now separate, so an `ok` ledger cannot hide a partial order.
- **High:** order-supplied event, theme and tick metadata could split correlated exposure or bypass price increments. Matching now requires the order to equal sealed instrument metadata.
- **High:** a sealed result could originally reach the ledger without recalculating fill totals and fees. The ledger now revalidates quantity, price, time, status, reservation and official fee math.
- **High:** only realized cash changes initially reached M0 stops. Executable liquidation marking, equity history and daily/rolling/high-watermark freezing now include unrealized risk.
- **High:** latency, queue and fee-stress knobs could be configured optimistically. The approved processing-buffer floor, queue floor, fee floor and exact venue-rule IDs now fail closed.
- **High:** fragmented fill fees could exceed the single-fill preflight estimate. Actual simulated cost is now also a floor for preflight risk.

Residual gates before M3.5:

- Instrument event/theme/tick/fee metadata must be derived from official market data, not caller text.
- Venue-specific point-orderbook latency must be measured before p95 plus 250 ms can be enforced automatically.
- Polymarket US resting fills cannot be verified from current public REST evidence alone; missing trade tape must remain `unverified` unless separately approved read-only access is added.
- Sub-cent, fractional, scalar, combo, leverage and complex collateral products remain unsupported and fail closed.
- The active database has no M3 tables, the running service imports no M3 code, and the M3 168-hour/600-intent clock has not started.
