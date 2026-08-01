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

Decision: **IN PROGRESS.** The existing local collector remains the evidence producer until an approval-gated service switch. No venue winner may be selected before 168 hours, 600 samples, and every quality gate.

## M2 implementation review

Date: 2026-08-01

Decision: **PASS for implementation and a single manual probe only.** M2 is not deployed and cannot be promoted until its own 168-hour/600-cycle evidence gate passes.

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

- Homebrew Python 3.11: 25 unit tests passed.
- Python compilation, JSON parsing, plist validation, and whitespace checks passed.
- A sandboxed network probe failed DNS resolution; the cycle persisted as `partial_failure`, created two venue alerts, marked the heartbeat degraded, and returned non-zero.
- The approved read-only network probe then completed as `ok`, persisted one SQLite cycle and three raw compressed responses, and made health pass.
- SQLite `PRAGMA integrity_check` returned `ok`; one and only one $5,000 paper account exists.
- SQLite online backup completed and passed its own integrity check.

Findings closed during review:

- **High:** low disk originally created a critical alert but could still return an `ok` cycle. Low disk now degrades the heartbeat, marks the cycle partial failure, and returns non-zero.
- **High:** a corrupt database could fail while opening the status connection before the health handler ran. Status and check now report unhealthy without crashing.

Residual gates:

- Existing M1 evidence remains in the previous local runtime. Moving it or switching the LaunchAgent requires explicit approval and a reconciliation plan.
- The new M2 database contains only manual probe evidence. The 24/7 clock has not started.
- Alert delivery is stored locally only; remote notification and dashboard work remain future, separately scoped work.

## M1-to-M2 migration review

Date: 2026-08-01

Status: implementation verified; production cutover evidence pending.

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
