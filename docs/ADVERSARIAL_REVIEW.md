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

## M4.1-A expert-wallet candidate audit review

Date: 2026-08-14

Decision: **PASS for an offline public-data candidate-audit tool only.** It does not establish an expert cohort, alpha, a consensus signal, M4 promotion, runtime integration or trading authorization.

Verified attacks:

- all nine Politics/Economics/Finance by Week/Month/All leaderboard slices must succeed before an observation pool can be emitted;
- wallets are deduplicated and ranked first by recurrence and breadth, not aggregate overlapping-period PnL;
- missing trades or closed positions fail a candidate closed, while a missing profile weakens identity evidence visibly;
- 30-minute aggregation prevents fragmented fills from becoming repeated expert actions;
- the aggregation uses a true rolling window per market, outcome and side, so fills around a clock boundary remain one action;
- sports, esports, weather, culture, mentions and crypto-price titles are excluded before target-domain matching;
- token-boundary matching prevents `nfl` inside `inflation` from creating false sports classifications;
- generic foreign-election titles no longer qualify merely because they contain `election`;
- a maximum of ten sampled material actions per day enforces the approved low-frequency preference;
- extreme-price, target-domain, excluded-domain and sample-size checks are explicit and user-editable;
- closed-position concentration is aggregated by event slug, so splitting one view across related contracts cannot fake diversification;
- incomplete leaderboard snapshots emit no pool, and every passing row is labeled candidate-only rather than expert;
- source and report evidence are separate, the compressed source is SHA-256 sealed, and the tool never opens SQLite or exposes order/credential capability.

Evidence:

- all 79 repository tests, including nine focused M4 tests, pass under Homebrew Python 3.11;
- final audit `2026-08-15T03:33:25.097838Z` completed all nine leaderboard slices with no source error;
- 106 recurring wallets were discovered, 30 were audited, and only two passed the mechanical screen;
- canonical raw-evidence content SHA-256 `a6e7d659e44054844c159736e18708508a96618505418e24e23a836a9a44d08f` re-verified exactly; configuration v3 later split content and gzip-file hashes into unambiguous fields;
- the two candidate wallets are `betwick` (`0xc851cd9bee7d262afd78674f861f9f576a12cd2a`) and `0x06b2934b382d4429d50d7239ee375a76167f9f35`;
- an earlier diagnostic report admitted four candidates; adversarial review found a substring classifier defect, foreign-politics leakage, a scanner-like candidate and contract-level concentration. Correcting those defects reduced the final pool to two without deleting the diagnostic evidence.

Residual gates:

- two candidates cannot satisfy a three-independent-expert rule, so no consensus signal may be generated;
- both require manual identity, strategy and wallet-independence review; one lacks a public X identity;
- recent trade and closed-position samples are capped and visibly truncated for some candidates;
- editable title keywords are a conservative screen, not authoritative market taxonomy;
- no delay replay, exact event mapping, Kalshi/Polymarket US executable-price comparison, prospective cohort freeze or paper shadow exists;
- current leaderboard selection cannot be used to claim historical out-of-sample performance.

## M4.1-A2 expanded audit and manual candidate review

Date: 2026-08-15

Decision: **PASS for deeper candidate discovery; FAIL for expert-cohort formation.** Configuration v2 changes only the maximum audited recurring wallets from 30 to 50. Every screen gate remains unchanged, and M1/M2/M3 evidence is untouched.

Evidence:

- all nine leaderboard slices completed; 108 recurring wallets were found and the top 50 were audited;
- the observation screen still returned only `betwick` (`0xc851cd9bee7d262afd78674f861f9f576a12cd2a`) and `0x06b2934b382d4429d50d7239ee375a76167f9f35`;
- report time is `2026-08-15T21:14:51.522367Z`; canonical raw-evidence content hash `ab6076e37f0a0ad9b7e67627261201522356a7752034cda8969e9f78ce1a6af8` re-verified exactly;
- their recent samples contain 16 and 22 distinct condition IDs respectively, with zero shared condition IDs.
- public identity references used for the manual review are Betwick's [Polymarket profile](https://polymarket.com/profile/0xc851cd9bee7d262afd78674f861f9f576a12cd2a), linked [X profile](https://x.com/Betwick1) and [Polymarket-published interview](https://news.polymarket.com/p/tilted-how-betwick-lost-70-of-his); the anonymous wallet exposed no comparable public identity.

