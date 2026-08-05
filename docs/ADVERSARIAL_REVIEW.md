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

## M3.5 runtime shadow review

Date: 2026-08-01

Decision: **PASS for approved public-data paper deployment and evidence collection only.** This does not promote M3, prove an edge, or authorize credentials or live orders.

Verified attacks:

- the active SQLite database was backed up and integrity-checked before schema migration;
- schema migration alone created no order intent and did not start the M3 clock;
- M3 runs inside the existing M2 writer and cannot create a second LaunchAgent or database;
- selected event, theme, tick and fee fields are derived from sealed official market metadata;
- in-play, sub-cent, combo, scalar, fractional, thin or one-sided markets fail eligibility;
- raw point responses, normalized books, the execution configuration and result are separately sealed;
- point-request p95 plus 250 ms determines eligibility, and slow failed depth probes remain in the latency sample;
- runtime fills use 50% depth, two-tick limits and 1.25x fee coefficients with rebates ignored;
- M3 errors do not erase or fail the completed M2 collection cycle;
- a reconciliation error durably alerts and freezes M3 while M2 continues;
- the safe runtime switch can stop probes without rewriting evidence; other configuration changes fail after the clock starts;
- no credential header, private key, authenticated call or order endpoint exists in the runtime source.

Deployment evidence:

- Pre-migration backup `state-20260802T023541355506Z.sqlite3` passed integrity check and has SHA-256 `4507ef05db25e468c9aa15f2e5c1b84710b56e9bf3b4d8fa4dc665282387d19e`.
- Schema version 2 initialized with M3 clock null, zero intents, zero failures and zero reconciliation errors; M2 remained healthy.
- The first natural LaunchAgent cycle was 115 at `2026-08-02T03:04:03Z`. Its Polymarket US probe filled one paper contract, reconciled exactly, and started the M3 clock at `2026-08-02T03:04:09.386305Z`.
- The first probe measured 76.3 ms decision-book and 79.4 ms execution-book requests, producing a 327 ms p95-plus-buffer assumption. Six sealed artifacts re-verified and the post-probe SQLite backup passed integrity check.
- The second natural LaunchAgent cycle was 116 at `2026-08-02T03:19:10Z`. Its Kalshi probe filled one paper contract and reconciled exactly; venue alternation, the 365 ms Kalshi latency assumption and the one-contract rule operated as approved.
- After two probes, all 12 sealed artifacts re-verified, both reconciliations were exact, SQLite integrity was `ok`, LaunchAgent reported 41 runs with last exit code 0, and stderr was empty. Final backup `state-20260802T031948951665Z.sqlite3` has SHA-256 `e08becfe110c1dcb67a49b237bca63efd88ccf101fadd65a5d997d1268fbadf9`.
- Homebrew Python 3.11: 62 repository tests, compilation, JSON parsing, plist validation, whitespace checks and `m3.py check` passed before deployment.

Findings closed during review:

- **High:** the runtime response initially allowed reconciliation `status=ok` to overwrite probe `status=recorded`. Runtime, execution and reconciliation statuses are now distinct.
- **High:** only successful probes initially fed the latency distribution. The decision latency is now persisted before the depth gate, preventing survivor bias from slow failures.
- **High:** normalized books alone could hide an adapter mistake. Raw and normalized decision/execution books are now separately sealed and retained.
- **High:** Polymarket's live market metadata reported fee coefficient 0.06 while the published base theta is 0.05. Runtime execution uses the approved 1.25x coefficient, 0.0625, and rejects a reported coefficient above that floor.
- **High:** increasing probe size before every open position receives fresh executable marking could hide unrealized risk. M3.5 now enforces exactly one contract; larger probes require a separately approved marking expansion.

Residual gates:

- M3 has only begun its 168-hour/600-intent evidence gate and is not eligible for promotion.
- Probe PnL measures execution friction and must not be presented as predictive alpha.
- At most one sub-$1 probe position per venue can remain until that venue's next turn; M4 must isolate strategy positions before signals are added.
- Polymarket US resting fills remain unverified without complete public trade evidence; M3.5 uses only marketable-limit probes.
- Alert delivery remains local; remote notifications and the dashboard are future milestones.

## M3.6 position lifecycle and settlement review

Date: 2026-08-02

Decision: **PASS for approved active paper-only M3.6 deployment and segment-2 evidence collection.** This repairs execution evidence only. It does not promote M3, establish alpha, or authorize live trading.

Verified attacks:

- an open position omitted from the 5,000-market sample is recovered only through its exact official identifier;
- an exact response with the wrong market, event, theme or product identity fails closed;
- a final Kalshi result must agree with its official settlement value;
- a Polymarket settlement must be binary and belong to the requested slug;
- a non-final market remains open without a guessed mark or settlement;
- a risk-reducing exit may bypass the entry-time buffer but still requires a valid executable book;
- official metadata and settlement responses are sealed inside the persisted settlement object;
- duplicate settlement remains impossible and every mutation reconciles cash and equity;
- a fresh evidence segment archives prior counters without deleting historical probes, orders or alerts;
- an evidence segment cannot be reset while the runtime probe is enabled;
- a forged `recorded` probe without a matching paper order does not count toward promotion.

Pre-deployment evidence:

- Homebrew Python 3.11: 67 repository tests, compilation and whitespace checks passed.
- An online backup copy archived segment 1 with 46 valid paper intents and 30 failed probes, then initialized segment 2 at zero.
- Dry-run cycle 191 recovered and closed the omitted active Polymarket position from its exact public endpoint.
- Dry-run cycle 192 settled the finalized Kalshi YES position at `$1.00`, sealed the complete official response, reconciled exactly, and opened the next one-contract probe.
- After both dry-run cycles, segment 2 had two valid intents, zero failures, zero reconciliation errors, no pending probe and `PRAGMA integrity_check = ok`.

Active deployment evidence:

- Final adversarial verification passed 68 repository tests, including the official Polymarket settlement schema and the paused-probe segment-reset guard; compilation, JSON, plist and whitespace checks also passed.
- Immediate pre-deployment backup `state-20260802T222621982706Z.sqlite3` passed integrity check and has SHA-256 `2795620ea9ce95b1ad21c12cc3d3201c9765492ea955494f64547d0b5c667054`.
- Segment 1 was archived at probe 77 with 46 valid intents and 30 failures. Segment 2 began at probe 78; no historical probe, order, settlement or alert was deleted.
- The first natural post-deployment LaunchAgent cycle was 192. It recovered the finalized Kalshi position by exact ticker, verified that official result `yes` agreed with settlement value `1`, sealed the response, settled one YES contract for `$1.00`, reconciled exactly, and opened the next one-contract Kalshi probe.
- The second natural post-deployment LaunchAgent cycle was 193. It recovered and closed the omitted active Polymarket position from the exact market endpoint, then reconciled exactly. No Polymarket position remained.
- All 14 new evidence seals re-verified, `PRAGMA integrity_check` returned `ok`, LaunchAgent reported 118 runs with last exit code 0, and stderr was empty.
- At `2026-08-02T22:44:12Z`, segment 2 had two valid intents, zero failures, zero reconciliation errors and no pending probe. The paper account held `$4,998.23` cash, `$4,998.77` executable equity and one sub-$1 Kalshi probe position.

Findings closed during review:

- **Critical:** finalized positions were never routed into the existing settlement engine, so stale marks could persist indefinitely. Exact-market lifecycle resolution now settles final binary outcomes from sealed official evidence.
- **High:** broad sample truncation was treated as missing official metadata, stranding otherwise valid positions. Existing positions now use exact market lookup; the broad list remains only an entry candidate source.
- **High:** the 60-minute entry buffer also blocked late risk-reducing exits. Entry and exit eligibility are now distinct while book validation remains unchanged.
- **High:** the original M3 clock could have promoted evidence collected under the lifecycle defect. Segment 1 is immutable diagnostic history; segment 2 alone counts toward promotion.
- **High:** settlement rows retained only a derived hash, not the replayable official source. The sealed settlement object and source response are now persisted together.