Manual review:

- **Betwick — conditional observation lead, not approved expert.** The public profile links `@Betwick1`, and a Polymarket-published interview describes repeatable scenario, macro and Fed research. The audit sees 269 trades across 16 markets over 698.7 hours, 45 material actions or 1.55 per day, mostly recent sells/exits, and a truncated 50-position closed sample. The same interview reports a 70% bankroll drawdown, correlated sizing, past tilt and prior mention/movie strategies. Raw wallet direction therefore cannot be copied without position reconstruction and thesis context.
- **Anonymous index wallet — watchlist only, manually ineligible for the explainable cohort.** It has no public X identity or human-readable profile. All 500 sampled recent trades are buys in daily SPX, DJIA, DAX or FTSE up/down markets; both trade and closed samples are truncated. Its 24.54% single-event absolute-PnL concentration barely clears the 25% gate. This is a short-horizon execution-sensitive pattern, not a documented expert research process.
- **Independence remains unproved.** Zero shared markets and different strategy families are evidence against simple copying, but public proxy wallets do not prove distinct beneficial owners. More importantly, zero overlap means these two wallets cannot form same-event consensus.

Residual gates:

- at least three explainable wallets that repeatedly overlap in the same target events must be discovered without selecting on later outcomes;
- beneficial-owner independence needs public identity or on-chain relationship review; unknown is not independent;
- the copied action must reconstruct net position change, not treat every buy or sell fill as a fresh directional thesis;
- latency, observable price, capacity, correlated-event exposure and an executable benchmark must be frozen before prospective collection;
- no signal, paper position, M4 clock or runtime integration is authorized.

## M4.1-B exact-market peer discovery

Date: 2026-08-15

Decision: **PASS for offline candidate discovery; FAIL for expert-team formation.** Configuration v3 expands discovery sideways from Betwick's exact target markets. It does not convert wallet correlation into expertise, independence or alpha.

Verified attacks and controls:

- matching requires exact `conditionId`, binary direction agreement and at least three distinct markets; repeated fills in one market do not count as three experts or three events;
- 30-minute netting reconstructs one action from fragmented fills; the $100 floor applies to estimated net directional notional, so high gross turnover with near-zero net exposure cannot fake a view;
- there is no minimum probability price and no time-to-resolution admission rule;
- signed timing distinguishes leaders from followers; a review warning fires when at least 80% of three or more matches occur after the reference and within one minute;
- public API limit hits and source errors are explicit; a capped sample supports observed presence but never an absence claim;
- every peer remains `not_approved`, `team_ready` remains false, and tracking remains `not_started`;
- compressed evidence now exposes separate canonical-content and gzip-file SHA-256 values, removing the earlier ambiguous hash label.

Evidence:

- report `2026-08-15T22:43:34.485267Z` completed with no source error;
- Betwick produced 166 material target actions across 31 exact seed markets; 65 mechanical peers were found and 20 were enriched for review;
- 23 of 31 market responses reached the configured 10,000-row cap;
- gzip file SHA-256 `3895b0e13fa6aacc6d56bf1eae981463a25c126e4eac38ccd70d38d340bfadf1` and canonical content SHA-256 `0896cbbe106e529cc8443c8aa443b033c42ed86f12dd17bb21f019b2b7b12509` both re-verified;
- Kekkone matched ten actions across six markets, with zero sub-minute matches and both leading and lagging observations; this is compatible with independence but does not prove it;
- PhilIvey9 matched 15 actions across seven markets, all after Betwick and all within one minute, with a 22-second median lag. It is a useful follower control and must not count as an independent expert.

Residual gates:

- identity evidence is pseudonymous and beneficial-owner independence remains unknown;
- current-data discovery is in-sample and cannot establish forward profitability;
- sampled `SELL` actions may be exits rather than fresh opposing theses until positions are reconstructed prospectively;
- Kekkone's recent trades and closed positions are capped; Betwick's known drawdown and strategy drift remain relevant;
- a named observation panel, collection cadence, position reconstruction, delayed executable quote, benchmark and falsification rule must be approved before prospective tracking begins;
- no signal, order, paper position, credential, M2/M3 integration or real-capital path exists.

## M4.1-C prospective expert-wallet observation

Date: 2026-08-18

Decision: **PASS for isolated prospective evidence collection; FAIL for expert approval, alpha, paper positions or orders.** Configuration v4 converts the approved five-wallet panel into a time-forward measurement process. It cannot promote a wallet or trade.

Verified attacks and controls:

- the complete configuration and five roles are frozen into the first hash-chained record; later configuration drift fails closed instead of sharing the old clock;
- only post-clock trades can become evidence; the first natural cycle explicitly excluded 19 earlier Tenebrus7 trades;
- five public trade sources are collected independently every 15 minutes, while the five-minute service cadence exists only to capture due delayed quotes and resolution checks;
- exact `conditionId`, Gamma market identity, outcome-index-to-token mapping and CLOB book identity must all agree before a quote is marked recorded;
- executable best bid and ask are preserved from the actual order book; midpoint, unlimited depth and favorable later-book selection are not substituted;
- 30-minute fill netting plus a one-collection buffer delays action detection conservatively and prevents fragmented fills or brief offsetting churn from becoming repeated calls;
- the quote available when our mature action is detected and the first service quote at or after five minutes are both timestamped; actual delay is retained;
- price and time to resolution are observations, never admission gates;
- source errors, invalid trades and API-limit hits mark the collection partial and produce a nonzero service exit;
- raw compressed source batches carry canonical-content and gzip-file SHA-256 values; the append-only JSONL chain verifies sequence, timestamps and previous-record hashes;
- the M4 writer uses a separate lock and ignored evidence directory and never opens M2/M3 SQLite;
- all panel members remain `not_approved`, and every action record hard-codes `signal_authorized=false` and `paper_position_authorized=false`.

Deployment evidence:

- all 91 repository tests pass under Homebrew Python 3.11, including 21 focused M4 tests;
- `com.williamniu.polymarket-m4` completed two natural wakes with `runs=2` and `last exit code=0`; the second correctly returned idle without manufacturing a 15-minute collection;
- tracking started at `2026-08-18T20:22:28.504513Z`; collection one completed at `2026-08-18T20:22:44.976614Z` with all five sources successful and no API limit hit;
- raw file SHA-256 `25781fbcc072f029d5993a8fe6303f7f2485dc12f6a93f7eab83ae63efcdd577` and canonical content SHA-256 `41c02004faffe5a71196a039ea1dafa2f0ae432f2116081f37bc86a10533b05d` independently re-verified;
- after deployment, M2 remained healthy at cycle 1703 and M3 segment 3 had 2,446 valid intents, 38 failed probes, zero reconciliation errors, zero pending probes and an unfrozen paper account.

Residual gates:

- zero post-clock target trades and zero material actions at startup are expected and prove nothing about future activity;
- public API publication delay can exceed the one-collection buffer; observed source time, first-seen time and executable quote time must remain separate in later analysis;
- net exposure change is observable, but a wallet's private thesis, full capital base, hedges elsewhere and beneficial owner remain unknown;
- the 30-day and 30-resolved-action-per-wallet gate has not begun to mature; sparse panel members may require longer observation or a separately approved later segment;
- outcomes alone are insufficient: later scoring still needs a frozen benchmark, capacity/fees, correlation controls and time-ordered holdout rules;
- no consensus formula, holding rule, strategy position, paper signal, order, credential or real-capital path is authorized.