Residual gates:

- Segment 2 must independently reach 168 hours, 600 valid intents and zero reconciliation errors.
- Probe PnL remains execution-friction evidence, not predictive alpha.
- Polymarket US resting fills remain unverified; runtime probes remain marketable-limit only.
- Remote alert delivery and a user dashboard remain future milestones.

## M3.7 dual-venue throughput and operations review

Date: 2026-08-04

Decision: **PASS for active paper-only M3.7 deployment and segment-3 evidence collection.** This increases execution-evidence density. It does not establish alpha, promote M3 or authorize live trading.

Verified attacks:

- Polymarket binary outcomes accept exactly one Yes and one No in either order, while duplicates, missing outcomes and identity changes still fail closed;
- schema migration preserves every old probe and changes uniqueness only from one probe per cycle to one probe per cycle per venue;
- one venue failure cannot stop the other venue or be overwritten by its success heartbeat;
- every configured venue must independently contribute 250 valid intents, so one venue cannot brute-force the 600-intent gate;
- both probes use separate official point books, evidence seals, order IDs and reconciliations;
- the 900-second M2 schedule, one writer, one database, one-contract size and all execution conservatism remain unchanged;
- M3 segment 2 is archived before configuration v3 starts segment 3.

Pre-deployment evidence:

- Homebrew Python 3.11: 70 repository tests, compilation and the isolated `m3.py check` passed.
- Segment 2 was archived with 124 valid intents, 91 failed probes and zero reconciliation errors; no historical row was deleted.
- Pre-change backup `state-20260805T045535152139Z.sqlite3` passed integrity check and has SHA-256 `59755fcf960bf9e436949fb44279897b2539c92dd4271f41363b43be6266a0f0`.
- An isolated copy migrated to schema version 4 with `PRAGMA integrity_check = ok`, configuration version 3 and a null segment-3 evidence clock.
- Isolated live-data cycle 409 recorded both Polymarket US and Kalshi intents in the same M2 cycle. All 12 probe seals re-verified and both cash/equity reconciliations were exact.

Active deployment evidence:

- Configuration version 3 and schema version 4 deployed with probes paused; M1/M2 continued while segment 2 was archived and segment 3 initialized with a null evidence clock.
- Homebrew Python 3.11 passed all 70 repository tests, compilation, `m3.py check`, SQLite integrity and whitespace checks against the deployed source.
- The first natural enabled LaunchAgent cycle was 460 at `2026-08-05T18:01:01Z`. It completed in 6.953 seconds and recorded distinct orders `m3-probe-460-polymarket_us` and `m3-probe-460-kalshi` under the `(cycle_id, venue)` uniqueness constraint.
- Both one-contract paper orders filled from independent fresh public books and reconciled exactly. Each probe retained raw and normalized decision/execution books, the execution configuration and result; four independent latency observations were recorded.
- Segment 3 started at `2026-08-05T18:01:08.247402Z` with two valid intents, one per venue, zero failures, zero pending probes, zero reconciliation errors and no account freeze. LaunchAgent reported 385 runs with last exit code 0 and process stderr was empty.
- By natural cycle 464, segment 3 had nine valid intents and one explicit Kalshi failure because the point book was not two-sided. The same cycle still recorded its independent Polymarket US intent, while status retained the Kalshi error, M2 stayed healthy, reconciliation errors remained zero and the paper account did not freeze. This is the expected safe-failure behavior, not a fill to optimize away.
- Post-deployment backup `state-20260805T182159449164Z.sqlite3` passed integrity check and has SHA-256 `7e037f58422dee79d17f619f725a6b61d84092726f6dbc1162c0056082cc7772`.

Residual gates:

- Segment 3 must independently reach 168 hours, 600 aggregate intents, at least 250 per venue and zero reconciliation errors.
- Probe PnL remains execution-friction evidence, not predictive alpha.
